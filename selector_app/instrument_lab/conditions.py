"""Re-test mined conditions on one instrument.

条件是在**全市场样本**上挖出来、并且通过了置换检验的。把同一条条件放到某一只
票上，它往往根本不成立——这正是检验台要回答的问题，所以这里的口径必须和因子检
验一致：

* 证据是**差值**（命中组前瞻收益均值 − 全样本均值），不是比值：收益能取负，两
  个负数相除会读成「差三倍」。
* 显著性走**连续日期块置换**：一天一天地打乱会把相邻交易日的依赖留在标签里却
  假装它没被破坏；整块搬动才是在问「这条条件到底有没有对上收益」。
* 一次检验多条条件时用 **BH-FDR** 校正：检验得越多，最好看的那个 p 值越可能是
  噪声挑出来的。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np

from selector_app.backtest.service import PreparedBacktest
from selector_app.formulas.registry import FORMULA_REGISTRY, FormulaRegistry
from selector_app.screening.engine import evaluate_rule_group
from selector_app.screening.models import ScreenCondition
from selector_app.screening.rules import RuleGroup
from selector_app.strategy_discovery.catalog import default_indicator_catalog
from selector_app.swing_research.scoring import benjamini_hochberg, block_permutations

from .models import (
    DEFAULT_ALPHA,
    DEFAULT_BLOCK_SIZE,
    DEFAULT_PERMUTATIONS,
    DEFAULT_SEED,
    LOW_SAMPLE_NOTE,
    MAX_HIT_DATES,
    MAX_LIBRARY_CONDITIONS,
    MIN_HIT_SAMPLES,
    MIN_SAMPLE_DAYS,
    NO_HIT_NOTE,
    SHORT_SERIES_NOTE,
    ConditionCheck,
    ConditionCheckReport,
    LibraryCondition,
)
from .values import window_indices

_OPERATOR_SYMBOLS = {
    "gt": ">",
    "gte": "≥",
    "lt": "<",
    "lte": "≤",
    "eq": "=",
    "neq": "≠",
    "cross_up": "上穿",
    "cross_down": "下穿",
}
_SIDE_LABELS = {"buy": "买入侧", "sell": "卖出侧"}
_LIBRARY_HISTORY_LIMIT = 10


@dataclass(frozen=True)
class ConditionCheckInput:
    key: str
    label: str
    condition: ScreenCondition


def _format_constant(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:g}"


def condition_label(condition: ScreenCondition) -> str:
    catalog = default_indicator_catalog()
    left = catalog.display_name(condition.left_value)
    symbol = _OPERATOR_SYMBOLS.get(str(condition.operator), str(condition.operator))
    right = (
        catalog.display_name(condition.right_value)
        if condition.right_value
        else _format_constant(condition.right_constant)
    )
    return f"{left} {symbol} {right}"


def condition_key(condition: ScreenCondition) -> str:
    return "|".join(
        (
            condition.left_value,
            str(condition.operator),
            condition.right_value or "",
            "" if condition.right_constant is None else f"{condition.right_constant:g}",
        )
    )


def condition_from_mapping(payload: Mapping[str, object]) -> ScreenCondition:
    """Rebuild a stored condition, rejecting anything malformed."""

    left_value = payload.get("left_value")
    operator = payload.get("operator")
    right_value = payload.get("right_value")
    right_constant = payload.get("right_constant")
    if not isinstance(left_value, str) or not left_value.strip():
        raise ValueError("条件缺少左值")
    if not isinstance(operator, str):
        raise ValueError("条件缺少运算符")
    if right_value is not None and not isinstance(right_value, str):
        raise ValueError("条件右值指标必须是字符串")
    if right_constant is not None and (
        isinstance(right_constant, bool) or not isinstance(right_constant, (int, float))
    ):
        raise ValueError("条件右值常数必须是数字")
    return ScreenCondition(
        left_value=left_value.strip(),
        operator=operator,
        right_value=right_value.strip() if isinstance(right_value, str) else None,
        right_constant=None if right_constant is None else float(right_constant),
    )


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if np.isfinite(result) else None


def significant_conditions(
    store: object,
    *,
    limit: int = MAX_LIBRARY_CONDITIONS,
) -> list[LibraryCondition]:
    """Collect conditions that passed their permutation test, newest first.

    只收 ``significant is True`` 的候选：挖出来的条件里绝大多数是噪声，把没通过
    检验的也摆上来等于把挑选工作推回给用户。
    """

    summaries = _list_history(store)[:_LIBRARY_HISTORY_LIMIT]
    history_ids = [str(item.get("history_id") or "") for item in summaries]
    history_ids = [item for item in history_ids if item]
    records = _read_histories(store, history_ids)
    best: dict[str, LibraryCondition] = {}
    for record in records:
        result = _mapping(record.get("result"))
        candidates = result.get("candidates")
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            payload = _mapping(candidate)
            mining = _mapping(payload.get("mining"))
            if mining.get("significant") is not True:
                continue
            try:
                condition = condition_from_mapping(_mapping(payload.get("condition")))
            except ValueError:
                continue
            key = condition_key(condition)
            lift = _number(mining.get("lift"))
            previous = best.get(key)
            if previous is not None and (previous.lift or -np.inf) >= (lift or -np.inf):
                continue
            side = payload.get("side")
            best[key] = LibraryCondition(
                key=key,
                label=condition_label(condition),
                side=str(side) if isinstance(side, str) else None,
                condition={
                    "left_value": condition.left_value,
                    "operator": str(condition.operator),
                    "right_value": condition.right_value,
                    "right_constant": condition.right_constant,
                },
                lift=lift,
                pvalue=_number(mining.get("permutation_pvalue")),
                support_count=_as_int(mining.get("support_count")),
                instrument_count=_as_int(mining.get("instrument_count")),
                history_id=str(record.get("history_id") or "") or None,
                created_at=_text(record.get("finished_at")),
                scanned_stocks=_as_int(_mapping(record.get("summary")).get("scanned_stocks")),
            )
    ordered = sorted(best.values(), key=lambda item: (item.lift is None, -(item.lift or 0.0)))
    return ordered[: max(1, limit)]


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _list_history(store: object) -> list[dict[str, object]]:
    method = getattr(store, "list_backtest_history", None)
    if method is None:
        return []
    return list(method("condition_research"))


def _read_histories(store: object, history_ids: Iterable[str]) -> list[dict[str, object]]:
    method = getattr(store, "read_backtest_histories", None)
    if method is None:
        return []
    return list(method(history_ids))


def _forward_returns(close: np.ndarray, horizon: int) -> np.ndarray:
    """Close-to-close return ``h`` days ahead; the tail has no label."""

    result = np.full(close.shape, np.nan, dtype=float)
    usable = len(close) - horizon
    if usable <= 0:
        return result
    with np.errstate(divide="ignore", invalid="ignore"):
        result[:usable] = close[horizon:] / close[:usable] - 1.0
    return np.where(np.isfinite(result), result, np.nan)


def check_conditions(
    prepared: PreparedBacktest,
    checks: Sequence[ConditionCheckInput],
    *,
    horizon: int,
    start_date: date | None = None,
    end_date: date | None = None,
    permutations: int = DEFAULT_PERMUTATIONS,
    block_size: int = DEFAULT_BLOCK_SIZE,
    seed: int = DEFAULT_SEED,
    alpha: float = DEFAULT_ALPHA,
    registry: FormulaRegistry | None = None,
) -> ConditionCheckReport:
    """Evaluate each condition on one instrument and test the evidence."""

    frame = prepared.frame
    indices = window_indices(frame, start_date, end_date)
    if len(indices) == 0:
        raise ValueError("指定日期范围内没有可用日线数据")
    days = frame["date"].dt.strftime("%Y-%m-%d").to_numpy()
    window_days = days[indices]
    close = frame["close"].to_numpy(dtype=float)
    forward = _forward_returns(close, horizon)
    window_forward = forward[indices]
    base_positions = np.flatnonzero(np.isfinite(window_forward))
    if len(base_positions) == 0:
        raise ValueError("区间内没有可用的前瞻收益，请延长日期范围")
    base_forward = window_forward[base_positions]
    base_mean = float(np.mean(base_forward))
    day_count = int(len(base_positions))
    selected_registry = registry or FORMULA_REGISTRY
    orders = (
        block_permutations(day_count, block_size=block_size, count=permutations, seed=seed)
        if day_count >= MIN_SAMPLE_DAYS
        else []
    )
    items: list[ConditionCheck] = []
    for check in checks:
        mask = evaluate_rule_group(
            RuleGroup(conditions=(check.condition,), combine_mode="all"),
            prepared.formula_results,
            selected_registry,
            custom=prepared.custom_formula,
        )
        hit_flags = np.asarray(mask[indices], dtype=bool)[base_positions]
        hit_count = int(hit_flags.sum())
        hit_dates = tuple(
            str(item) for item in window_days[base_positions][hit_flags][-MAX_HIT_DATES:]
        )
        if hit_count == 0:
            items.append(
                ConditionCheck(
                    key=check.key,
                    label=check.label,
                    hit_count=0,
                    sample_days=day_count,
                    hit_mean_return=None,
                    base_mean_return=base_mean,
                    difference=None,
                    p_value=None,
                    adjusted_p_value=None,
                    significant=None,
                    hit_dates=hit_dates,
                    note=NO_HIT_NOTE,
                )
            )
            continue
        hit_mean = float(np.mean(base_forward[hit_flags]))
        difference = hit_mean - base_mean
        p_value: float | None = None
        if orders:
            observed = difference
            reached = 0
            for order in orders:
                permuted = base_forward[order]
                statistic = float(np.mean(permuted[hit_flags])) - base_mean
                if (observed >= 0 and statistic >= observed) or (
                    observed < 0 and statistic <= observed
                ):
                    reached += 1
            p_value = (1 + reached) / (1 + len(orders))
        note: str | None = None
        if hit_count < MIN_HIT_SAMPLES:
            note = LOW_SAMPLE_NOTE
        elif not orders:
            note = SHORT_SERIES_NOTE
        items.append(
            ConditionCheck(
                key=check.key,
                label=check.label,
                hit_count=hit_count,
                sample_days=day_count,
                hit_mean_return=hit_mean,
                base_mean_return=base_mean,
                difference=difference,
                p_value=p_value,
                adjusted_p_value=None,
                significant=None,
                hit_dates=hit_dates,
                note=note,
            )
        )
    adjusted = benjamini_hochberg([item.p_value for item in items])
    final: list[ConditionCheck] = []
    for item, adjusted_p in zip(items, adjusted):
        significant: bool | None = None
        if item.p_value is not None and item.hit_count >= MIN_HIT_SAMPLES:
            significant = bool((adjusted_p if adjusted_p is not None else item.p_value) <= alpha)
        final.append(
            ConditionCheck(
                key=item.key,
                label=item.label,
                hit_count=item.hit_count,
                sample_days=item.sample_days,
                hit_mean_return=item.hit_mean_return,
                base_mean_return=item.base_mean_return,
                difference=item.difference,
                p_value=item.p_value,
                adjusted_p_value=adjusted_p,
                significant=significant,
                hit_dates=item.hit_dates,
                note=item.note,
            )
        )
    notes: list[str] = []
    if not orders:
        notes.append(SHORT_SERIES_NOTE)
    if len(final) > 1:
        notes.append(
            f"同时检验 {len(final)} 条条件，p 值已按 BH 校正（控制错误发现率），"
            f"显著性门槛 α={alpha:g}"
        )
    return ConditionCheckReport(
        market=str(prepared.config.market),
        code=prepared.config.code,
        name=prepared.ref.name,
        horizon=horizon,
        start_date=str(window_days[0]) if len(window_days) else None,
        end_date=str(window_days[-1]) if len(window_days) else None,
        sample_days=day_count,
        permutations=len(orders),
        block_size=block_size,
        items=tuple(final),
        notes=tuple(notes),
    )
