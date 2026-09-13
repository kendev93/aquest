"""DuckDB-backed adapter consumed by screening, charting, and backtesting."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Protocol

import pandas as pd

from .models import InstrumentBoard, InstrumentType, MarketCode, StockRef
from .store import DuckDbMarketDataStore

_CODE_PATTERN = re.compile(r"^\d{6}$")
_MARKET_PATTERN = re.compile(r"^(SH|SZ)[:\s-]?([0-9]{6})$", re.IGNORECASE)


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

    def __init__(self, store: DuckDbMarketDataStore) -> None:
        self._store = store

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
            for ref in self._store.list_instruments(
                market=market,
                instrument_types=instrument_types,
                boards=boards,
            )
        ]

    def read_stock(self, ref: StockRef) -> pd.DataFrame:
        return self._store.read_bars(ref.market, ref.code)

    def read_many_stocks(
        self,
        refs: list[StockRef],
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        warmup_bars: int = 0,
    ) -> pd.DataFrame:
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
        normalized = line.split("#", 1)[0].strip()
        if not normalized:
            return None
        match = _MARKET_PATTERN.fullmatch(normalized)
        if match:
            market: MarketCode = "SH" if match.group(1).upper() == "SH" else "SZ"
            return market, match.group(2)
        if _CODE_PATTERN.fullmatch(normalized):
            market = "SH" if normalized.startswith("6") else "SZ"
            return market, normalized
        raise ValueError(f"自定义股票列表中的格式无效: {line.strip()}")
