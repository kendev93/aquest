"""DuckDB-backed adapter consumed by screening, charting, and backtesting."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from threading import local
from typing import NamedTuple, Protocol

import pandas as pd

from .adjustment import CorporateAction
from .models import InstrumentBoard, InstrumentType, MarketCode, StockRef
from .store import DuckDbMarketDataStore
from .store_bars import corporate_actions_from_frame

_CODE_PATTERN = re.compile(r"^\d{6}$")
_MARKET_PATTERN = re.compile(r"^(SH|SZ)[:\s-]?([0-9]{6})$", re.IGNORECASE)


class _SymbolKey(NamedTuple):
    """只有 market / code 的轻量标识。

    ``StockRef`` 要求 ``path``，而批量读事件根本不关心文件路径；造一个只带身份
    的键，避免为了查个事件去拼一个假的 Path。
    """

    market: str
    code: str


def parse_universe_code(raw: str) -> tuple[MarketCode, str] | None:
    """Parse a single universe entry into ``(market, code)``.

    Accepts ``600519``, ``SH600519``, ``SH:600519`` and ``sh 600519``. Returns
    ``None`` for blank lines. Market inference follows the stock-code rules used
    by the online sync: 6xxxxx and the 900xxx B share board are Shanghai,
    everything else resolves to Shenzhen.
    """

    normalized = raw.split("#", 1)[0].strip()
    if not normalized:
        return None
    match = _MARKET_PATTERN.fullmatch(normalized)
    if match is not None:
        market: MarketCode = "SH" if match.group(1).upper() == "SH" else "SZ"
        return market, match.group(2)
    if _CODE_PATTERN.fullmatch(normalized):
        market = "SH" if normalized.startswith("6") else "SZ"
        return market, normalized
    raise ValueError(f"无效的证券代码: {raw.strip()}")


class MarketDataAdapter(Protocol):
    """Compatibility-shaped adapter for formula and chart consumers."""

    def stock_ref(self, vipdoc_path: str | Path, market: MarketCode, code: str) -> StockRef: ...

    def list_stock_refs(
        self,
        vipdoc_path: str | Path,
        universe: str,
        universe_file: str | Path | None = None,
        instrument_types: tuple[InstrumentType, ...] | None = None,
        boards: tuple[InstrumentBoard, ...] | None = None,
    ) -> list[StockRef]: ...

    def read_stock(self, ref: StockRef) -> pd.DataFrame: ...


class BatchMarketDataAdapter(MarketDataAdapter, Protocol):
    """Adapter capabilities required by batched screening and snapshots."""

    def read_many_stocks(
        self,
        refs: list[StockRef],
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        warmup_bars: int = 0,
    ) -> pd.DataFrame: ...

    def count_many_stocks(
        self,
        refs: list[StockRef],
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> int: ...


class DuckDbMarketDataAdapter:
    """Expose the repository shape expected by existing domain services."""

    def __init__(self, store: DuckDbMarketDataStore, *, cache_stock_reads: bool = False) -> None:
        self._store = store
        self._cache_stock_reads = cache_stock_reads
        self._stock_read_cache = local()

    @property
    def database_path(self) -> Path:
        """Expose the store path so worker processes can rebuild this adapter."""

        return self._store.database_path

    def stock_ref(self, vipdoc_path: str | Path, market: MarketCode, code: str) -> StockRef:
        normalized_code = code.strip()
        refs = self.list_stock_refs(vipdoc_path, market.lower())
        for ref in refs:
            if ref.code == normalized_code:
                return ref
        raise ValueError(f"找不到已导入的行情: {market} {normalized_code}")

    def list_stock_refs(
        self,
        vipdoc_path: str | Path,
        universe: str,
        universe_file: str | Path | None = None,
        instrument_types: tuple[InstrumentType, ...] | None = None,
        boards: tuple[InstrumentBoard, ...] | None = None,
    ) -> list[StockRef]:
        source_path = Path(vipdoc_path).expanduser()
        if universe == "custom":
            if universe_file is None:
                raise ValueError("自定义股票范围必须提供股票列表文件")
            return self._list_custom_refs(
                Path(universe_file).expanduser(),
                source_path,
                instrument_types=instrument_types,
                boards=boards,
            )
        if universe not in {"all", "sh", "sz"}:
            raise ValueError(f"不支持的扫描范围: {universe}")
        market = None if universe == "all" else universe.upper()
        return [
            StockRef(
                market=ref.market,
                code=ref.code,
                name=ref.name,
                path=ref.source_path or source_path,
                instrument_type=ref.instrument_type,
                board=ref.board,
                provider=ref.provider,
                bar_count=ref.bar_count,
                first_date=ref.first_date,
                last_date=ref.last_date,
                last_close=ref.last_close,
            )
            # 只导入了目录、没有 K 线的标的（在线同步偶尔留下空壳）既不能回测
            # 也不能排序。把它们留在候选池里，全市场任务会为每一只做一次注定落空的
            # 读库和指标查询——导入一次就可能让候选数从五千涨到一万七，回测随之超时。
            for ref in self._store.list_instruments(
                market=market,
                instrument_types=instrument_types,
                boards=boards,
            )
            if (ref.bar_count or 0) > 0
        ]

    def read_corporate_actions(self, ref: StockRef) -> tuple[CorporateAction, ...]:
        """单标的的分红送转事件，指标复权时用。"""

        frame = self._store.read_corporate_actions(ref.market, ref.code)
        return tuple(corporate_actions_from_frame(frame))

    def load_corporate_actions(
        self,
        keys: Iterable[tuple[str, str]],
    ) -> dict[tuple[str, str], tuple[CorporateAction, ...]]:
        """一次取回整批标的的事件，``keys`` 是 ``(market, code)`` 序列。

        全市场 5000+ 只的规模下逐标的查事件表会变成 5000 次查询。事件表很小
        （五千只 × 平均三十个事件 ≈ 15 万行），一次拉回来在内存里分组更划算。
        没有事件的标的不会出现在结果里，调用方按缺省"无事件"处理即可。
        """

        frame = self._store.read_corporate_actions_batch(
            _SymbolKey(market, code) for market, code in keys
        )
        grouped: dict[tuple[str, str], list[CorporateAction]] = {}
        for action in corporate_actions_from_frame(frame):
            grouped.setdefault((action.market, action.code), []).append(action)
        return {key: tuple(value) for key, value in grouped.items()}

    def read_stock(self, ref: StockRef) -> pd.DataFrame:
        key = (ref.market, ref.code)
        cached_key = getattr(self._stock_read_cache, "key", None)
        cached_frame = getattr(self._stock_read_cache, "frame", None)
        if self._cache_stock_reads and cached_key == key and isinstance(cached_frame, pd.DataFrame):
            return cached_frame.copy(deep=True)

        frame = self._store.read_bars(ref.market, ref.code)
        if self._cache_stock_reads:
            # Keep one immutable full-history frame per calling thread. Research
            # stages reuse it, while different tickers never share mutable frames.
            self._stock_read_cache.key = key
            self._stock_read_cache.frame = frame.copy(deep=True)
        return frame

    def clear_thread_stock_cache(self) -> None:
        """Forget the prior ticker snapshot before starting another research run."""

        self._stock_read_cache.key = None
        self._stock_read_cache.frame = None

    def read_many_stocks(
        self,
        refs: list[StockRef],
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        warmup_bars: int = 0,
    ) -> pd.DataFrame:
        if isinstance(warmup_bars, bool) or warmup_bars < 0:
            raise ValueError("行情预热 K 线数量必须是非负整数")
        if warmup_bars > 0 and start_date is None:
            raise ValueError("行情预热 K 线需要设置开始日期")
        if self._cache_stock_reads:
            unique_refs = {(ref.market, ref.code): ref for ref in refs}
            if len(unique_refs) == 1:
                ref = next(iter(unique_refs.values()))
                frame = self.read_stock(ref)
                dates = pd.to_datetime(frame["date"], errors="coerce")
                window_mask = dates.notna()
                if start_date is not None:
                    window_mask &= dates >= pd.Timestamp(start_date)
                if end_date is not None:
                    window_mask &= dates <= pd.Timestamp(end_date)
                window = frame.loc[window_mask].copy()
                if warmup_bars > 0:
                    assert start_date is not None
                    prior = frame.loc[dates < pd.Timestamp(start_date)].tail(warmup_bars)
                    window = pd.concat((prior, window), ignore_index=True)
                return window.reset_index(drop=True)
        return self._store.read_many_bars(
            refs,
            start_date=start_date,
            end_date=end_date,
            warmup_bars=warmup_bars,
        )

    def count_many_stocks(
        self,
        refs: list[StockRef],
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> int:
        return self._store.count_many_bars(refs, start_date=start_date, end_date=end_date)

    def _list_custom_refs(
        self,
        universe_file: Path,
        source_path: Path,
        *,
        instrument_types: tuple[InstrumentType, ...] | None = None,
        boards: tuple[InstrumentBoard, ...] | None = None,
    ) -> list[StockRef]:
        if not universe_file.is_file():
            raise ValueError(f"自定义股票列表文件不存在: {universe_file}")
        requested: list[tuple[MarketCode, str]] = []
        seen: set[tuple[MarketCode, str]] = set()
        for line in universe_file.read_text(encoding="utf-8").splitlines():
            parsed = self._parse_universe_line(line)
            if parsed is None or parsed in seen:
                continue
            seen.add(parsed)
            requested.append(parsed)
        available = {
            (ref.market, ref.code): ref
            for ref in self._store.list_instruments(include_missing=True)
        }
        return [
            StockRef(
                market=market,
                code=code,
                name=available[(market, code)].name,
                path=available[(market, code)].source_path or source_path,
                instrument_type=available[(market, code)].instrument_type,
                board=available[(market, code)].board,
                provider=available[(market, code)].provider,
                bar_count=available[(market, code)].bar_count,
                first_date=available[(market, code)].first_date,
                last_date=available[(market, code)].last_date,
                last_close=available[(market, code)].last_close,
            )
            for market, code in requested
            if (market, code) in available
            and (
                instrument_types is None
                or available[(market, code)].instrument_type in instrument_types
            )
            and (boards is None or available[(market, code)].board in boards)
        ]

    @staticmethod
    def _parse_universe_line(line: str) -> tuple[MarketCode, str] | None:
        try:
            return parse_universe_code(line)
        except ValueError as exc:
            raise ValueError(f"自定义股票列表中的格式无效: {line.strip()}") from exc
