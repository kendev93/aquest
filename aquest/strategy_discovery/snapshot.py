"""Read-only, memory-mapped market data for repeated strategy evaluations."""

from __future__ import annotations

import gc
import hashlib
import json
import shutil
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp
from typing import Any, cast

import numpy as np
import pandas as pd

from aquest.formulas.registry import FORMULA_REGISTRY
from aquest.formulas.types import FormulaResult
from aquest.indicators.cache import IndicatorCache, frame_fingerprint
from aquest.market_data.adapter import (
    BatchMarketDataAdapter,
    DuckDbMarketDataAdapter,
    MarketDataAdapter,
    parse_universe_code,
)
from aquest.market_data.adjustment import mark_adjusted
from aquest.market_data.day_format import classify_board
from aquest.market_data.models import InstrumentBoard, InstrumentType, MarketCode, StockRef
from aquest.market_data.store import DuckDbMarketDataStore
from aquest.portfolio_backtest.context import SortedDateIndex
from aquest.portfolio_backtest.models import (
    SINGLE_SECURITY_RANKING_VALUE,
    PortfolioBacktestConfig,
)
from aquest.portfolio_backtest.service import (
    PortfolioBacktestService,
    _atr_series,
    _StockSeries,
)
from aquest.screening.engine import evaluate_rule_group
from aquest.screening.models import ScreenCondition
from aquest.screening.rules import RuleGroup

from .catalog import default_indicator_catalog
from .models import DiscoveryRequest

#: 快照行情数组的行名。``adjusted_close``（第 6 行，索引 5）是给**买入持有基准**
#: 用的复权收盘价：持有者会收到分红，不复权 close 会把这部分收益全部漏掉，基准
#: 会被系统性低估（浦发银行 2015~2026 名义 -41%，含分红实际 +31%）。成交、整手、
#: ATR 仍然只认 raw 的 ``close``，两边各答各的问题。
_BAR_ARRAY_NAMES = ("open", "high", "low", "close", "atr", "adjusted_close")
#: 1 → 2：新增 ``adjusted_close`` 行。旧快照行数不对，版本号不匹配会让它们重建
#: 而不是被误读成旧口径。
_SNAPSHOT_VERSION = 2
SNAPSHOT_SYMBOL_BATCH_SIZE = 64


@dataclass(frozen=True)
class _ArrayFormulaResult:
    """Small duck-typed subset consumed by ``evaluate_rule_group``."""

    values: Mapping[str, np.ndarray]
    signals: Mapping[str, np.ndarray]


@dataclass(frozen=True)
class _SnapshotSymbol:
    market: str
    code: str
    instrument_type: InstrumentType
    board: InstrumentBoard
    offset: int
    length: int
    data_fingerprint: str

    @property
    def symbol(self) -> str:
        return f"{self.market}{self.code}"


@dataclass(frozen=True)
class _SnapshotBaseSeries:
    """Read-only per-symbol arrays shared by all candidates in one worker."""

    market: str
    code: str
    instrument_type: InstrumentType
    board: InstrumentBoard
    dates: np.ndarray
    opens: np.ndarray
    highs: np.ndarray
    lows: np.ndarray
    closes: np.ndarray
    atrs: np.ndarray
    buy_required: np.ndarray
    sell_required: np.ndarray
    signals: Mapping[str, np.ndarray]
    values: Mapping[str, np.ndarray]
    formula_results: Mapping[str, _ArrayFormulaResult]
    date_to_index: Mapping[int, int]
    data_fingerprint: str
    #: 复权收盘价（快照数组第 6 行），仅供买入持有基准；拿不到时退回 ``closes``。
    adjusted_closes: np.ndarray | None = None

    @property
    def symbol(self) -> str:
        return f"{self.market}{self.code}"


@dataclass(frozen=True)
class SnapshotWindow:
    """The inclusive date window materialized into one discovery snapshot."""

    start: date
    end: date


class DiscoverySnapshotCache:
    """One-entry, worker-lifetime cache for immutable discovery snapshots."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._key: str | None = None
        self._path: Path | None = None

    def get_or_build(
        self,
        key: str,
        builder: Callable[[Path], None],
    ) -> tuple[Path, bool]:
        current = self._path
        if key == self._key and current is not None and (current / "manifest.json").is_file():
            return current, True

        # The strategy worker claims jobs serially and waits for its candidate
        # pool to shut down before the next job, so its previous snapshot is no
        # longer mapped when this single cache entry is replaced.
        self.invalidate()
        path = Path(mkdtemp(prefix="snapshot-", dir=self.root))
        try:
            builder(path)
        except BaseException:
            shutil.rmtree(path, ignore_errors=True)
            raise
        self._key = key
        self._path = path
        return path, False

    def invalidate(self) -> None:
        path = self._path
        self._path = None
        self._key = None
        if path is not None:
            shutil.rmtree(path, ignore_errors=True)


class DiscoverySnapshot:
    """Open a compact snapshot without copying its arrays into each worker."""

    def __init__(
        self,
        root: str | Path,
        *,
        max_cached_symbols: int = 256,
        max_cached_date_indexes: int = 512,
    ) -> None:
        if isinstance(max_cached_symbols, bool) or max_cached_symbols < 1:
            raise ValueError("策略实验标的缓存上限必须大于 0")
        if isinstance(max_cached_date_indexes, bool) or max_cached_date_indexes < 1:
            raise ValueError("策略实验日期索引缓存上限必须大于 0")
        self.root = Path(root)
        manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("version") != _SNAPSHOT_VERSION:
            raise ValueError("策略实验行情快照版本不受支持")
        self.total_candidates = int(manifest["total_candidates"])
        raw_build_metadata = manifest.get("build_metadata")
        self.build_metadata = (
            {str(key): value for key, value in raw_build_metadata.items()}
            if isinstance(raw_build_metadata, Mapping)
            else {}
        )
        self._value_ids = tuple(str(value) for value in manifest["value_ids"])
        self._signal_ids = tuple(str(signal) for signal in manifest["signal_ids"])
        self._value_indexes = {value: index for index, value in enumerate(self._value_ids)}
        self._signal_indexes = {signal: index for index, signal in enumerate(self._signal_ids)}
        self._symbols = tuple(
            _SnapshotSymbol(
                market=str(item["market"]),
                code=str(item["code"]),
                instrument_type=cast(InstrumentType, str(item["instrument_type"])),
                board=cast(InstrumentBoard, str(item["board"])),
                offset=int(item["offset"]),
                length=int(item["length"]),
                data_fingerprint=str(item["data_fingerprint"]),
            )
            for item in manifest["symbols"]
        )
        self._dates = np.load(self.root / "dates.npy", mmap_mode="r", allow_pickle=False)
        self._bars = np.load(self.root / "bars.npy", mmap_mode="r", allow_pickle=False)
        self._values = np.load(self.root / "values.npy", mmap_mode="r", allow_pickle=False)
        self._signals = np.load(self.root / "signals.npy", mmap_mode="r", allow_pickle=False)
        self._required_buy = np.load(
            self.root / "required_buy.npy", mmap_mode="r", allow_pickle=False
        )
        self._required_sell = np.load(
            self.root / "required_sell.npy", mmap_mode="r", allow_pickle=False
        )
        self._required_buy_conditions = tuple(
            _condition_from_dict(item) for item in manifest["required_buy_conditions"]
        )
        self._required_sell_conditions = tuple(
            _condition_from_dict(item) for item in manifest["required_sell_conditions"]
        )
        self._max_cached_symbols = max_cached_symbols
        self._max_cached_date_indexes = max_cached_date_indexes
        self._base_series_cache: OrderedDict[str, _SnapshotBaseSeries] = OrderedDict()
        self._date_index_cache: OrderedDict[tuple[str, int, int], Mapping[int, int]] = OrderedDict()
        self._base_context_cache_hits = 0
        self._candidate_context_cache_key: str | None = None
        self._candidate_context_cache: list[_StockSeries] | None = None
        self._candidate_context_cache_hits = 0

    @property
    def context_cache_metrics(self) -> dict[str, int]:
        return {
            "base_contexts_built": len(self._base_series_cache),
            "base_context_cache_hits": self._base_context_cache_hits,
            "date_index_cache_entries": len(self._date_index_cache),
            "base_context_cache_limit": self._max_cached_symbols,
            "date_index_cache_limit": self._max_cached_date_indexes,
            "candidate_context_cache_hits": self._candidate_context_cache_hits,
        }

    def close(self) -> None:
        """Release mapped files before the temporary directory is removed."""

        self._base_series_cache.clear()
        self._date_index_cache.clear()
        self._candidate_context_cache = None
        self._candidate_context_cache_key = None
        for name in (
            "_dates",
            "_bars",
            "_values",
            "_signals",
            "_required_buy",
            "_required_sell",
        ):
            setattr(self, name, None)
        gc.collect()

    def __enter__(self) -> DiscoverySnapshot:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def contexts_for_config(
        self,
        config: PortfolioBacktestConfig,
        *,
        reuse_candidate_contexts: bool = False,
    ) -> list[_StockSeries]:
        """Build cheap candidate-specific contexts from the shared arrays."""

        if config.formula_text:
            raise ValueError("共享行情快照不支持自定义公式")
        if config.buy_rule is None:
            raise ValueError("买入规则不能为空")

        cache_key = _context_config_cache_key(config) if reuse_candidate_contexts else None
        if cache_key is not None:
            if cache_key == self._candidate_context_cache_key:
                cached = self._candidate_context_cache
                if cached is not None:
                    self._candidate_context_cache_hits += 1
                    return cached
            # Keep one window per worker. Coarse, medium, and full fidelities
            # use different date windows, so retaining all three can multiply
            # the per-process context memory without helping later stages.
            self._candidate_context_cache_key = None
            self._candidate_context_cache = None

        value_ids = _value_ids(config)
        signal_ids = _rule_signal_ids(config.buy_rule, config.sell_rule)
        formula_ids = {value_id.split(".", 1)[0] for value_id in value_ids}
        formula_ids.update(
            FORMULA_REGISTRY.signal(signal_id).formula_id for signal_id in signal_ids
        )
        minimum_bars = max(
            FORMULA_REGISTRY.formula(formula_id).minimum_bars for formula_id in formula_ids
        )
        start_index = _date_int(config.start_date) if config.start_date else None
        end_index = _date_int(config.end_date) if config.end_date else None
        contexts: list[_StockSeries] = []
        for item in self._symbols:
            if item.length < minimum_bars:
                continue
            base = self._base_series(item)
            dates = base.dates
            left = int(np.searchsorted(dates, start_index, side="left")) if start_index else 0
            right = (
                int(np.searchsorted(dates, end_index, side="right")) if end_index else item.length
            )
            if left >= right:
                continue

            formula_results = base.formula_results
            buy_mask = self._candidate_mask(
                config.buy_rule,
                self._required_buy_conditions,
                base.buy_required,
                formula_results,
            )
            sell_mask = (
                self._candidate_mask(
                    config.sell_rule,
                    self._required_sell_conditions,
                    base.sell_required,
                    formula_results,
                )
                if config.sell_rule is not None
                else np.zeros(item.length, dtype=bool)
            )
            window_dates = np.asarray(dates[left:right])
            context_values = {value_id: base.values[value_id][left:right] for value_id in value_ids}
            if config.single_security_mode:
                context_values[SINGLE_SECURITY_RANKING_VALUE] = np.zeros(
                    right - left,
                    dtype=float,
                )
            context_signals = {
                signal_id: base.signals[signal_id][left:right] for signal_id in signal_ids
            }
            context_dates = np.asarray(window_dates, dtype=np.int64)
            date_to_index = self._date_index(
                base,
                left,
                right,
            )
            contexts.append(
                _StockSeries(
                    market=base.market,
                    code=base.code,
                    instrument_type=base.instrument_type,
                    board=base.board,
                    dates=context_dates,
                    opens=base.opens[left:right],
                    highs=base.highs[left:right],
                    lows=base.lows[left:right],
                    closes=base.closes[left:right],
                    atrs=base.atrs[left:right],
                    adjusted_closes=(
                        base.adjusted_closes[left:right]
                        if base.adjusted_closes is not None
                        else None
                    ),
                    buy_mask=np.asarray(buy_mask[left:right], dtype=bool),
                    sell_mask=np.asarray(sell_mask[left:right], dtype=bool),
                    signals=context_signals,
                    values=context_values,
                    date_to_index=date_to_index,
                    data_fingerprint=item.data_fingerprint,
                )
            )
        if cache_key is not None:
            self._candidate_context_cache_key = cache_key
            self._candidate_context_cache = contexts
        return contexts

    def _base_series(self, item: _SnapshotSymbol) -> _SnapshotBaseSeries:
        cached = self._base_series_cache.get(item.symbol)
        if cached is not None:
            self._base_context_cache_hits += 1
            self._base_series_cache.move_to_end(item.symbol)
            return cached
        offset = item.offset
        limit = offset + item.length
        dates = np.asarray(self._dates[offset:limit], dtype=np.int64)
        opens = np.asarray(self._bars[0, offset:limit], dtype=float)
        highs = np.asarray(self._bars[1, offset:limit], dtype=float)
        lows = np.asarray(self._bars[2, offset:limit], dtype=float)
        closes = np.asarray(self._bars[3, offset:limit], dtype=float)
        atrs = np.asarray(self._bars[4, offset:limit], dtype=float)
        # 第 6 行是复权收盘价（_SNAPSHOT_VERSION 2 起才有）。旧快照会被版本号拦下，
        # 这里的形状判断是给半旧的、已落盘但未被清理的文件兜底。
        adjusted_closes: np.ndarray | None = None
        if self._bars.shape[0] > 5:
            adjusted_closes = np.asarray(self._bars[5, offset:limit], dtype=float)
        values = {
            value_id: np.asarray(
                self._values[self._value_indexes[value_id], offset:limit], dtype=float
            )
            for value_id in self._value_ids
        }
        signals = {
            signal_id: np.asarray(
                self._signals[self._signal_indexes[signal_id], offset:limit], dtype=bool
            )
            for signal_id in self._signal_ids
        }
        formula_results: dict[str, _ArrayFormulaResult] = {}
        for formula_id in FORMULA_REGISTRY.all_formula_ids():
            formula_results[formula_id] = _ArrayFormulaResult(
                values={
                    value_id.split(".", 1)[1]: value
                    for value_id, value in values.items()
                    if value_id.startswith(f"{formula_id}.")
                },
                signals={
                    signal_id.split(".", 1)[1]: signal
                    for signal_id, signal in signals.items()
                    if FORMULA_REGISTRY.signal(signal_id).formula_id == formula_id
                },
            )
        base = _SnapshotBaseSeries(
            market=item.market,
            code=item.code,
            instrument_type=item.instrument_type,
            board=item.board,
            dates=dates,
            opens=opens,
            highs=highs,
            lows=lows,
            closes=closes,
            atrs=atrs,
            adjusted_closes=adjusted_closes,
            buy_required=np.asarray(self._required_buy[offset:limit], dtype=bool),
            sell_required=np.asarray(self._required_sell[offset:limit], dtype=bool),
            signals=signals,
            values=values,
            formula_results=formula_results,
            date_to_index=SortedDateIndex(dates),
            data_fingerprint=item.data_fingerprint,
        )
        self._base_series_cache[item.symbol] = base
        self._base_series_cache.move_to_end(item.symbol)
        while len(self._base_series_cache) > self._max_cached_symbols:
            self._base_series_cache.popitem(last=False)
        return base

    def _date_index(
        self,
        base: _SnapshotBaseSeries,
        left: int,
        right: int,
    ) -> Mapping[int, int]:
        key = (base.symbol, left, right)
        cached = self._date_index_cache.get(key)
        if cached is not None:
            self._date_index_cache.move_to_end(key)
            return cached
        mapping: Mapping[int, int] = SortedDateIndex(base.dates[left:right])
        self._date_index_cache[key] = mapping
        self._date_index_cache.move_to_end(key)
        while len(self._date_index_cache) > self._max_cached_date_indexes:
            self._date_index_cache.popitem(last=False)
        return mapping

    def _candidate_mask(
        self,
        rule: RuleGroup,
        required_conditions: tuple[ScreenCondition, ...],
        required_mask: np.ndarray,
        formula_results: Mapping[str, _ArrayFormulaResult],
    ) -> np.ndarray:
        cast_results = cast(Mapping[str, FormulaResult], formula_results)
        if not required_conditions and not rule.required_conditions:
            return evaluate_rule_group(rule, cast_results, FORMULA_REGISTRY)
        if rule.required_conditions:
            if required_conditions and set(rule.required_conditions) != set(required_conditions):
                raise ValueError("候选规则缺少用户设置的必选条件")
            if required_conditions:
                effective_required_mask = required_mask
            else:
                required_rule = RuleGroup(
                    conditions=rule.required_conditions,
                    combine_mode="all",
                )
                effective_required_mask = np.asarray(
                    evaluate_rule_group(required_rule, cast_results, FORMULA_REGISTRY),
                    dtype=bool,
                )
            auto_conditions = tuple(rule.conditions)
        else:
            required_set = set(required_conditions)
            if not required_set.issubset(set(rule.conditions)):
                raise ValueError("候选规则缺少用户设置的必选条件")
            auto_conditions = tuple(
                condition for condition in rule.conditions if condition not in required_set
            )
            effective_required_mask = required_mask
        if not rule.signals and not auto_conditions:
            return np.asarray(effective_required_mask, dtype=bool)
        auto_rule = RuleGroup(
            signals=rule.signals,
            conditions=auto_conditions,
            combine_mode=rule.combine_mode,
            minimum_matches=rule.minimum_matches,
        )
        auto_mask = np.asarray(
            evaluate_rule_group(
                auto_rule,
                cast_results,
                FORMULA_REGISTRY,
            ),
            dtype=bool,
        )
        combined = np.asarray(effective_required_mask, dtype=bool) & auto_mask
        return cast(np.ndarray, combined)

    def _formula_results(
        self,
        formula_ids: set[str],
        signal_ids: tuple[str, ...],
        value_ids: tuple[str, ...],
        offset: int,
        limit: int,
    ) -> dict[str, _ArrayFormulaResult]:
        results: dict[str, _ArrayFormulaResult] = {}
        for formula_id in formula_ids:
            values = {
                value_id.split(".", 1)[1]: np.asarray(
                    self._values[self._value_indexes[value_id], offset:limit], dtype=float
                )
                for value_id in value_ids
                if value_id.startswith(f"{formula_id}.")
            }
            signals = {
                signal_id.split(".", 1)[1]: np.asarray(
                    self._signals[self._signal_indexes[signal_id], offset:limit], dtype=bool
                )
                for signal_id in signal_ids
                if FORMULA_REGISTRY.signal(signal_id).formula_id == formula_id
            }
            results[formula_id] = _ArrayFormulaResult(values=values, signals=signals)
        return results


def _snapshot_atr_period(base: Mapping[str, object]) -> int:
    """快照里那条 ``atr`` 数组用的周期，与候选风控 ATR 同源。

    写死 14 会让「研究配置声明了别的周期」时搜索用的线和最终回放用的线不是同一条；
    寻优本身不搜周期（只搜模式与倍数），所以这里跟着请求走就够了。
    """

    value = base.get("atr_period")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return 14
    return int(value)


def _discovery_snapshot_cache_key(
    store: DuckDbMarketDataStore,
    request: DiscoveryRequest,
    adapter: BatchMarketDataAdapter,
) -> str:
    base = request.base_config
    instrument_types = _string_tuple(base.get("instrument_types"))
    boards = _string_tuple(base.get("boards"))
    universe = str(base.get("universe") or "all")
    universe_file = str(base["universe_file"]) if base.get("universe_file") else None
    refs = _request_refs(
        adapter,
        base,
        universe=universe,
        universe_file=universe_file,
        instrument_types=cast(tuple[InstrumentType, ...] | None, instrument_types),
        boards=cast(tuple[InstrumentBoard, ...] | None, boards),
    )
    revisions = store.data_revisions((ref.market, ref.code) for ref in refs)
    window = snapshot_window(request)
    catalog = default_indicator_catalog()
    payload = {
        "snapshot_version": _SNAPSHOT_VERSION,
        # 事件补录或修正不会动任何一根 bar，但复权后的指标序列会变，所以快照缓存
        # 必须跟着事件表的内容走，否则事件表更新后策略实验仍在用旧的 raw 指标。
        "corporate_actions_version": store.corporate_actions_version(),
        "window": (window.start.isoformat(), window.end.isoformat()),
        "warmup_bars": _snapshot_warmup_bars(),
        "scope": {
            "universe": universe,
            "universe_file": universe_file,
            "instrument_types": instrument_types,
            "boards": boards,
            "codes": base.get("codes"),
            "exclude_st": base.get("exclude_st", False),
            "formula_text": base.get("formula_text"),
            "formula_parameters": base.get("formula_parameters", {}),
        },
        "symbols": [
            (
                ref.market,
                ref.code,
                ref.instrument_type,
                ref.board,
                revisions.get((str(ref.market).upper(), str(ref.code).strip())),
            )
            for ref in sorted(refs, key=lambda item: (item.market, item.code))
        ],
        "required_buy_conditions": [
            _condition_to_dict(condition) for condition in request.required_buy_conditions
        ],
        "required_sell_conditions": [
            _condition_to_dict(condition) for condition in request.required_sell_conditions
        ],
        "value_ids": list(catalog.value_ids),
        "signal_ids": list(catalog.signal_ids),
        "formula_versions": [
            (
                formula_id,
                FORMULA_REGISTRY.formula(formula_id).cache_version,
                FORMULA_REGISTRY.formula(formula_id).minimum_bars,
                FORMULA_REGISTRY.formula(formula_id).recommended_bars,
            )
            for formula_id in FORMULA_REGISTRY.all_formula_ids()
        ],
        "atr_period": _snapshot_atr_period(base),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@contextmanager
def build_discovery_snapshot(
    store: DuckDbMarketDataStore,
    request: DiscoveryRequest,
    *,
    adapter: MarketDataAdapter | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
    abort_check: Callable[[], bool] | None = None,
    persist_indicator_cache: bool = True,
    cache: DiscoverySnapshotCache | None = None,
) -> Iterator[DiscoverySnapshot]:
    """Materialize one run snapshot and keep it alive for all workers."""

    selected_adapter = cast(
        BatchMarketDataAdapter,
        adapter
        or DuckDbMarketDataAdapter(DuckDbMarketDataStore(store.database_path, read_only=True)),
    )

    def build(root: Path) -> None:
        _write_snapshot(
            root,
            store,
            request,
            adapter=selected_adapter,
            progress_callback=progress_callback,
            abort_check=abort_check,
            persist_indicator_cache=persist_indicator_cache,
        )

    if cache is not None:
        if abort_check is not None and abort_check():
            raise RuntimeError("策略实验已取消")
        cache_key = _discovery_snapshot_cache_key(store, request, selected_adapter)
        root, cache_hit = cache.get_or_build(cache_key, build)
        try:
            snapshot = DiscoverySnapshot(root)
        except (OSError, ValueError, KeyError, EOFError):
            if not cache_hit:
                raise
            cache.invalidate()
            root, _ = cache.get_or_build(cache_key, build)
            snapshot = DiscoverySnapshot(root)
            cache_hit = False
        snapshot.build_metadata["snapshot_cache_hit"] = cache_hit
        try:
            yield snapshot
        finally:
            snapshot.close()
        return

    with TemporaryDirectory(prefix="aquest-discovery-") as temporary_root:
        root = Path(temporary_root)
        build(root)
        snapshot = DiscoverySnapshot(root)
        try:
            yield snapshot
        finally:
            snapshot.close()


def _write_snapshot(
    root: Path,
    store: DuckDbMarketDataStore,
    request: DiscoveryRequest,
    *,
    adapter: MarketDataAdapter | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
    abort_check: Callable[[], bool] | None = None,
    persist_indicator_cache: bool = True,
) -> None:
    snapshot_started = time.perf_counter()
    base = request.base_config
    atr_period = _snapshot_atr_period(base)
    window = snapshot_window(request)
    instrument_types = _string_tuple(base.get("instrument_types"))
    boards = _string_tuple(base.get("boards"))
    selected_adapter = cast(
        BatchMarketDataAdapter,
        adapter
        or DuckDbMarketDataAdapter(DuckDbMarketDataStore(store.database_path, read_only=True)),
    )
    refs = _request_refs(
        selected_adapter,
        base,
        universe=str(base.get("universe") or "all"),
        universe_file=str(base["universe_file"]) if base.get("universe_file") else None,
        instrument_types=cast(tuple[InstrumentType, ...] | None, instrument_types),
        boards=cast(tuple[InstrumentBoard, ...] | None, boards),
    )
    total_rows = selected_adapter.count_many_stocks(
        refs,
        start_date=window.start,
        end_date=window.end,
    )
    if total_rows <= 0:
        raise ValueError("行情数据库没有可用日线，请先同步行情")
    if abort_check is not None and abort_check():
        raise RuntimeError("策略实验已取消")

    catalog = default_indicator_catalog()
    # Warm every numeric output, not just the rankable ones. Snapshot columns
    # are a compute cache: a candidate may legitimately reference a price-scaled
    # value in a condition ("close 上穿 MA20") even though that value must never
    # be mined into a cross-stock threshold.
    value_ids = tuple(catalog.value_ids)
    signal_ids = tuple(catalog.signal_ids)
    formula_ids = tuple(FORMULA_REGISTRY.all_formula_ids())
    bars = _open_snapshot_memmap(
        root / "bars.npy",
        dtype=np.float64,
        shape=(len(_BAR_ARRAY_NAMES), total_rows),
    )
    dates = _open_snapshot_memmap(root / "dates.npy", dtype=np.int64, shape=(total_rows,))
    values = _open_snapshot_memmap(
        root / "values.npy",
        dtype=np.float64,
        shape=(len(value_ids), total_rows),
    )
    signals = _open_snapshot_memmap(
        root / "signals.npy",
        dtype=np.bool_,
        shape=(len(signal_ids), total_rows),
    )
    required_buy = _open_snapshot_memmap(
        root / "required_buy.npy", dtype=np.bool_, shape=(total_rows,)
    )
    required_sell = _open_snapshot_memmap(
        root / "required_sell.npy", dtype=np.bool_, shape=(total_rows,)
    )
    required_buy_rule = _required_rule(request.required_buy_conditions)
    required_sell_rule = _required_rule(request.required_sell_conditions)
    ref_by_key = {(ref.market, ref.code): ref for ref in refs}
    memory_cache = IndicatorCache(
        store if persist_indicator_cache else None,
        max_memory_entries=max(128, len(formula_ids) * 64),
    )
    symbol_rows: list[dict[str, object]] = []
    cursor = 0
    # 指纹取自"窗口 + 预热"这份 frame，跟组合回测读的是同一份。早先为了对齐"用全
    # 历史复核"的回测，这里会先把全历史整读一遍来算指纹——多一次全市场扫描，而现在
    # 回测本身也只读窗口，那份指纹反而是唯一对不上的东西。
    warmup_bars = _snapshot_warmup_bars()
    indicator_compute_started = time.perf_counter()
    if progress_callback is not None:
        progress_callback(0, len(refs))
    group_index = 0
    for ref_batch in _iter_ref_batches(refs):
        if abort_check is not None and abort_check():
            raise RuntimeError("策略实验已取消")
        combined = selected_adapter.read_many_stocks(
            ref_batch,
            start_date=window.start,
            end_date=window.end,
            warmup_bars=warmup_bars,
        )
        if combined.empty:
            group_index += len(ref_batch)
            del combined
            continue
        prepared_batch = [
            ((str(key[0]), str(key[1])), PortfolioBacktestService._prepare_frame(group))
            for key, group in combined.groupby(["market", "code"], sort=False)
        ]
        frames_batch = dict(prepared_batch)
        # 指标按除权校正后的价格算，成交价与 ATR 仍用 raw（ATR 的单位是"元"，会和
        # 成交价直接加减运算）。事件跟着这一批行情一次拉回来：逐标的查会在全市场
        # 5000+ 只的规模下变成 5000 次查询。
        action_loader = getattr(selected_adapter, "load_corporate_actions", None)
        actions_by_key = dict(action_loader(frames_batch.keys())) if callable(action_loader) else {}
        metric_frames = {
            key: mark_adjusted(frame, actions_by_key.get(key, ()))
            for key, frame in frames_batch.items()
        }
        memory_cache.preload(metric_frames, formula_ids)
        computed_before = int(memory_cache.metrics()["computed"])
        results_batch: dict[tuple[str, str], Mapping[str, FormulaResult]] = {}
        for (market, code), prepared in prepared_batch:
            group_index += 1
            if abort_check is not None and abort_check():
                raise RuntimeError("策略实验已取消")
            timestamps = pd.to_datetime(prepared["date"], errors="coerce")
            selected = (timestamps >= pd.Timestamp(window.start)) & (
                timestamps <= pd.Timestamp(window.end)
            )
            positions = np.flatnonzero(selected.to_numpy(dtype=bool, copy=False))
            if len(positions) == 0:
                continue
            length = len(positions)
            end = cursor + length
            result_by_formula = memory_cache.get_or_compute(
                metric_frames[(market, code)],
                formula_ids,
                market=market,
                code=code,
                persistent_read=False,
                persist=False,
            )
            results_batch[(market, code)] = result_by_formula
            dates[cursor:end] = np.asarray(
                timestamps.iloc[positions].dt.strftime("%Y%m%d"), dtype=np.int64
            )
            for index, column in enumerate(("open", "high", "low", "close")):
                bars[index, cursor:end] = prepared[column].to_numpy(dtype=float, copy=False)[
                    positions
                ]
            bars[4, cursor:end] = _atr_series(prepared, atr_period)[positions]
            # 基准口径：复权收盘价。metric_frames 已经在上面按事件校正过，
            # 这里只是把同一段窗口搬进快照数组，避免基准再单独做一次复权。
            bars[5, cursor:end] = metric_frames[(market, code)]["close"].to_numpy(
                dtype=float, copy=False
            )[positions]
            required_buy_mask = _evaluate_required_mask(
                required_buy_rule, result_by_formula, len(prepared)
            )
            required_sell_mask = _evaluate_required_mask(
                required_sell_rule, result_by_formula, len(prepared)
            )
            required_buy[cursor:end] = required_buy_mask[positions]
            required_sell[cursor:end] = required_sell_mask[positions]
            for index, value_id in enumerate(value_ids):
                formula_id, _, value_name = value_id.partition(".")
                value = result_by_formula[formula_id].values.get(value_name)
                value_array = np.asarray(value, dtype=float) if value is not None else None
                values[index, cursor:end] = (
                    value_array[positions]
                    if value_array is not None and len(value_array) == len(prepared)
                    else np.nan
                )
            for index, signal_id in enumerate(signal_ids):
                formula_id = FORMULA_REGISTRY.signal(signal_id).formula_id
                signal_name = signal_id.split(".", 1)[1]
                signal = result_by_formula[formula_id].signals.get(signal_name)
                signal_array = np.asarray(signal, dtype=bool) if signal is not None else None
                signals[index, cursor:end] = (
                    signal_array[positions]
                    if signal_array is not None and len(signal_array) == len(prepared)
                    else False
                )
            ref = ref_by_key[(cast(MarketCode, market), code)]
            symbol_rows.append(
                {
                    "market": market,
                    "code": code,
                    "instrument_type": str(getattr(ref, "instrument_type", "stock")),
                    "board": str(classify_board(market, code) or getattr(ref, "board", "main")),
                    "offset": cursor,
                    "length": length,
                    "data_fingerprint": frame_fingerprint(prepared),
                }
            )
            cursor = end
            if progress_callback is not None and (
                group_index % 25 == 0 or group_index >= len(refs)
            ):
                progress_callback(min(group_index, len(refs)), len(refs))
        if persist_indicator_cache and int(memory_cache.metrics()["computed"]) > computed_before:
            memory_cache.persist_results(frames_batch, results_batch)
        del results_batch, frames_batch, prepared_batch, combined
        gc.collect()
    if cursor != total_rows:
        raise RuntimeError(f"策略实验行情快照行数不一致，预期 {total_rows}，实际 {cursor}")
    indicator_compute_seconds = time.perf_counter() - indicator_compute_started
    if progress_callback is not None and symbol_rows:
        progress_callback(len(refs), len(refs))
    for mapped in (bars, dates, values, signals, required_buy, required_sell):
        mapped.flush()
    snapshot_bytes = sum(
        int(mapped.nbytes) for mapped in (bars, dates, values, signals, required_buy, required_sell)
    )
    del bars, dates, values, signals, required_buy, required_sell
    gc.collect()
    cache_metrics = memory_cache.metrics()
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "version": _SNAPSHOT_VERSION,
                "total_candidates": len(refs),
                "value_ids": list(value_ids),
                "signal_ids": list(signal_ids),
                "required_buy_conditions": [
                    _condition_to_dict(condition) for condition in request.required_buy_conditions
                ],
                "required_sell_conditions": [
                    _condition_to_dict(condition) for condition in request.required_sell_conditions
                ],
                "build_metadata": {
                    "snapshot_seconds": time.perf_counter() - snapshot_started,
                    "indicator_compute_seconds": indicator_compute_seconds,
                    "indicator_cache_lookups": cache_metrics["lookups"],
                    "indicator_cache_hits": cache_metrics["hits"],
                    "indicator_cache_misses": cache_metrics["misses"],
                    "indicator_persistent_hits": cache_metrics["persistent_hits"],
                    "indicator_computed": cache_metrics["computed"],
                    "indicator_cache_hit_rate": cache_metrics["hit_rate"],
                    "indicator_persistent_read_seconds": (
                        float(cache_metrics["persistent_read_time_ms"]) / 1000.0
                    ),
                    "indicator_persistent_write_seconds": (
                        float(cache_metrics["persistent_write_time_ms"]) / 1000.0
                    ),
                    "snapshot_start_date": window.start.isoformat(),
                    "snapshot_end_date": window.end.isoformat(),
                    "snapshot_rows": total_rows,
                    "snapshot_batch_size": SNAPSHOT_SYMBOL_BATCH_SIZE,
                    "warmup_bars": warmup_bars,
                    "snapshot_bytes": snapshot_bytes,
                },
                "symbols": symbol_rows,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )


def snapshot_window(request: DiscoveryRequest) -> SnapshotWindow:
    """Return the inclusive window covered by all discovery splits."""

    starts: list[date] = []
    ends: list[date] = []
    base = request.base_config
    for key, target in (("start_date", starts), ("end_date", ends)):
        parsed = _parse_date(base.get(key))
        if parsed is not None:
            target.append(parsed)
    for split in request.splits.values():
        parsed_start = _parse_date(split.get("start_date"))
        parsed_end = _parse_date(split.get("end_date"))
        if parsed_start is not None:
            starts.append(parsed_start)
        if parsed_end is not None:
            ends.append(parsed_end)
    if not starts or not ends:
        raise ValueError("策略实验日期范围无效")
    start = min(starts)
    end = max(ends)
    if start > end:
        raise ValueError("策略实验开始日期不能晚于结束日期")
    return SnapshotWindow(start=start, end=end)


def _iter_ref_batches(
    refs: Iterable[StockRef],
    batch_size: int = SNAPSHOT_SYMBOL_BATCH_SIZE,
) -> Iterator[list[StockRef]]:
    if isinstance(batch_size, bool) or batch_size < 1:
        raise ValueError("策略实验快照批次大小必须大于 0")
    items = list(refs)
    for offset in range(0, len(items), batch_size):
        yield items[offset : offset + batch_size]


def _parse_date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _snapshot_warmup_bars() -> int:
    """Extra bars read before the window so indicators are fully warmed up.

    Uses ``recommended_bars``, not ``minimum_bars``: the latter only guarantees
    ``sufficient_data`` is true, not that a value is actually produced. Reading
    just 34 bars left ``rsrs_zscore`` (268-bar warmup) finite on ~11 of 243
    bars, so mining and the portfolio replay - which computes on full history -
    disagreed on the same indicator.
    """

    return max(
        1,
        max(
            FORMULA_REGISTRY.formula(formula_id).recommended_bars
            for formula_id in FORMULA_REGISTRY.all_formula_ids()
        ),
    )


def _condition_to_dict(condition: ScreenCondition) -> dict[str, object]:
    return {
        "left_value": condition.left_value,
        "operator": condition.operator,
        "right_value": condition.right_value,
        "right_constant": condition.right_constant,
    }


def _open_snapshot_memmap(
    path: Path,
    *,
    dtype: type | str | np.dtype[Any] | None,
    shape: tuple[int, ...],
) -> np.memmap:
    typed_open_memmap = cast(Callable[..., np.memmap], np.lib.format.open_memmap)
    return typed_open_memmap(
        path,
        mode="w+",
        dtype=dtype,
        shape=shape,
    )


def _condition_from_dict(value: object) -> ScreenCondition:
    if not isinstance(value, Mapping):
        raise ValueError("策略实验快照中的必选条件格式错误")
    right_value = value.get("right_value")
    right_constant = value.get("right_constant")
    return ScreenCondition(
        left_value=str(value.get("left_value") or ""),
        operator=str(value.get("operator") or "gt"),
        right_value=str(right_value) if right_value is not None else None,
        right_constant=float(right_constant) if right_constant is not None else None,
    )


def _required_rule(conditions: tuple[ScreenCondition, ...]) -> RuleGroup | None:
    return RuleGroup(conditions=conditions, combine_mode="all") if conditions else None


def _evaluate_required_mask(
    rule: RuleGroup | None,
    formula_results: Mapping[str, FormulaResult],
    length: int,
) -> np.ndarray:
    if rule is None:
        return np.ones(length, dtype=bool)
    try:
        mask = np.asarray(evaluate_rule_group(rule, formula_results, FORMULA_REGISTRY), dtype=bool)
    except (KeyError, ValueError):
        return np.zeros(length, dtype=bool)
    return mask if len(mask) == length else np.zeros(length, dtype=bool)


def _value_ids(config: PortfolioBacktestConfig) -> tuple[str, ...]:
    values = [] if config.single_security_mode else [config.ranking_value]
    for rule in (config.buy_rule, config.sell_rule):
        if rule is None:
            continue
        for condition in (*rule.required_conditions, *rule.conditions):
            values.append(condition.left_value)
            if condition.right_value is not None:
                values.append(condition.right_value)
    return tuple(dict.fromkeys(values))


def _request_refs(
    adapter: MarketDataAdapter,
    base: Mapping[str, object],
    *,
    universe: str,
    universe_file: str | None,
    instrument_types: tuple[InstrumentType, ...] | None,
    boards: tuple[InstrumentBoard, ...] | None,
) -> list[StockRef]:
    vipdoc_path = str(base.get("vipdoc_path") or "")
    if universe != "codes":
        return adapter.list_stock_refs(
            vipdoc_path,
            universe,
            universe_file,
            instrument_types=instrument_types,
            boards=boards,
        )

    raw_codes = base.get("codes")
    if not isinstance(raw_codes, (list, tuple)):
        raise ValueError("指定标的范围必须填写代码")
    refs: list[StockRef] = []
    seen: set[tuple[str, str]] = set()
    for raw in raw_codes:
        parsed = parse_universe_code(str(raw))
        if parsed is None:
            continue
        market, code = parsed
        key = (market, code)
        if key in seen:
            continue
        seen.add(key)
        ref = adapter.stock_ref(vipdoc_path, market, code)
        if instrument_types and ref.instrument_type not in instrument_types:
            continue
        if boards and ref.board not in boards:
            continue
        refs.append(ref)
    return refs


def _context_config_cache_key(config: PortfolioBacktestConfig) -> str:
    def rule_payload(rule: RuleGroup | None) -> dict[str, object] | None:
        if rule is None:
            return None
        return {
            "signals": list(rule.signals),
            "conditions": [_condition_to_dict(item) for item in rule.conditions],
            "combine_mode": rule.combine_mode,
            "minimum_matches": rule.minimum_matches,
            "required_conditions": [_condition_to_dict(item) for item in rule.required_conditions],
        }

    payload = {
        "start_date": config.start_date.isoformat() if config.start_date else None,
        "end_date": config.end_date.isoformat() if config.end_date else None,
        "atr_period": config.atr_period,
        "value_ids": list(_value_ids(config)),
        "signal_ids": list(_rule_signal_ids(config.buy_rule, config.sell_rule)),
        "buy_rule": rule_payload(config.buy_rule),
        "sell_rule": rule_payload(config.sell_rule),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _rule_signal_ids(*rules: RuleGroup | None) -> tuple[str, ...]:
    return tuple(dict.fromkeys(signal_id for rule in rules if rule for signal_id in rule.signals))


def _date_int(value: date) -> int:
    return int(value.strftime("%Y%m%d"))


def _string_tuple(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, (list, tuple)):
        return None
    result = tuple(str(item).strip() for item in value if str(item).strip())
    return result or None
