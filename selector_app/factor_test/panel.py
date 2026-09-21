"""Build the (values x stocks x days) panel a factor test scores.

Two passes over the market, for the same reason the swing study does it: the
panel needs one shared date axis and one row per stock, and the only honest
way to get either is to look before writing. The first pass reads nothing but
the calendar; the second reads with warm-up and computes.

The forward return is the reason the second pass reads past ``end_date``. A
label on the last study day needs ``horizon`` bars that live after it, so the
read window is extended while the study window is not - the extra days never
enter the axis, they only supply closes to divide by. Days whose future does
not exist end up with a NaN label and drop out of their own cross-section.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import date, timedelta

import numpy as np
import pandas as pd

from selector_app.formulas.registry import FORMULA_REGISTRY
from selector_app.market_data.adapter import BatchMarketDataAdapter
from selector_app.market_data.models import StockRef
from selector_app.swing_research.values import formula_groups

from .models import MAX_PANEL_BYTES, FactorTestConfig

#: Peak memory runs above the array size: the kernel caches pages, and scoring
#: copies each (stocks x days) slice into float64. Refusing before the run is
#: far kinder than being killed halfway through it.
PANEL_MEMORY_SAFETY = 2

ProgressCallback = Callable[[int, int], None] | None


def estimate_panel_bytes(stock_count: int, day_count: int, value_count: int) -> int:
    """Values are stored as float32, labels as float64."""

    return stock_count * day_count * (value_count * 4 + 8)


def _available_memory_bytes() -> int | None:
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        return None
    return None


def check_panel_budget(required: int) -> None:
    """Refuse a run that cannot fit, naming the wall it hit.

    Two walls. ``MAX_PANEL_BYTES`` is our own ceiling - a request past it is
    not dangerous, just slow enough that finishing was never the likely
    outcome. Available RAM is the one that bites: scoring copies each slice
    into float64 and the kernel caches the rest, so peak memory runs well above
    the panel itself.
    """

    gib = 1024**3
    if required > MAX_PANEL_BYTES:
        raise ValueError(
            f"因子面板需要 {required / gib:.1f} GB 内存，超出上限 {MAX_PANEL_BYTES / gib:.0f} GB；"
            "请缩短区间、减少股票数或减少指标数量后重试"
        )
    raw_ceiling = os.environ.get("FACTOR_PANEL_MAX_MEMORY_BYTES", "").strip()
    memory_ceiling = int(raw_ceiling) if raw_ceiling.isdigit() else 0
    if memory_ceiling == 0:
        available = _available_memory_bytes()
        memory_ceiling = available // PANEL_MEMORY_SAFETY if available is not None else 0
    if memory_ceiling > 0 and required > memory_ceiling:
        raise ValueError(
            f"因子面板需要 {required / gib:.1f} GB，但当前可用内存 "
            f"{memory_ceiling * PANEL_MEMORY_SAFETY / gib:.1f} GB 不足以安全容纳，"
            "运行中很可能把后端拖垮；请减少股票数或缩短区间"
            "（确要强行运行可设置 FACTOR_PANEL_MAX_MEMORY_BYTES）"
        )


class FactorPanel:
    """One shared date axis, one row per stock, one slice per value."""

    def __init__(
        self,
        dates: np.ndarray,
        rows: dict[str, int],
        values: np.ndarray,
        labels: np.ndarray,
    ) -> None:
        self.dates = dates
        self.rows = rows
        self.values = values
        self.labels = labels

    @property
    def day_count(self) -> int:
        return int(self.dates.size)

    @property
    def stock_count(self) -> int:
        return len(self.rows)


def _label_end(config: FactorTestConfig) -> date | None:
    """Read this far past the study window to have closes to divide by.

    Trading days are thinner than calendar days (weekends, holidays), so the
    margin is generous on purpose; reading a few days that turn out not to
    exist costs nothing, while reading too few silently drops the last weeks of
    the study from every label.
    """

    if config.end_date is None:
        return None
    return config.end_date + timedelta(days=config.horizon * 2 + 15)


def scan_calendar(
    adapter: BatchMarketDataAdapter,
    refs: list[StockRef],
    config: FactorTestConfig,
    progress: ProgressCallback = None,
) -> tuple[np.ndarray, dict[str, int]]:
    """First pass: the trading days in the window and the stocks that have bars."""

    seen_days: set[pd.Timestamp] = set()
    rows: dict[str, int] = {}
    total = len(refs)
    for offset in range(0, total, config.batch_size):
        batch = refs[offset : offset + config.batch_size]
        frame = adapter.read_many_stocks(
            batch, start_date=config.start_date, end_date=config.end_date
        )
        if not frame.empty:
            seen_days.update(pd.to_datetime(frame["date"]).unique())
            for key in pd.unique(frame["market"].astype(str) + frame["code"].astype(str)):
                if key not in rows:
                    rows[key] = len(rows)
        if progress is not None:
            progress(min(offset + len(batch), total), total)
    if not seen_days:
        return np.array([], dtype="datetime64[ns]"), {}
    return np.array(sorted(seen_days), dtype="datetime64[ns]"), rows


def build_panel(
    adapter: BatchMarketDataAdapter,
    refs: list[StockRef],
    config: FactorTestConfig,
    value_ids: tuple[str, ...],
    dates: np.ndarray,
    rows: dict[str, int],
    progress: ProgressCallback = None,
) -> tuple[FactorPanel, int]:
    """Second pass: compute indicators and forward returns into the panel."""

    groups = formula_groups(value_ids)
    # Every involved formula needs its own warm-up and the longest one wins: a
    # formula computed on too little history returns NaNs, which would quietly
    # shrink the cross-section instead of failing loudly.
    warmup = max(FORMULA_REGISTRY.formula(formula_id).recommended_bars for formula_id in groups)
    # Warm-up only makes sense against a window: without a start date the read
    # already begins at the first bar on record and the store rejects a warm-up
    # it cannot anchor.
    warmup = warmup if config.start_date else 0
    start_timestamp = pd.Timestamp(config.start_date) if config.start_date else None
    end_timestamp = pd.Timestamp(config.end_date) if config.end_date else None
    calendar_index = pd.DatetimeIndex(dates)
    values = np.full((len(value_ids), len(rows), dates.size), np.nan, dtype=np.float32)
    labels = np.full((len(rows), dates.size), np.nan, dtype=float)

    filled = 0
    total = len(refs)
    for offset in range(0, total, config.batch_size):
        batch = refs[offset : offset + config.batch_size]
        frame = adapter.read_many_stocks(
            batch,
            start_date=config.start_date,
            end_date=_label_end(config),
            warmup_bars=warmup,
        )
        if not frame.empty:
            keys = frame["market"].astype(str) + frame["code"].astype(str)
            for key, group in frame.groupby(keys, sort=True):
                row = rows.get(str(key))
                if row is None:
                    continue
                if _write_stock(
                    group,
                    config,
                    groups,
                    len(value_ids),
                    calendar_index,
                    start_timestamp,
                    end_timestamp,
                    values,
                    labels,
                    row,
                ):
                    filled += 1
        if progress is not None:
            progress(min(offset + len(batch), total), total)
    return FactorPanel(dates=dates, rows=rows, values=values, labels=labels), filled


def _write_stock(
    group: pd.DataFrame,
    config: FactorTestConfig,
    groups: dict[str, list[tuple[int, str]]],
    column_count: int,
    calendar_index: pd.DatetimeIndex,
    start_timestamp: pd.Timestamp | None,
    end_timestamp: pd.Timestamp | None,
    values: np.ndarray,
    labels: np.ndarray,
    row: int,
) -> bool:
    """Write one stock's indicator values and forward returns into the panel.

    A formula that cannot produce values for this stock (too few bars, or a
    denominator it refuses to divide by) leaves its columns NaN rather than
    aborting the row: the study scores each value independently, and one silent
    formula should not cost the stock its forward returns.
    """

    if len(group) < 2:
        return False
    timestamps = pd.DatetimeIndex(pd.to_datetime(group["date"]))
    keep = np.ones(len(group), dtype=bool)
    if start_timestamp is not None:
        keep &= timestamps >= start_timestamp
    if end_timestamp is not None:
        keep &= timestamps <= end_timestamp
    kept = np.nonzero(keep)[0]
    if kept.size == 0:
        return False

    computed = np.full((len(group), column_count), np.nan, dtype=float)
    for formula_id, items in groups.items():
        result = FORMULA_REGISTRY.formula(formula_id).calculate(group)
        for index, name in items:
            array = np.asarray(result.values.get(name), dtype=float)
            if array.shape == (len(group),):
                computed[:, index] = array
    forward = _forward_returns(group, config.horizon)

    positions = calendar_index.get_indexer(timestamps[keep])
    valid = positions >= 0
    if not valid.any():
        return False
    columns = positions[valid]
    source = kept[valid]
    values[:, row, columns] = computed[source, :].T
    labels[row, columns] = forward[source]
    return bool(np.isfinite(labels[row, columns]).any())


def _forward_returns(group: pd.DataFrame, horizon: int) -> np.ndarray:
    """Close-to-close return ``horizon`` trading days ahead; NaN past the end.

    Deliberately raw: no costs, no slippage, no halts, no limit-up/limit-down
    and no liquidity constraint. This measures whether the value ranks stocks,
    not whether the ranking can be traded - mixing the two is how a factor test
    ends up reporting a number nobody can earn.
    """

    closes = pd.to_numeric(group["close"], errors="coerce").to_numpy(dtype=float)
    result = np.full(closes.size, np.nan, dtype=float)
    if horizon >= closes.size:
        return result
    base = closes[:-horizon]
    ahead = closes[horizon:]
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(base > 1e-12, ahead / base, np.nan)
    result[:-horizon] = ratio - 1.0
    return result


__all__ = [
    "FactorPanel",
    "build_panel",
    "check_panel_budget",
    "estimate_panel_bytes",
    "scan_calendar",
]
