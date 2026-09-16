"""Interpretable contrastive rule mining over completed trade samples."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from selector_app.screening.models import ScreenCondition

DEFAULT_QUANTILES = (0.2, 0.4, 0.6, 0.8)
# Comparing two series whose spreads differ by orders of magnitude degenerates
# into "the varying series vs roughly zero". That is how five different feature
# pairs collapsed into the same `indicator_three.var1 < 0` condition, so pairs
# beyond this ratio are rejected. Scale-compatible pairs (ma5 vs ma20,
# rsi14 vs ma20) stay well inside the limit.
_MAX_SCALE_RATIO = 100.0
# Two conditions whose matching samples agree this closely are the same finding
# for the study; only the best-ranked one is reported.
_DUPLICATE_AGREEMENT = 0.995
_PRICE_SCALE_FEATURES = frozenset(
    {
        "technical.ma5",
        "technical.ma10",
        "technical.ma20",
        "technical.ma60",
        "technical.macd",
        "technical.macd_signal",
        "technical.macd_histogram",
        # Raw prices are absolute per stock; they are supported as user-written
        # condition operands but must never become a mined cross-stock threshold.
        "price.open",
        "price.high",
        "price.low",
        "price.close",
    }
)


def condition_signature(condition: ScreenCondition) -> str:
    """Stable textual key for one condition, including its constant operand."""

    right = (
        condition.right_value
        if condition.right_value is not None
        else f"={condition.right_constant}"
    )
    return f"{condition.left_value}|{condition.operator}|{right}"


def equivalent_condition_signatures(condition: ScreenCondition) -> frozenset[str]:
    """Every signature that means the same thing as one condition.

    ``a > b`` equals ``b < a`` and ``a 上穿 b`` equals ``b 下穿 a``. Mining must
    not re-propose a rule the base strategy already applies, because ANDing a
    condition with itself cannot change a single trade.
    """

    signature = condition_signature(condition)
    right = condition.right_value
    if right is None:
        return frozenset({signature})
    mirrored = {
        "gt": "lt",
        "lt": "gt",
        "gte": "lte",
        "lte": "gte",
        "cross_up": "cross_down",
        "cross_down": "cross_up",
    }.get(str(condition.operator))
    if mirrored is None:
        return frozenset({signature})
    return frozenset(
        {
            signature,
            condition_signature(
                ScreenCondition(
                    left_value=right, operator=mirrored, right_value=condition.left_value
                )
            ),
        }
    )


def _spread(values: np.ndarray) -> float:
    """Interquartile range; 0 means the comparison treats the series as constant."""

    low, high = np.percentile(values, [25, 75])
    spread = float(high) - float(low)
    return spread if np.isfinite(spread) and spread > 0.0 else 0.0


def _comparable_scale(left: np.ndarray, right: np.ndarray) -> bool:
    """Reject pairwise comparisons dominated by one operand's magnitude."""

    left_spread = _spread(left)
    right_spread = _spread(right)
    if left_spread == 0.0 or right_spread == 0.0:
        return False
    return max(left_spread, right_spread) / min(left_spread, right_spread) <= _MAX_SCALE_RATIO


def _masks_agree(
    left: np.ndarray,
    right: np.ndarray,
    *,
    threshold: float = _DUPLICATE_AGREEMENT,
) -> bool:
    """True when two candidate masks select effectively the same samples."""

    comparable = left | right
    if not comparable.any():
        return False
    return float(np.mean(left[comparable] == right[comparable])) >= threshold


@dataclass(frozen=True)
class MinedCondition:
    """A single numeric condition enriched for the requested trade outcome."""

    condition: ScreenCondition
    support_count: int
    support_ratio: float
    instrument_count: int
    outcome_rate: float
    baseline_rate: float
    lift: float


def mine_binary_conditions(
    features: pd.DataFrame,
    outcomes: Sequence[bool] | pd.Series,
    instruments: Sequence[str] | pd.Series,
    *,
    sample_groups: Sequence[str] | pd.Series | None = None,
    quantiles: Sequence[float] = DEFAULT_QUANTILES,
    min_support: int = 20,
    min_instruments: int = 5,
    limit: int = 50,
    excluded: frozenset[str] = frozenset(),
) -> tuple[MinedCondition, ...]:
    """Find readable thresholds whose matching samples are enriched for an outcome.

    Outcomes should indicate profitable trades for buy-filter discovery and
    losing trades for sell-trigger discovery. Candidate thresholds are derived
    only from the samples passed by the caller, allowing chronological
    train/validation/holdout separation.

    ``excluded`` carries ``equivalent_condition_signatures`` of the base rule so
    the miner never proposes a condition the strategy already applies.

    Sample keys are factorized to integer codes once so the per-candidate work
    stays vectorized. ``pd.factorize`` hands out codes in order of first
    appearance, which is the same order the earlier dict-based implementation
    iterated, keeping results reproducible.
    """

    if min_support < 1:
        raise ValueError("最少支持样本数必须大于 0")
    if min_instruments < 1:
        raise ValueError("最少覆盖股票数必须大于 0")
    if limit < 1:
        raise ValueError("候选条件上限必须大于 0")
    resolved_quantiles = tuple(sorted(set(float(value) for value in quantiles)))
    if not resolved_quantiles or any(not 0.0 < value < 1.0 for value in resolved_quantiles):
        raise ValueError("候选分位数必须在 0 和 1 之间")

    target = np.asarray(outcomes, dtype=bool)
    symbols = np.asarray(instruments, dtype=object)
    groups = (
        np.asarray(sample_groups, dtype=object)
        if sample_groups is not None
        else np.arange(len(target), dtype=object)
    )
    if target.ndim != 1 or symbols.ndim != 1:
        raise ValueError("交易样本必须是一维数据")
    if groups.ndim != 1 or len(features) != len(target) or len(target) != len(symbols):
        raise ValueError("样本数量不一致")
    if len(target) != len(groups):
        raise ValueError("样本数量不一致")
    if len(target) == 0 or features.empty:
        return ()

    valid_symbols = pd.notna(symbols) & pd.notna(groups)
    feature_ids = tuple(str(column) for column in features.columns)
    arrays = {
        feature_id: pd.to_numeric(features[feature_id], errors="coerce").to_numpy(dtype=float)
        for feature_id in feature_ids
    }
    finite = {feature_id: np.isfinite(values) for feature_id, values in arrays.items()}
    comparable = {
        feature_id: _has_two_distinct(arrays[feature_id], finite[feature_id])
        for feature_id in feature_ids
    }
    target_float = target.astype(np.float64)
    group_codes = pd.factorize(groups)[0]
    instrument_codes = pd.factorize(symbols)[0]

    candidates: list[tuple[MinedCondition, np.ndarray]] = []
    seen_rules: set[tuple[str, str, str | None, float | None]] = set()

    def record(candidate: MinedCondition | None, valid: np.ndarray, mask: np.ndarray) -> None:
        if candidate is None:
            return
        full_mask = np.zeros(len(target), dtype=bool)
        full_mask[valid] = mask
        candidates.append((candidate, full_mask))

    for feature_id in feature_ids:
        if feature_id in _PRICE_SCALE_FEATURES:
            continue
        values = arrays[feature_id]
        valid = finite[feature_id] & valid_symbols
        if int(valid.sum()) < min_support:
            continue
        valid_values = values[valid]
        if len(np.unique(valid_values)) < 2:
            continue

        valid_target = target_float[valid]
        valid_instruments = instrument_codes[valid]
        valid_groups = group_codes[valid]
        baseline_rate = _grouped_outcome_rate(valid_target, valid_groups)
        if not 0.0 < baseline_rate < 1.0:
            continue
        thresholds = np.unique(np.quantile(valid_values, resolved_quantiles))

        for threshold in thresholds:
            for operator, condition_mask in (
                ("gte", valid_values >= threshold),
                ("lte", valid_values <= threshold),
            ):
                condition = ScreenCondition(
                    left_value=feature_id,
                    operator=operator,
                    right_constant=float(threshold),
                )
                key = _condition_key(condition)
                if key in seen_rules or condition_signature(condition) in excluded:
                    continue
                seen_rules.add(key)
                candidate = _candidate_for_mask(
                    condition,
                    condition_mask,
                    valid_target,
                    valid_instruments,
                    valid_groups,
                    min_support=min_support,
                    min_instruments=min_instruments,
                    baseline_rate=baseline_rate,
                )
                record(candidate, valid, condition_mask)

    for left_index, left_id in enumerate(feature_ids):
        if not comparable[left_id]:
            continue
        left_values = arrays[left_id]
        left_finite = finite[left_id]
        for right_id in feature_ids[left_index + 1 :]:
            if not comparable[right_id]:
                continue
            right_values = arrays[right_id]
            valid = left_finite & finite[right_id] & valid_symbols
            if int(valid.sum()) < min_support:
                continue
            valid_left = left_values[valid]
            valid_right = right_values[valid]
            if np.all(valid_left == valid_right):
                continue
            # One side nearly constant next to a wide-ranging side means the rule
            # is really "the wide side vs ~0"; those collapse into duplicates.
            if not _comparable_scale(valid_left, valid_right):
                continue
            valid_target = target_float[valid]
            valid_instruments = instrument_codes[valid]
            valid_groups = group_codes[valid]
            baseline_rate = _grouped_outcome_rate(valid_target, valid_groups)
            if not 0.0 < baseline_rate < 1.0:
                continue
            for operator, condition_mask in (
                ("gt", valid_left > valid_right),
                ("lt", valid_left < valid_right),
            ):
                condition = ScreenCondition(
                    left_value=left_id,
                    operator=operator,
                    right_value=right_id,
                )
                key = _condition_key(condition)
                if key in seen_rules or condition_signature(condition) in excluded:
                    continue
                seen_rules.add(key)
                candidate = _candidate_for_mask(
                    condition,
                    condition_mask,
                    valid_target,
                    valid_instruments,
                    valid_groups,
                    min_support=min_support,
                    min_instruments=min_instruments,
                    baseline_rate=baseline_rate,
                )
                record(candidate, valid, condition_mask)

    ranked = sorted(
        candidates,
        key=lambda item: (
            -item[0].lift,
            -item[0].outcome_rate,
            -item[0].support_count,
            -item[0].instrument_count,
            item[0].condition.left_value,
            str(item[0].condition.operator),
            float(item[0].condition.right_constant or 0.0),
        ),
    )
    selected: list[MinedCondition] = []
    kept_masks: list[np.ndarray] = []
    for candidate, mask in ranked:
        if any(_masks_agree(mask, kept) for kept in kept_masks):
            continue
        selected.append(candidate)
        kept_masks.append(mask)
        if len(selected) == limit:
            break
    return tuple(selected)


def _has_two_distinct(values: np.ndarray, finite_mask: np.ndarray) -> bool:
    finite_values = values[finite_mask]
    return finite_values.size >= 2 and np.unique(finite_values).size >= 2


def _candidate_for_mask(
    condition: ScreenCondition,
    condition_mask: np.ndarray,
    outcomes: np.ndarray,
    instrument_codes: np.ndarray,
    group_codes: np.ndarray,
    *,
    min_support: int,
    min_instruments: int,
    baseline_rate: float,
) -> MinedCondition | None:
    order = np.flatnonzero(condition_mask)
    if order.size == 0:
        # Never true: it cannot produce a single trade.
        return None
    if order.size == outcomes.size:
        # Always true: an AND with it cannot change any trade.
        return None
    # A group contributes at most one sample: its first matching row.
    # np.unique(..., return_index=True) reports that first occurrence, and
    # sorting restores the ascending order the previous dict loop produced.
    _, first_positions = np.unique(group_codes[order], return_index=True)
    matched_indices = np.sort(order[first_positions])
    support_count = int(matched_indices.size)
    if support_count < min_support:
        return None
    outcome_rate = float(np.mean(outcomes[matched_indices]))
    if outcome_rate <= baseline_rate:
        return None
    instrument_count = int(np.unique(instrument_codes[matched_indices]).size)
    if instrument_count < min_instruments:
        return None
    group_count = int(np.unique(group_codes).size)
    return MinedCondition(
        condition=condition,
        support_count=support_count,
        support_ratio=float(support_count / group_count),
        instrument_count=instrument_count,
        outcome_rate=outcome_rate,
        baseline_rate=baseline_rate,
        lift=float(outcome_rate / baseline_rate),
    )


def _grouped_outcome_rate(outcomes: np.ndarray, group_codes: np.ndarray) -> float:
    """Equal-weight mean of per-group outcome rates.

    Encoded with ``bincount`` so no Python loop runs per candidate. Group codes
    come from ``pd.factorize``, i.e. first-appearance order, so the summation
    order matches the previous dict-based implementation.
    """

    if group_codes.size == 0:
        return float("nan")
    size = int(group_codes.max()) + 1
    counts = np.bincount(group_codes, minlength=size)
    wins = np.bincount(group_codes, weights=outcomes, minlength=size)
    present = counts > 0
    return float(np.mean(wins[present] / counts[present]))


def _condition_key(
    condition: ScreenCondition,
) -> tuple[str, str, str | None, float | None]:
    return (
        condition.left_value,
        str(condition.operator),
        condition.right_value,
        condition.right_constant,
    )
