"""Cut a whole market into swings and describe what those swings look like.

The service exists so the question "what counts as a big move?" can be
answered with data instead of taste. It reads the universe in bounded batches,
cuts every stock with each requested threshold, and reduces the result to
duration and amplitude distributions - never materialising a full panel of
segments, which is what would otherwise make a 5,000-stock scan fall over.
"""

from __future__ import annotations

from collections.abc import Callable

import pandas as pd

from selector_app.market_data.adapter import BatchMarketDataAdapter
from selector_app.market_data.models import StockRef

from .models import SwingOverviewConfig
from .overview import SwingAccumulator
from .segments import atr_percent, split_swings

ProgressCallback = Callable[[int, int], None] | None


class SwingResearchService:
    """Segment a universe and summarise the legs it produced."""

    def __init__(self, adapter: BatchMarketDataAdapter) -> None:
        self._adapter = adapter

    def overview(
        self,
        config: SwingOverviewConfig,
        progress_callback: ProgressCallback = None,
    ) -> dict[str, object]:
        """Run one overview; returns a JSON-ready payload."""

        resolved = config.normalized()
        refs = self._list_refs(resolved)
        total = len(refs)
        accumulators = {
            multiple: SwingAccumulator(multiple, min_bars=resolved.min_bars)
            for multiple in resolved.reversal_multiples
        }
        # Enough history for the ATR smoothing to settle before the first leg
        # is allowed to start; three times the window is plenty for Wilder.
        # Only meaningful when a window was requested - without a start date
        # the read already begins at the first bar on record, and the store
        # rejects a warm-up it cannot anchor.
        warmup = resolved.atr_window * 3 + 10 if resolved.start_date else 0
        start_timestamp = pd.Timestamp(resolved.start_date) if resolved.start_date else None
        end_timestamp = pd.Timestamp(resolved.end_date) if resolved.end_date else None

        scanned = 0
        first_seen: pd.Timestamp | None = None
        last_seen: pd.Timestamp | None = None
        for offset in range(0, total, resolved.batch_size):
            batch = refs[offset : offset + resolved.batch_size]
            frame = self._adapter.read_many_stocks(
                batch,
                start_date=resolved.start_date,
                end_date=resolved.end_date,
                warmup_bars=warmup,
            )
            if not frame.empty:
                timestamps = pd.to_datetime(frame["date"])
                batch_first = timestamps.min()
                batch_last = timestamps.max()
                first_seen = batch_first if first_seen is None else min(first_seen, batch_first)
                last_seen = batch_last if last_seen is None else max(last_seen, batch_last)
                for _, group in frame.groupby(["market", "code"], sort=True):
                    scanned += 1
                    self._accumulate(
                        group,
                        accumulators,
                        resolved,
                        start_timestamp,
                        end_timestamp,
                    )
            if progress_callback is not None:
                progress_callback(min(offset + len(batch), total), total)

        years = _years_between(first_seen, last_seen)
        return {
            "stock_count": total,
            "scanned_stocks": scanned,
            "min_bars": resolved.min_bars,
            "atr_window": resolved.atr_window,
            "date_range": {
                "start": first_seen.strftime("%Y-%m-%d") if first_seen is not None else None,
                "end": last_seen.strftime("%Y-%m-%d") if last_seen is not None else None,
                "years": years,
            },
            "scenarios": [
                accumulator.summary(years=years).to_dict() for accumulator in accumulators.values()
            ],
        }

    def _list_refs(self, config: SwingOverviewConfig) -> list[StockRef]:
        refs = self._adapter.list_stock_refs(
            config.vipdoc_path,
            config.universe,
            instrument_types=config.instrument_types or None,
            boards=config.boards or None,
        )
        if config.limit_stocks is not None:
            refs = refs[: config.limit_stocks]
        return refs

    @staticmethod
    def _accumulate(
        group: pd.DataFrame,
        accumulators: dict[float, SwingAccumulator],
        config: SwingOverviewConfig,
        start_timestamp: pd.Timestamp | None,
        end_timestamp: pd.Timestamp | None,
    ) -> None:
        """Cut one stock once per threshold, keeping only in-window starts.

        A leg is in the study when it *started* inside the window, even if it
        ran past the end date: the start is what the study will later try to
        predict. Warm-up bars therefore only feed the ATR, never the counts.
        """

        if len(group) < 2:
            return
        timestamps = pd.to_datetime(group["date"])
        keep = pd.Series(True, index=group.index)
        if start_timestamp is not None:
            keep &= timestamps >= start_timestamp
        if end_timestamp is not None:
            keep &= timestamps <= end_timestamp
        keep_values = keep.to_numpy(dtype=bool)
        atr = atr_percent(group, window=config.atr_window)
        for multiple, accumulator in accumulators.items():
            segments = split_swings(
                group,
                reversal_multiple=multiple,
                atr_window=config.atr_window,
                include_pending=config.include_pending,
                atr=atr,
            )
            if keep_values.all():
                accumulator.add(segments)
            else:
                accumulator.add([item for item in segments if keep_values[item.start_index]])


def _years_between(first: pd.Timestamp | None, last: pd.Timestamp | None) -> float | None:
    if first is None or last is None or pd.isna(first) or pd.isna(last):
        return None
    days = (last - first).days
    return days / 365.25 if days > 0 else None
