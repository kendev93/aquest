"""Read indicator values on the day each entry signal fired.

Attribution only needs a handful of points per trade - the signal day of every
entry - but indicators are whole-series formulas, so the read still has to be
per stock and the formula still has to see the bars before the window. Hence the
shape borrowed from :mod:`selector_app.factor_test.panel`: read in batches, run
each formula once over the whole slice, then pick the points out.

A missing point is left NaN and counted, never filled with zero: "RSI was 0 on
the day" and "there is no RSI for that day" are different claims, and filling
the second with the first manufactures a finding out of a gap.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from selector_app.formulas.registry import FORMULA_REGISTRY
from selector_app.market_data.adapter import BatchMarketDataAdapter
from selector_app.market_data.models import StockRef
from selector_app.swing_research.values import formula_groups

from .trades import RoundTrip

ProgressCallback = Callable[[int, int], None] | None


@dataclass(frozen=True)
class SignalValues:
    """One row per trade, one column per value; NaN where the day had nothing."""

    values: np.ndarray
    holding_days: np.ndarray
    missing_stocks: tuple[str, ...]


def collect_signal_values(
    adapter: BatchMarketDataAdapter,
    refs: Sequence[StockRef],
    trips: Sequence[RoundTrip],
    value_ids: tuple[str, ...],
    *,
    batch_size: int = 100,
    progress: ProgressCallback = None,
) -> SignalValues:
    """Compute every indicator on every entry signal day."""

    groups = formula_groups(value_ids)
    # Warm-up uses recommended_bars, not minimum_bars: the minimum lets a
    # formula return values, most of which are NaN, which would quietly shrink
    # the sample instead of announcing itself.
    warmup = max(FORMULA_REGISTRY.formula(formula_id).recommended_bars for formula_id in groups)
    start_date = min(trip.signal_date for trip in trips)
    end_date = max(trip.exit_date for trip in trips)
    needed = {trip.key for trip in trips}
    by_key: dict[str, list[int]] = {}
    for index, trip in enumerate(trips):
        by_key.setdefault(trip.key, []).append(index)

    values = np.full((len(trips), len(value_ids)), np.nan, dtype=float)
    holding_days = np.zeros(len(trips), dtype=int)
    seen: set[str] = set()

    total = len(refs)
    for offset in range(0, total, batch_size):
        batch = list(refs[offset : offset + batch_size])
        frame = adapter.read_many_stocks(
            batch,
            start_date=start_date,
            end_date=end_date,
            warmup_bars=warmup,
        )
        if not frame.empty:
            keys = frame["market"].astype(str) + frame["code"].astype(str)
            for key, group in frame.groupby(keys, sort=False):
                stock_key = str(key)
                indices = by_key.get(stock_key)
                if not indices:
                    continue
                seen.add(stock_key)
                _write_stock(group, trips, indices, groups, len(value_ids), values, holding_days)
        if progress is not None:
            progress(min(offset + len(batch), total), total)

    missing = tuple(sorted(needed - seen))
    return SignalValues(values=values, holding_days=holding_days, missing_stocks=missing)


def _write_stock(
    group: pd.DataFrame,
    trips: Sequence[RoundTrip],
    indices: Sequence[int],
    groups: dict[str, list[tuple[int, str]]],
    column_count: int,
    values: np.ndarray,
    holding_days: np.ndarray,
) -> None:
    """Fill in one stock's trades: signal-day values and holding periods."""

    if len(group) == 0:
        return
    computed = np.full((len(group), column_count), np.nan, dtype=float)
    for formula_id, items in groups.items():
        result = FORMULA_REGISTRY.formula(formula_id).calculate(group)
        for column, name in items:
            array = np.asarray(result.values.get(name), dtype=float)
            if array.shape == (len(group),):
                computed[:, column] = array

    stamps = pd.DatetimeIndex(pd.to_datetime(group["date"])).to_numpy()
    stock_trips = [trips[index] for index in indices]
    signal_positions = _positions(stamps, [trip.signal_date for trip in stock_trips])
    entry_positions = _positions(stamps, [trip.entry_date for trip in stock_trips])
    exit_positions = _positions(stamps, [trip.exit_date for trip in stock_trips])

    for slot, index in enumerate(indices):
        signal = int(signal_positions[slot])
        if signal >= 0:
            values[index, :] = computed[signal, :]
        entry = int(entry_positions[slot])
        exit_ = int(exit_positions[slot])
        if entry >= 0 and exit_ >= 0 and exit_ >= entry:
            holding_days[index] = int(exit_ - entry)


def _positions(stamps: np.ndarray, days: Sequence[date]) -> np.ndarray:
    """Index of each day in the stock's calendar, ``-1`` when it has no bar."""

    if stamps.size == 0:
        return np.full(len(days), -1, dtype=int)
    targets = np.array([np.datetime64(day) for day in days], dtype=stamps.dtype)
    found = np.searchsorted(stamps, targets)
    np.clip(found, 0, stamps.size - 1, out=found)
    return np.where(stamps[found] == targets, found, -1).astype(int)


__all__ = ["SignalValues", "collect_signal_values"]
