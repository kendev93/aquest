"""Formula selection engine; no web or upstream protocol imports leak here."""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import numpy as np
import pandas as pd

from aquest.formulas.common import validate_market_data
from aquest.formulas.custom import (
    ParsedFormula,
    custom_output_id,
    evaluate_custom_formula,
    parse_formula,
)
from aquest.formulas.registry import FORMULA_REGISTRY, FormulaRegistry
from aquest.formulas.types import FormulaResult
from aquest.indicators.cache import IndicatorCache
from aquest.market_data.adapter import DuckDbMarketDataAdapter, MarketDataAdapter
from aquest.market_data.adjustment import CorporateAction, mark_adjusted
from aquest.market_data.day_format import classify_board, classify_instrument
from aquest.market_data.models import StockRef
from aquest.market_data.store import DuckDbMarketDataStore

from .models import ScanConfig, ScanReport, ScreenCondition, ScreenMatch
from .rules import RuleGroup

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int, "ScanOutcome"], None]


def combine_matches(matches: list[bool], mode: str, minimum_matches: int | None) -> bool:
    if not matches:
        return False
    if mode == "all":
        return all(matches)
    if mode == "any":
        return any(matches)
    if mode == "at_least" and minimum_matches is not None:
        return sum(matches) >= minimum_matches
    return False


def combine_masks(
    masks: Sequence[np.ndarray], mode: str, minimum_matches: int | None
) -> np.ndarray:
    """Combine full-history boolean masks with the same semantics as screening."""

    if not masks:
        return np.zeros(0, dtype=bool)
    length = len(masks[0])
    if any(len(mask) != length for mask in masks):
        raise ValueError("交易规则条件的历史长度不一致")
    stacked = np.vstack([np.asarray(mask, dtype=bool) for mask in masks])
    if mode == "all":
        return np.asarray(np.all(stacked, axis=0), dtype=bool)
    if mode == "any":
        return np.asarray(np.any(stacked, axis=0), dtype=bool)
    if mode == "at_least" and minimum_matches is not None:
        return np.asarray(np.sum(stacked, axis=0) >= minimum_matches, dtype=bool)
    raise ValueError(f"不支持的条件组合方式: {mode}")


@dataclass(frozen=True)
class ScanOutcome:
    result: ScreenMatch | None = None
    skipped_reason: str | None = None
    error_reason: str | None = None


def _value_series(value_id: str, formula_results: Mapping[str, FormulaResult]) -> np.ndarray:
    formula_id, separator, value_name = value_id.partition(".")
    result = formula_results.get(formula_id)
    if not separator or result is None:
        raise ValueError(f"未知指标输出: {value_id}")
    if value_name in result.values:
        return np.asarray(result.values[value_name], dtype=float)
    if formula_id == "custom":
        for name, values in result.values.items():
            if custom_output_id(name) == value_id:
                return np.asarray(values, dtype=float)
    raise ValueError(f"未知指标输出: {value_id}")


def _condition_mask(
    condition: ScreenCondition, formula_results: Mapping[str, FormulaResult]
) -> np.ndarray:
    left = _value_series(condition.left_value, formula_results)
    if condition.right_value is not None:
        right = _value_series(condition.right_value, formula_results)
    else:
        assert condition.right_constant is not None
        right = np.full(len(left), float(condition.right_constant), dtype=float)
    if len(left) != len(right):
        raise ValueError("条件两侧指标的历史长度不一致")

    valid = np.isfinite(left) & np.isfinite(right)
    operator = condition.operator
    if operator == "gt":
        return np.asarray(valid & (left > right), dtype=bool)
    if operator == "gte":
        return np.asarray(valid & (left >= right), dtype=bool)
    if operator == "lt":
        return np.asarray(valid & (left < right), dtype=bool)
    if operator == "lte":
        return np.asarray(valid & (left <= right), dtype=bool)
    equality = np.asarray(np.isclose(left, right, rtol=1e-9, atol=1e-12), dtype=bool)
    if operator == "eq":
        return np.asarray(valid & equality, dtype=bool)
    if operator == "neq":
        return np.asarray(valid & ~equality, dtype=bool)

    result = np.zeros(len(left), dtype=bool)
    if len(left) < 2:
        return result
    previous_valid = valid[:-1] & valid[1:]
    if operator == "cross_up":
        result[1:] = previous_valid & (left[:-1] <= right[:-1]) & (left[1:] > right[1:])
    elif operator == "cross_down":
        result[1:] = previous_valid & (left[:-1] >= right[:-1]) & (left[1:] < right[1:])
    else:
        raise ValueError(f"不支持的条件运算符: {operator}")
    return result


def evaluate_rule_group(
    rule: RuleGroup,
    formula_results: Mapping[str, FormulaResult],
    registry: FormulaRegistry,
    *,
    custom: bool = False,
) -> np.ndarray:
    """Evaluate signal and numeric conditions into one causal history mask."""

    required_masks = [
        _condition_mask(condition, formula_results) for condition in rule.required_conditions
    ]
    masks: list[np.ndarray] = []
    for signal_id in rule.signals:
        if custom:
            result = formula_results["custom"]
            signal_name = signal_id
        else:
            definition = registry.signal(signal_id)
            result = formula_results[definition.formula_id]
            signal_name = signal_id.split(".", 1)[1]
        try:
            masks.append(np.asarray(result.signals[signal_name], dtype=bool))
        except KeyError as exc:
            raise ValueError(f"找不到信号输出: {signal_id}") from exc
    masks.extend(_condition_mask(condition, formula_results) for condition in rule.conditions)
    if not required_masks:
        return combine_masks(masks, rule.combine_mode, rule.minimum_matches)
    required = combine_masks(required_masks, "all", None)
    if not masks:
        return required
    generated = combine_masks(masks, rule.combine_mode, rule.minimum_matches)
    return np.asarray(required & generated, dtype=bool)


def _condition_formula_results(
    frame: pd.DataFrame,
    config: ScanConfig,
    registry: FormulaRegistry,
    parsed_formula: ParsedFormula | None,
    *,
    market: str = "",
    code: str = "",
    indicator_cache: IndicatorCache | None = None,
) -> dict[str, FormulaResult]:
    if parsed_formula is not None:
        return {
            "custom": (
                indicator_cache.get_or_compute_custom(
                    frame,
                    parsed_formula,
                    config.formula_parameters,
                    market=market,
                    code=code,
                )["custom"]
                if indicator_cache is not None
                else evaluate_custom_formula(
                    parsed_formula,
                    frame.copy(deep=True),
                    config.formula_parameters,
                )
            )
        }
    value_ids = tuple(
        value_id
        for condition in config.conditions
        for value_id in (condition.left_value, condition.right_value)
        if value_id is not None
    )
    if config.ranking_value is not None:
        value_ids += (config.ranking_value,)
    value_ids = tuple(dict.fromkeys(value_ids))
    definitions = registry.formulas_for_values(value_ids)
    if indicator_cache is not None:
        return indicator_cache.get_or_compute(
            frame,
            (definition.id for definition in definitions),
            market=market,
            code=code,
        )
    return {
        definition.id: definition.calculate(frame.copy(deep=True)) for definition in definitions
    }


def _date_to_int(value: object) -> int:
    timestamp = pd.Timestamp(value)
    return int(timestamp.strftime("%Y%m%d"))


def _ranking_sort_key(value: float | None, order: str) -> tuple[int, float]:
    """Sort finite rank values first, retaining scan order for ties and nulls."""

    if value is None or not np.isfinite(value):
        return (1, 0.0)
    return (0, float(value) if order == "asc" else -float(value))


def _visible_indicator_values(
    formula_results: Mapping[str, FormulaResult],
    registry: FormulaRegistry,
) -> dict[str, float | None]:
    """Return only the stable, user-facing outputs in a scan result.

    Built-in formulas keep their intermediate arrays in ``FormulaResult`` so
    conditions and future formula changes can reuse them.  They are not part
    of the result display contract, though: the registry's selectable output
    list is the single source of truth for what the UI and exports expose.
    Custom formulas have no built-in whitelist, so all named custom outputs
    remain visible.
    """

    visible: dict[str, float | None] = {}
    for formula_id, result in formula_results.items():
        if formula_id == "custom":
            names = tuple(result.last_indicator_values)
        else:
            names = registry.formula(formula_id).selectable_value_names
        for name in names:
            if name in result.last_indicator_values:
                visible[f"{formula_id}.{name}"] = result.last_indicator_values[name]
    return visible


def _evaluate_frame(
    ref: StockRef,
    frame: pd.DataFrame,
    config: ScanConfig,
    registry: FormulaRegistry,
    parsed_formula: ParsedFormula | None = None,
    indicator_cache: IndicatorCache | None = None,
) -> ScanOutcome:
    if frame.empty:
        return ScanOutcome(skipped_reason="数据为空")
    validate_market_data(frame)

    if config.conditions:
        formula_results = _condition_formula_results(
            frame,
            config,
            registry,
            parsed_formula,
            market=ref.market,
            code=ref.code,
            indicator_cache=indicator_cache,
        )
        required_formula_ids = {
            value_id.partition(".")[0]
            for condition in config.conditions
            for value_id in (condition.left_value, condition.right_value)
            if value_id is not None
        }
        if any(
            not formula_results[formula_id].sufficient_data for formula_id in required_formula_ids
        ):
            return ScanOutcome(skipped_reason="数据不足")
        condition_masks = [
            _condition_mask(condition, formula_results) for condition in config.conditions
        ]
        matched_ids = tuple(
            f"condition_{index + 1}"
            for index, mask in enumerate(condition_masks)
            if len(mask) > 0 and bool(mask[-1])
        )
        matched_bools = [len(mask) > 0 and bool(mask[-1]) for mask in condition_masks]
    elif parsed_formula is not None:
        formula_result = (
            indicator_cache.get_or_compute_custom(
                frame,
                parsed_formula,
                config.formula_parameters,
                market=ref.market,
                code=ref.code,
            )["custom"]
            if indicator_cache is not None
            else evaluate_custom_formula(
                parsed_formula,
                frame.copy(deep=True),
                config.formula_parameters,
            )
        )
        if any(
            signal_id not in formula_result.last_signal_state
            for signal_id in config.selected_signals
        ):
            raise ValueError("选择的自定义输出不属于当前公式")
        if not formula_result.sufficient_data:
            return ScanOutcome(skipped_reason="数据不足")
        matched_ids = tuple(
            signal_id
            for signal_id in config.selected_signals
            if formula_result.last_signal_state[signal_id]
        )
        formula_results = {"custom": formula_result}
        matched_bools = [signal_id in matched_ids for signal_id in config.selected_signals]
    else:
        definitions_by_id = {
            definition.id: definition
            for definition in registry.formulas_for_signals(config.selected_signals)
        }
        if config.ranking_value is not None:
            definitions_by_id.update(
                {
                    definition.id: definition
                    for definition in registry.formulas_for_values((config.ranking_value,))
                }
            )
        definitions = tuple(definitions_by_id.values())
        results = (
            indicator_cache.get_or_compute(
                frame,
                (definition.id for definition in definitions),
                market=ref.market,
                code=ref.code,
            )
            if indicator_cache is not None
            else {
                definition.id: definition.calculate(frame.copy(deep=True))
                for definition in definitions
            }
        )
        signal_formula_ids = {
            registry.signal(signal_id).formula_id for signal_id in config.selected_signals
        }
        if any(not results[formula_id].sufficient_data for formula_id in signal_formula_ids):
            return ScanOutcome(skipped_reason="数据不足")
        matched_ids = tuple(
            signal_id
            for signal_id in config.selected_signals
            if results[registry.signal(signal_id).formula_id].last_signal_state[
                signal_id.split(".", 1)[1]
            ]
        )
        formula_results = results
        matched_bools = [signal_id in matched_ids for signal_id in config.selected_signals]
    if not combine_matches(matched_bools, config.combine_mode, config.minimum_matches):
        return ScanOutcome()

    indicator_values = _visible_indicator_values(formula_results, registry)
    if config.ranking_value is not None:
        indicator_values.setdefault(config.ranking_value, None)
    last_close = float(frame["close"].iloc[-1])
    return ScanOutcome(
        result=ScreenMatch(
            market=ref.market,
            code=ref.code,
            signal_date=_date_to_int(frame["date"].iloc[-1]),
            last_close=last_close,
            matched_signals=matched_ids,
            match_count=len(matched_ids),
            indicator_values=indicator_values,
            instrument_type=getattr(ref, "instrument_type", "stock"),
            board=getattr(ref, "board", "main"),
            name=getattr(ref, "name", None),
        )
    )


class ScreenEngine:
    """Scan local daily files and combine selected formula signal states."""

    def __init__(
        self,
        adapter: MarketDataAdapter | None = None,
        registry: FormulaRegistry | None = None,
        indicator_cache: IndicatorCache | None = None,
    ) -> None:
        self._adapter = adapter or DuckDbMarketDataAdapter(DuckDbMarketDataStore())
        self._registry = registry or FORMULA_REGISTRY
        self._indicator_cache = indicator_cache

    def scan(
        self,
        config: ScanConfig,
        progress_callback: ProgressCallback | None = None,
        *,
        abort_check: Callable[[], bool] | None = None,
    ) -> ScanReport:
        parsed_formula = parse_formula(config.formula_text) if config.formula_text else None
        refs = self._list_refs(config)
        if not refs:
            if progress_callback is not None:
                progress_callback(0, 0, ScanOutcome())
            return ScanReport(
                0,
                0,
                0,
                0,
                0,
                (),
                {},
                {},
                ranking_value=config.ranking_value,
                rank_order=config.rank_order,
            )

        frames = self._batch_frames(refs)
        outcomes = self._scan_refs(
            refs, config, progress_callback, parsed_formula, frames, abort_check=abort_check
        )
        results = tuple(outcome.result for outcome in outcomes if outcome.result is not None)
        ranking_value = config.ranking_value
        if ranking_value is not None:
            results = tuple(
                sorted(
                    results,
                    key=lambda result: _ranking_sort_key(
                        result.indicator_values.get(ranking_value), config.rank_order
                    ),
                )
            )
        failures = Counter(
            outcome.error_reason for outcome in outcomes if outcome.error_reason is not None
        )
        skips = Counter(
            outcome.skipped_reason for outcome in outcomes if outcome.skipped_reason is not None
        )
        return ScanReport(
            total_candidates=len(refs),
            total_scanned=len(outcomes),
            total_signals=sum(result.match_count for result in results),
            errors=sum(failures.values()),
            skipped=sum(skips.values()),
            results=results,
            failure_reasons=dict(failures),
            skip_reasons=dict(skips),
            ranking_value=config.ranking_value,
            rank_order=config.rank_order,
        )

    def _scan_refs(
        self,
        refs: list[StockRef],
        config: ScanConfig,
        progress_callback: ProgressCallback | None,
        parsed_formula: ParsedFormula | None,
        frames: Mapping[tuple[str, str], pd.DataFrame] | None = None,
        *,
        abort_check: Callable[[], bool] | None = None,
    ) -> list[ScanOutcome]:
        def evaluate(ref: StockRef) -> ScanOutcome:
            if abort_check is not None and abort_check():
                return ScanOutcome(skipped_reason="扫描已取消")
            if frames is None:
                return self._scan_one(ref, config, parsed_formula)
            frame = frames.get((ref.market, ref.code), pd.DataFrame())
            return _evaluate_frame(
                ref,
                frame,
                config,
                self._registry,
                parsed_formula,
                self._indicator_cache,
            )

        if config.workers <= 1:
            outcomes: list[ScanOutcome] = []
            for current, ref in enumerate(refs, start=1):
                if abort_check is not None and abort_check():
                    break
                try:
                    outcome = evaluate(ref)
                except Exception as exc:  # noqa: BLE001 - one bad stock must not abort scan
                    logger.warning("扫描 %s (%s) 失败，已跳过", ref.code, ref.path, exc_info=True)
                    outcome = ScanOutcome(error_reason=str(exc) or type(exc).__name__)
                outcomes.append(outcome)
                if progress_callback is not None:
                    progress_callback(current, len(refs), outcome)
            return outcomes

        # Each worker opens its own DuckDB connection through the adapter. This
        # avoids sharing a connection across threads/processes and keeps custom
        # injected adapters usable in tests and embedding applications.
        executor_type = ThreadPoolExecutor
        outcomes_by_index: dict[int, ScanOutcome] = {}
        executor = executor_type(max_workers=config.workers)
        try:
            futures = {executor.submit(evaluate, ref): index for index, ref in enumerate(refs)}
            completed = 0
            for future in as_completed(futures):
                if abort_check is not None and abort_check():
                    for pending in futures:
                        pending.cancel()
                    break
                index = futures[future]
                try:
                    outcome = future.result()
                except Exception as exc:  # noqa: BLE001 - per-stock isolation
                    logger.warning(
                        "扫描 %s 失败，继续处理其余股票", refs[index].code, exc_info=True
                    )
                    outcome = ScanOutcome(error_reason=str(exc) or type(exc).__name__)
                outcomes_by_index[index] = outcome
                completed += 1
                if progress_callback is not None:
                    progress_callback(completed, len(refs), outcome)
        finally:
            # Running stock evaluations finish cooperatively at the next task
            # boundary; queued stock work is cancelled and never starts.
            executor.shutdown(wait=True, cancel_futures=True)
        return [outcomes_by_index[index] for index in sorted(outcomes_by_index)]

    def _list_refs(self, config: ScanConfig) -> list[StockRef]:
        if not config.instrument_types and not config.boards:
            return self._adapter.list_stock_refs(
                config.vipdoc_path,
                config.universe,
                config.universe_file,
            )
        try:
            refs = self._adapter.list_stock_refs(
                config.vipdoc_path,
                config.universe,
                config.universe_file,
                instrument_types=config.instrument_types or None,
                boards=config.boards or None,
            )
        except TypeError as exc:
            if "unexpected keyword" not in str(exc):
                raise
            refs = self._adapter.list_stock_refs(
                config.vipdoc_path,
                config.universe,
                config.universe_file,
            )
        return [ref for ref in refs if _matches_scope(ref, config)]

    def _batch_frames(self, refs: list[StockRef]) -> Mapping[tuple[str, str], pd.DataFrame] | None:
        reader = getattr(self._adapter, "read_many_stocks", None)
        if reader is None:
            return None
        combined = reader(refs)
        frames: dict[tuple[str, str], pd.DataFrame] = {}
        if combined.empty:
            return frames
        for key, group in combined.groupby(["market", "code"], sort=False):
            frames[(str(key[0]), str(key[1]))] = group.reset_index(drop=True)
        # 批量路径不走 _scan_one，指标复权要在这里单独做。事件一次批量取，避免
        # 全市场规模下变成逐标的查询。
        loader = getattr(self._adapter, "load_corporate_actions", None)
        if callable(loader):
            actions = loader(frames.keys())
            for key, frame in frames.items():
                frames[key] = mark_adjusted(frame, actions.get(key, ()))
        return frames

    def _corporate_actions(self, ref: StockRef) -> tuple[CorporateAction, ...]:
        """该标的的分红送转事件；适配器不支持时退化成"无事件"。

        选股只做指标判断、没有成交环节，所以这里整段行情都可以按除权校正后的价格
        计算。适配器没有这个能力时（测试用的 FakeAdapter 之类）退化为 raw 口径。
        """

        cache: dict[tuple[str, str], tuple[CorporateAction, ...]] | None = getattr(
            self, "_corporate_action_cache", None
        )
        if cache is None:
            cache = {}
            self._corporate_action_cache = cache
        key = (str(ref.market).upper(), ref.code.strip())
        if key not in cache:
            reader = getattr(self._adapter, "read_corporate_actions", None)
            cache[key] = tuple(reader(ref)) if callable(reader) else ()
        return cache[key]

    def _scan_one(
        self,
        ref: StockRef,
        config: ScanConfig,
        parsed_formula: ParsedFormula | None = None,
    ) -> ScanOutcome:
        try:
            frame = mark_adjusted(self._adapter.read_stock(ref), self._corporate_actions(ref))
            return _evaluate_frame(
                ref,
                frame,
                config,
                self._registry,
                parsed_formula,
                self._indicator_cache,
            )
        except Exception as exc:  # noqa: BLE001 - one bad stock must not abort scan
            logger.warning("扫描 %s (%s) 失败，已跳过", ref.code, ref.path, exc_info=True)
            return ScanOutcome(error_reason=str(exc) or type(exc).__name__)


def _matches_scope(ref: StockRef, config: ScanConfig) -> bool:
    board = classify_board(ref.market, ref.code) or getattr(ref, "board", "main")
    instrument_type = classify_instrument(ref.market, ref.code) or getattr(
        ref, "instrument_type", "stock"
    )
    return (not config.instrument_types or instrument_type in config.instrument_types) and (
        not config.boards or board in config.boards
    )
