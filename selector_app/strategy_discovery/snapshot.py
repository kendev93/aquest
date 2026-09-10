"""Read-only, memory-mapped market data for repeated strategy evaluations."""

from __future__ import annotations

import gc
import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

import numpy as np
import pandas as pd

from selector_app.formulas.registry import FORMULA_REGISTRY
from selector_app.formulas.types import FormulaResult
from selector_app.indicators.cache import IndicatorCache, frame_fingerprint
from selector_app.market_data.adapter import DuckDbMarketDataAdapter
from selector_app.market_data.day_format import classify_board
from selector_app.market_data.models import InstrumentBoard, InstrumentType, MarketCode
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.portfolio_backtest.models import PortfolioBacktestConfig
from selector_app.portfolio_backtest.service import (
    PortfolioBacktestService,
    _atr_series,
    _StockSeries,
)
from selector_app.screening.engine import evaluate_rule_group
from selector_app.screening.models import ScreenCondition
from selector_app.screening.rules import RuleGroup

from .catalog import default_indicator_catalog
from .models import DiscoveryRequest

_BAR_ARRAY_NAMES = ("open", "high", "low", "close", "atr")
_SNAPSHOT_VERSION = 1


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


class DiscoverySnapshot:
    """Open a compact snapshot without copying its arrays into each worker."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("version") != _SNAPSHOT_VERSION:
            raise ValueError("策略实验行情快照版本不受支持")
        self.total_candidates = int(manifest["total_candidates"])
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

    def close(self) -> None:
        """Release mapped files before the temporary directory is removed."""

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

    def contexts_for_config(self, config: PortfolioBacktestConfig) -> list[_StockSeries]:
        """Build cheap candidate-specific contexts from the shared arrays."""

        if config.formula_text:
            raise ValueError("共享行情快照不支持自定义公式")
        if config.buy_rule is None:
            raise ValueError("买入规则不能为空")

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
            offset = item.offset
            limit = offset + item.length
            dates = self._dates[offset:limit]
            left = int(np.searchsorted(dates, start_index, side="left")) if start_index else 0
            right = (
                int(np.searchsorted(dates, end_index, side="right")) if end_index else item.length
            )
            if left >= right:
                continue

            formula_results = self._formula_results(
                formula_ids,
                signal_ids,
                value_ids,
                offset,
                limit,
            )
            buy_mask = self._candidate_mask(
                config.buy_rule,
                self._required_buy_conditions,
                self._required_buy[offset:limit],
                formula_results,
            )
            sell_mask = (
                self._candidate_mask(
                    config.sell_rule,
                    self._required_sell_conditions,
                    self._required_sell[offset:limit],
                    formula_results,
                )
                if config.sell_rule is not None
                else np.zeros(item.length, dtype=bool)
            )
            window_dates = np.asarray(dates[left:right])
            context_start = offset + left
            context_end = offset + right
            context_values = {
                value_id: np.asarray(
                    self._values[self._value_indexes[value_id], context_start:context_end],
                    dtype=float,
                )
                for value_id in value_ids
            }
            context_signals = {
                signal_id: np.asarray(
                    self._signals[self._signal_indexes[signal_id], context_start:context_end],
                    dtype=bool,
                )
                for signal_id in signal_ids
            }
            context_dates = np.asarray(window_dates, dtype=np.int64)
            contexts.append(
                _StockSeries(
                    market=item.market,
                    code=item.code,
                    instrument_type=item.instrument_type,
                    board=item.board,
                    dates=context_dates,
                    opens=np.asarray(self._bars[0, context_start:context_end], dtype=float),
                    highs=np.asarray(self._bars[1, context_start:context_end], dtype=float),
                    lows=np.asarray(self._bars[2, context_start:context_end], dtype=float),
                    closes=np.asarray(self._bars[3, context_start:context_end], dtype=float),
                    atrs=np.asarray(self._bars[4, context_start:context_end], dtype=float),
                    buy_mask=np.asarray(buy_mask[left:right], dtype=bool),
                    sell_mask=np.asarray(sell_mask[left:right], dtype=bool),
                    signals=context_signals,
                    values=context_values,
                    date_to_index={int(value): index for index, value in enumerate(context_dates)},
                    data_fingerprint=item.data_fingerprint,
                )
            )
        return contexts

    def _candidate_mask(
        self,
        rule: RuleGroup,
        required_conditions: tuple[ScreenCondition, ...],
        required_mask: np.ndarray,
        formula_results: Mapping[str, _ArrayFormulaResult],
    ) -> np.ndarray:
        cast_results = cast(Mapping[str, FormulaResult], formula_results)
        if not required_conditions:
            return evaluate_rule_group(rule, cast_results, FORMULA_REGISTRY)
        required_set = set(required_conditions)
        if not required_set.issubset(set(rule.conditions)):
            raise ValueError("候选规则缺少用户设置的必选条件")
        auto_conditions = tuple(
            condition for condition in rule.conditions if condition not in required_set
        )
        if not rule.signals and not auto_conditions:
            return np.asarray(required_mask, dtype=bool)
        auto_rule = RuleGroup(
            signals=rule.signals,
            conditions=auto_conditions,
            combine_mode="all",
        )
        auto_mask = np.asarray(
            evaluate_rule_group(
                auto_rule,
                cast_results,
                FORMULA_REGISTRY,
            ),
            dtype=bool,
        )
        combined = np.asarray(required_mask, dtype=bool) & auto_mask
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


@contextmanager
def build_discovery_snapshot(
    store: DuckDbMarketDataStore,
    request: DiscoveryRequest,
    *,
    progress_callback: Callable[[int, int], None] | None = None,
    abort_check: Callable[[], bool] | None = None,
) -> Iterator[DiscoverySnapshot]:
    """Materialize one run snapshot and keep it alive for all workers."""

    with TemporaryDirectory(prefix="indicator-lab-discovery-") as temporary_root:
        root = Path(temporary_root)
        _write_snapshot(
            root,
            store,
            request,
            progress_callback=progress_callback,
            abort_check=abort_check,
        )
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
    progress_callback: Callable[[int, int], None] | None = None,
    abort_check: Callable[[], bool] | None = None,
) -> None:
    base = request.base_config
    instrument_types = _string_tuple(base.get("instrument_types"))
    boards = _string_tuple(base.get("boards"))
    snapshot_store = DuckDbMarketDataStore(store.database_path, read_only=True)
    adapter = DuckDbMarketDataAdapter(snapshot_store)
    refs = adapter.list_stock_refs(
        str(base.get("vipdoc_path") or ""),
        str(base.get("universe") or "all"),
        str(base["universe_file"]) if base.get("universe_file") else None,
        instrument_types=cast(tuple[InstrumentType, ...] | None, instrument_types),
        boards=cast(tuple[InstrumentBoard, ...] | None, boards),
    )
    combined = adapter.read_many_stocks(refs)
    if combined.empty:
        raise ValueError("本地 DuckDB 没有可用行情，请先在“本地行情”页面导入日线数据")
    if abort_check is not None and abort_check():
        raise RuntimeError("策略实验已取消")

    catalog = default_indicator_catalog()
    value_ids = tuple(catalog.rankable_ids)
    signal_ids = tuple(catalog.signal_ids)
    formula_ids = tuple(FORMULA_REGISTRY.all_formula_ids())
    total_rows = len(combined)
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
    memory_cache = IndicatorCache()
    symbol_rows: list[dict[str, object]] = []
    cursor = 0
    if progress_callback is not None:
        progress_callback(0, len(refs))
    for group_index, (key, group) in enumerate(
        combined.groupby(["market", "code"], sort=False), start=1
    ):
        if abort_check is not None and abort_check():
            raise RuntimeError("策略实验已取消")
        market = cast(MarketCode, str(key[0]))
        code = str(key[1])
        prepared = PortfolioBacktestService._prepare_frame(group)
        length = len(prepared)
        end = cursor + length
        result_by_formula = memory_cache.get_or_compute(
            prepared,
            formula_ids,
            market=market,
            code=code,
            persistent_read=False,
            persist=False,
        )
        dates[cursor:end] = np.asarray(
            pd.to_datetime(prepared["date"]).dt.strftime("%Y%m%d"), dtype=np.int64
        )
        bars[0, cursor:end] = prepared["open"].to_numpy(dtype=float, copy=False)
        bars[1, cursor:end] = prepared["high"].to_numpy(dtype=float, copy=False)
        bars[2, cursor:end] = prepared["low"].to_numpy(dtype=float, copy=False)
        bars[3, cursor:end] = prepared["close"].to_numpy(dtype=float, copy=False)
        bars[4, cursor:end] = _atr_series(prepared, 14)
        required_buy[cursor:end] = _evaluate_required_mask(
            required_buy_rule, result_by_formula, length
        )
        required_sell[cursor:end] = _evaluate_required_mask(
            required_sell_rule, result_by_formula, length
        )
        for index, value_id in enumerate(value_ids):
            formula_id, _, value_name = value_id.partition(".")
            value = result_by_formula[formula_id].values.get(value_name)
            value_array = np.asarray(value, dtype=float) if value is not None else None
            values[index, cursor:end] = (
                value_array if value_array is not None and len(value_array) == length else np.nan
            )
        for index, signal_id in enumerate(signal_ids):
            formula_id = FORMULA_REGISTRY.signal(signal_id).formula_id
            signal_name = signal_id.split(".", 1)[1]
            signal = result_by_formula[formula_id].signals.get(signal_name)
            signal_array = np.asarray(signal, dtype=bool) if signal is not None else None
            signals[index, cursor:end] = (
                signal_array if signal_array is not None and len(signal_array) == length else False
            )
        ref = ref_by_key[(market, code)]
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
        if progress_callback is not None and (group_index % 25 == 0 or group_index == len(refs)):
            progress_callback(min(group_index, len(refs)), len(refs))
    if progress_callback is not None and symbol_rows:
        progress_callback(len(refs), len(refs))
    for mapped in (bars, dates, values, signals, required_buy, required_sell):
        mapped.flush()
    del bars, dates, values, signals, required_buy, required_sell, combined
    gc.collect()
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
                "symbols": symbol_rows,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
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
    mapped = np.lib.format.open_memmap(
        path,
        mode="w+",
        dtype=dtype,
        shape=shape,
    )
    return mapped


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
    values = [config.ranking_value]
    for rule in (config.buy_rule, config.sell_rule):
        if rule is None:
            continue
        for condition in rule.conditions:
            values.append(condition.left_value)
            if condition.right_value is not None:
                values.append(condition.right_value)
    return tuple(dict.fromkeys(values))


def _rule_signal_ids(*rules: RuleGroup | None) -> tuple[str, ...]:
    return tuple(dict.fromkeys(signal_id for rule in rules if rule for signal_id in rule.signals))


def _date_int(value: date) -> int:
    return int(value.strftime("%Y%m%d"))


def _string_tuple(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, (list, tuple)):
        return None
    result = tuple(str(item).strip() for item in value if str(item).strip())
    return result or None
