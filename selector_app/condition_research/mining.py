"""Interpretable contrastive rule mining over completed trade samples."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from selector_app.screening.models import ScreenCondition

DEFAULT_QUANTILES = (0.2, 0.4, 0.6, 0.8)
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
) -> tuple[MinedCondition, ...]:
    """Find readable thresholds whose matching samples are enriched for an outcome.

    Outcomes should indicate profitable trades for buy-filter discovery and
    losing trades for sell-trigger discovery. Candidate thresholds are derived
    only from the samples passed by the caller, allowing chronological
    train/validation/holdout separation.

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

    candidates: list[MinedCondition] = []
    seen_rules: set[tuple[str, str, str | None, float | None]] = set()

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
                if key in seen_rules:
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
                if candidate is not None:
                    candidates.append(candidate)

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
                if key in seen_rules:
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
                if candidate is not None:
                    candidates.append(candidate)

    candidates.sort(
        key=lambda candidate: (
            -candidate.lift,
            -candidate.outcome_rate,
            -candidate.support_count,
            -candidate.instrument_count,
            candidate.condition.left_value,
            str(candidate.condition.operator),
            float(candidate.condition.right_constant or 0.0),
        )
    )
    return tuple(candidates[:limit])


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
