"""Compare winning trades against losing ones, one indicator at a time.

The statistic is a **difference of means**, never a ratio: indicator values go
negative, and a ratio of two negatives reads as "three times better" while the
underlying difference may point the other way.

The permutation moves the win/loss **labels** in contiguous blocks of positions.
Trades are not independent observations - every trade entered on the same day
lived through the same market - so shuffling them one by one would build a null
far too narrow and everything would come out significant. Positions are kept in
signal-day order, so a block of positions is a block of days.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

import numpy as np

from aquest.swing_research.scoring import benjamini_hochberg

from .models import (
    CONSTANT_NOTE,
    LOW_SAMPLE_NOTE,
    NO_SPLIT_NOTE,
    SHORT_SERIES_NOTE,
    AttributionFactor,
)

NO_VALUES_NOTE = "样本内没有任何可用取值"


def evaluate_factor(
    values: np.ndarray,
    wins: np.ndarray,
    *,
    value_id: str,
    display_name: str,
    orders: Sequence[np.ndarray],
    min_group_trades: int,
) -> AttributionFactor:
    """Compare one indicator's value between winning and losing trades."""

    indices = np.flatnonzero(np.isfinite(values))
    missing = int(values.size - indices.size)
    if indices.size == 0:
        return AttributionFactor(
            value_id=value_id,
            display_name=display_name,
            win_mean=None,
            loss_mean=None,
            difference=None,
            standardized=None,
            win_count=0,
            loss_count=0,
            missing=missing,
            note=NO_VALUES_NOTE,
        )

    subset = np.asarray(values[indices], dtype=float)
    labels = np.asarray(wins[indices], dtype=bool)
    win_values = subset[labels]
    loss_values = subset[~labels]
    win_count = int(win_values.size)
    loss_count = int(loss_values.size)
    if win_count == 0 or loss_count == 0:
        return AttributionFactor(
            value_id=value_id,
            display_name=display_name,
            win_mean=None,
            loss_mean=None,
            difference=None,
            standardized=None,
            win_count=win_count,
            loss_count=loss_count,
            missing=missing,
            note=NO_SPLIT_NOTE,
        )

    win_mean = float(win_values.mean())
    loss_mean = float(loss_values.mean())
    difference = win_mean - loss_mean
    pooled = _pooled_std(win_values, loss_values)
    standardized = float(difference / pooled) if pooled and pooled > 0 else None

    note: str | None = None
    p_value: float | None = None
    if win_count < min_group_trades or loss_count < min_group_trades:
        note = LOW_SAMPLE_NOTE
    elif pooled is None or pooled <= 0:
        # Both groups are constant. The difference can still be real - total
        # separation - but there is no spread to scale it by, and permuting the
        # labels cannot move a statistic that the values do not vary.
        note = CONSTANT_NOTE
    elif not orders:
        note = SHORT_SERIES_NOTE
    else:
        p_value = _permutation_p(subset, labels, indices, orders, difference, total=values.size)

    return AttributionFactor(
        value_id=value_id,
        display_name=display_name,
        win_mean=win_mean,
        loss_mean=loss_mean,
        difference=difference,
        standardized=standardized,
        win_count=win_count,
        loss_count=loss_count,
        missing=missing,
        p_value=p_value,
        note=note,
    )


def apply_fdr(
    factors: Sequence[AttributionFactor],
    *,
    alpha: float,
) -> list[AttributionFactor]:
    """Adjust every p-value at once, then pass judgement.

    Called once over the whole batch on purpose: calling it per indicator would
    quietly correct for a family of one and select noise at the stated rate.
    """

    adjusted = benjamini_hochberg([item.p_value for item in factors])
    result: list[AttributionFactor] = []
    for item, qvalue in zip(factors, adjusted):
        significant: bool | None = None
        # A factor carrying a note was never tested (too few trades, or no noise
        # floor), so "not significant" would be a claim we did not make.
        if item.p_value is not None and item.note is None:
            significant = bool((qvalue if qvalue is not None else item.p_value) <= alpha)
        result.append(replace(item, adjusted_p_value=qvalue, significant=significant))
    return result


def _pooled_std(win_values: np.ndarray, loss_values: np.ndarray) -> float:
    """Spread of the two groups combined; 0.0 when neither group varies."""

    first = int(win_values.size)
    second = int(loss_values.size)
    if first + second - 2 <= 0:
        return 0.0
    variance = 0.0
    if first > 1:
        variance += (first - 1) * float(win_values.var(ddof=1))
    if second > 1:
        variance += (second - 1) * float(loss_values.var(ddof=1))
    return float(np.sqrt(variance / (first + second - 2)))


def _permutation_p(
    subset: np.ndarray,
    labels: np.ndarray,
    indices: np.ndarray,
    orders: Sequence[np.ndarray],
    observed: float,
    total: int,
) -> float:
    """Block-permutation p-value for the observed difference, counted two-sided.

    ``orders`` permute the whole sample, so each one is projected onto this
    indicator's usable positions before it is applied. Dropping a few positions
    shortens some blocks but keeps them contiguous, which is the property the
    test relies on.

    计数必须双侧（数绝对值更极端的置换），不能"先看观测值符号再决定数哪一侧"。
    后者会把 p 压成双侧的一半：alpha=0.05 的检验实测误报率约 9%，而归因本来就是
    在没有预期方向的前提下问"赢输两组差在哪"，没有可事先注册的单侧方向。
    """

    member = np.zeros(total, dtype=bool)
    member[indices] = True
    # Orders carry whole-sample positions; the statistic works on the usable
    # subset, so each position has to be translated into a subset slot first.
    slot_of = np.full(total, -1, dtype=int)
    slot_of[indices] = np.arange(int(indices.size))
    reached = 0
    usable = 0
    for order in orders:
        projected = order[member[order]]
        permuted = labels[slot_of[projected]]
        win_values = subset[permuted]
        loss_values = subset[~permuted]
        if win_values.size == 0 or loss_values.size == 0:
            continue
        usable += 1
        statistic = float(win_values.mean()) - float(loss_values.mean())
        if abs(statistic) >= abs(observed):
            reached += 1
    if usable == 0:
        return 1.0
    return (1.0 + reached) / (1.0 + usable)


__all__ = ["NO_VALUES_NOTE", "apply_fdr", "evaluate_factor"]
