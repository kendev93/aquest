"""Bounded, scale-aware pair features for swing-label research."""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd

from .scoring import tie_average_rank

TRAIN_RATIO = 0.6
VALIDATION_RATIO = 0.2
PAIR_COMPONENT_LIMIT = 8
PAIR_HOLDOUT_LIMIT_PER_LABEL = 5
PAIR_OPERATIONS = ("rank_gap", "rank_ratio", "cross_up", "cross_down")
PAIR_OPERATION_NAMES: Mapping[str, str] = {
    "rank_gap": "分位差",
    "rank_ratio": "分位比",
    "cross_up": "分位上穿",
    "cross_down": "分位下穿",
}
RANK_RATIO_EPSILON = 0.02


def split_trading_days(
    dates: np.ndarray,
    horizon: int,
) -> tuple[dict[str, slice], dict[str, object]]:
    """Split ordered trading dates 60/20/20 and purge labels crossing boundaries."""

    day_count = int(len(dates))
    if day_count < 3:
        empty = {phase: slice(0, 0) for phase in ("train", "validation", "holdout")}
        return empty, {
            "train_ratio": TRAIN_RATIO,
            "validation_ratio": VALIDATION_RATIO,
            "holdout_ratio": 1.0 - TRAIN_RATIO - VALIDATION_RATIO,
            "purge_trading_days": horizon,
            "train": _window_metadata(dates, 0, 0, 0),
            "validation": _window_metadata(dates, 0, 0, 0),
            "holdout": _window_metadata(dates, 0, 0, 0),
        }

    train_cut = max(1, min(day_count - 2, int(day_count * TRAIN_RATIO)))
    validation_cut = max(
        train_cut + 1,
        min(day_count - 1, int(day_count * (TRAIN_RATIO + VALIDATION_RATIO))),
    )
    train_stop = max(0, train_cut - horizon)
    validation_stop = max(train_cut, validation_cut - horizon)
    holdout_stop = max(validation_cut, day_count - horizon)
    slices = {
        "train": slice(0, train_stop),
        "validation": slice(train_cut, validation_stop),
        "holdout": slice(validation_cut, holdout_stop),
    }
    return slices, {
        "train_ratio": TRAIN_RATIO,
        "validation_ratio": VALIDATION_RATIO,
        "holdout_ratio": 1.0 - TRAIN_RATIO - VALIDATION_RATIO,
        "purge_trading_days": horizon,
        "train": _window_metadata(dates, 0, train_stop, train_cut - train_stop),
        "validation": _window_metadata(
            dates, train_cut, validation_stop, validation_cut - validation_stop
        ),
        "holdout": _window_metadata(dates, validation_cut, holdout_stop, day_count - holdout_stop),
    }


def _window_metadata(
    dates: np.ndarray,
    start: int,
    stop: int,
    purged_days: int,
) -> dict[str, object]:
    if stop <= start:
        return {"start": None, "end": None, "days": 0, "purged_days": purged_days}
    return {
        "start": pd.Timestamp(dates[start]).strftime("%Y-%m-%d"),
        "end": pd.Timestamp(dates[stop - 1]).strftime("%Y-%m-%d"),
        "days": stop - start,
        "purged_days": purged_days,
    }


def percentile_rank_panel(values: np.ndarray) -> np.ndarray:
    """Map each date's valid cross-section to comparable ranks in (0, 1)."""

    matrix = np.asarray(values, dtype=float)
    mask = np.isfinite(matrix)
    counts = mask.sum(axis=0).astype(float)
    ranks = tie_average_rank(matrix, mask)
    denominator = counts[None, :] + 1.0
    return np.asarray(np.where(mask, ranks / denominator, np.nan), dtype=np.float32)


def combine_rank_panels(
    left: np.ndarray,
    right: np.ndarray,
    operation: str,
) -> np.ndarray:
    """Build one causal composite from two daily percentile-rank panels."""

    a = np.asarray(left, dtype=float)
    b = np.asarray(right, dtype=float)
    if a.shape != b.shape:
        raise ValueError("组合指标两侧面板的形状必须相同")
    valid = np.isfinite(a) & np.isfinite(b)
    if operation == "rank_gap":
        return np.asarray(np.where(valid, a - b, np.nan), dtype=np.float32)
    if operation == "rank_ratio":
        ratio = (a + RANK_RATIO_EPSILON) / (b + RANK_RATIO_EPSILON)
        return np.asarray(np.where(valid, ratio, np.nan), dtype=np.float32)
    if operation not in {"cross_up", "cross_down"}:
        raise ValueError(f"未知的组合指标运算: {operation}")

    result = np.full(a.shape, np.nan, dtype=np.float32)
    if a.shape[1] < 2:
        return result
    gap = a - b
    previous_valid = valid[:, :-1] & valid[:, 1:]
    if operation == "cross_up":
        crossed = (gap[:, :-1] <= 0) & (gap[:, 1:] > 0)
    else:
        crossed = (gap[:, :-1] >= 0) & (gap[:, 1:] < 0)
    result[:, 1:] = np.where(previous_valid, crossed.astype(float), np.nan)
    return result


def fast_rank_ic(
    values: np.ndarray,
    labels: np.ndarray,
    *,
    min_cross_section: int,
) -> dict[str, object]:
    """Compute daily cross-sectional rank IC without the date-by-date null matrix."""

    matrix = np.asarray(values, dtype=float)
    target = np.asarray(labels, dtype=float)
    if matrix.shape != target.shape:
        raise ValueError("指标面板和波段标签的形状必须相同")
    mask = np.isfinite(matrix) & np.isfinite(target)
    counts = mask.sum(axis=0).astype(float)
    usable = counts >= max(2, min_cross_section)
    if matrix.size == 0 or not usable.any():
        return _empty_rank_ic()

    value_ranks = tie_average_rank(matrix, mask)
    label_ranks = tie_average_rank(target, mask)
    divisor = np.maximum(counts, 1.0)
    value_centered = np.where(
        mask, value_ranks - value_ranks.sum(axis=0, keepdims=True) / divisor, 0.0
    )
    label_centered = np.where(
        mask, label_ranks - label_ranks.sum(axis=0, keepdims=True) / divisor, 0.0
    )
    covariance = (value_centered * label_centered).sum(axis=0)
    value_variance = (value_centered**2).sum(axis=0)
    label_variance = (label_centered**2).sum(axis=0)
    denominator = np.sqrt(value_variance * label_variance)
    with np.errstate(divide="ignore", invalid="ignore"):
        daily_ic = covariance / denominator
    daily_ic[~usable | (denominator <= 0)] = np.nan
    finite = np.isfinite(daily_ic)
    if not finite.any():
        return _empty_rank_ic()

    signed_mean = float(np.mean(daily_ic[finite]))
    deviation = float(np.std(daily_ic[finite], ddof=1)) if int(finite.sum()) > 1 else 0.0
    return {
        "ic_mean": abs(signed_mean),
        "signed_ic_mean": signed_mean,
        "icir": signed_mean / deviation if deviation > 1e-12 else None,
        "ic_days": int(finite.sum()),
        "order": "high" if signed_mean >= 0 else "low",
    }


def _empty_rank_ic() -> dict[str, object]:
    return {
        "ic_mean": None,
        "signed_ic_mean": None,
        "icir": None,
        "ic_days": 0,
        "order": None,
    }


__all__ = [
    "PAIR_COMPONENT_LIMIT",
    "PAIR_HOLDOUT_LIMIT_PER_LABEL",
    "PAIR_OPERATIONS",
    "PAIR_OPERATION_NAMES",
    "TRAIN_RATIO",
    "VALIDATION_RATIO",
    "combine_rank_panels",
    "fast_rank_ic",
    "percentile_rank_panel",
    "split_trading_days",
]
