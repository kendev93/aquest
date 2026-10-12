"""One instrument's indicator readings over its own history.

这里只做两件事：把指标值按日期列出来，并且算清楚「最后一个值处在自身历史的
什么位置」。**分位是跟这只票自己的历史比**，不是跟全市场比——单只票没有横截面
可比性，把一个数写成 P80 却不说是谁的分位，就是误导。
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from aquest.backtest.service import PreparedBacktest
from aquest.strategy_discovery.catalog import default_indicator_catalog

from .models import InstrumentValues, ValueTimeline, thin_indices

_PERCENTILE_NOTE = (
    "自身历史分位：当前值在这只票自身历史取值中的位置（≤ 当前值的比例），不是全市场排名"
)


def window_indices(
    frame: pd.DataFrame,
    start_date: date | None,
    end_date: date | None,
) -> np.ndarray:
    """Inclusive date window, mirroring the backtest service's slicing."""

    days = frame["date"].to_numpy(dtype="datetime64[D]")
    mask = np.ones(days.shape, dtype=bool)
    if start_date is not None:
        mask &= days >= np.datetime64(start_date, "D")
    if end_date is not None:
        mask &= days <= np.datetime64(end_date, "D")
    return np.flatnonzero(mask)


def _series(values: np.ndarray) -> list[float | None]:
    return [float(item) if np.isfinite(item) else None for item in values]


def build_instrument_values(
    prepared: PreparedBacktest,
    value_ids: tuple[str, ...],
    *,
    start_date: date | None,
    end_date: date | None,
) -> InstrumentValues:
    """Project requested indicator outputs onto the requested date window."""

    frame = prepared.frame
    indices = window_indices(frame, start_date, end_date)
    if len(indices) == 0:
        raise ValueError("指定日期范围内没有可用日线数据")
    days = frame["date"].dt.strftime("%Y-%m-%d").to_numpy()
    window_days = [str(days[index]) for index in indices]
    keep = thin_indices(len(indices))
    thinned = len(keep) < len(indices)
    out_dates = [window_days[position] for position in keep]
    catalog = default_indicator_catalog()
    features = prepared.feature_values
    series: list[ValueTimeline] = []
    notes: list[str] = []
    for value_id in value_ids:
        column = features[value_id] if value_id in features.columns else None
        if column is None:
            notes.append(f"指标 {catalog.display_name(value_id)} 没有可用输出")
            continue
        raw = np.asarray(column.to_numpy(dtype=float)[indices], dtype=float)
        finite = np.isfinite(raw)
        last_value = float(raw[finite][-1]) if bool(finite.any()) else None
        percentile = (
            float(np.mean(raw[finite] <= last_value) * 100.0) if last_value is not None else None
        )
        full_values = _series(raw)
        thinned_values = full_values if not thinned else [full_values[i] for i in keep]
        series.append(
            ValueTimeline(
                value_id=value_id,
                label=catalog.display_name(value_id),
                dates=tuple(out_dates),
                values=tuple(thinned_values),
                last_value=last_value,
                percentile=percentile,
                note=None if last_value is not None else "该区间内没有有效取值",
            )
        )
    return InstrumentValues(
        market=str(prepared.config.market),
        code=prepared.config.code,
        name=prepared.ref.name,
        start_date=out_dates[0] if out_dates else None,
        end_date=out_dates[-1] if out_dates else None,
        bars=len(indices),
        series=tuple(series),
        thinned=thinned,
        notes=(_PERCENTILE_NOTE, *notes),
    )
