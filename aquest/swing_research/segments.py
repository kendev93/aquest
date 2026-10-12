"""Volatility-adaptive swing segmentation (ZigZag).

A swing study needs a mechanical definition of "a big move" before any
indicator can be blamed or credited for it. This module uses the classic
ZigZag: a leg keeps running until price retraces by more than a threshold, at
which point the leg is closed at its extreme bar and the opposite leg starts
from that extreme.

Two deliberate choices make the result usable across a whole market:

* **The threshold is measured in ATR units, not percent.** ``k * ATR / close``
  is evaluated at the bar where a leg starts and then held constant for that
  leg. A 3% move is noise for a volatile small cap and a real reversal for a
  utility; in ATR units both are judged on their own scale, so a segment means
  roughly the same thing for every stock in the universe.
* **A leg ends at its extreme bar, not at the bar that confirmed the
  reversal.** The confirmation is always late - the retrace has to happen
  first - so closing the leg at the confirmation bar would silently shorten
  every leg by however long the market took to turn.

The ATR recipe matches ``formulas.technical.atr_pct`` exactly (Wilder smoothing
with ``alpha = 1 / 14``), so the threshold a study uses later on the live
indicator and the one used here to cut history agree with each other.

Segments are deliberately *not* filtered by duration here. Filtering is a
study-level decision (how big is "big"?), so callers get every leg and decide.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

from aquest.formulas.common import safe_divide

ATR_WINDOW = 14
DEFAULT_REVERSAL_MULTIPLE = 2.0
DEFAULT_MIN_BARS = 5

SwingDirection = Literal["up", "down"]


@dataclass(frozen=True)
class SwingSegment:
    """One directional leg of a price series.

    ``amplitude`` is a fraction (0.08 = a 8% move): positive for up legs and
    negative for down legs, because the leg is measured from the extreme price
    at ``start_index`` to the extreme price at ``end_index``. ``duration`` is
    the calendar distance in bars between the two extremes, so a leg that
    started and topped on the same bar has duration 0.
    """

    direction: SwingDirection
    start_index: int
    end_index: int
    start_price: float
    end_price: float
    amplitude: float
    threshold: float
    pending: bool

    @property
    def duration(self) -> int:
        """Bars between the two extremes - zero for a single-bar leg."""

        return self.end_index - self.start_index

    def to_dict(self) -> dict[str, object]:
        return {
            "direction": self.direction,
            "start_index": self.start_index,
            "end_index": self.end_index,
            "duration": self.duration,
            "start_price": self.start_price,
            "end_price": self.end_price,
            "amplitude": self.amplitude,
            "amplitude_pct": self.amplitude * 100,
            "threshold": self.threshold,
            "threshold_pct": self.threshold * 100,
            "pending": self.pending,
        }


def atr_percent(frame: pd.DataFrame, *, window: int = ATR_WINDOW) -> np.ndarray:
    """Wilder ATR expressed as a percentage of the close.

    Same recipe as ``technical.atr_pct``; duplicated here because the swing
    study only needs this one number and pulling in the whole indicator bundle
    would make a full-market scan an order of magnitude slower.
    """

    high = pd.Series(np.asarray(frame["high"], dtype=float))
    low = pd.Series(np.asarray(frame["low"], dtype=float))
    close = pd.Series(np.asarray(frame["close"], dtype=float))
    previous_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()],
        axis=1,
    ).max(axis=1)
    atr = true_range.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    return safe_divide(atr.to_numpy(), close.to_numpy()) * 100.0


def split_swings(
    frame: pd.DataFrame,
    *,
    reversal_multiple: float = DEFAULT_REVERSAL_MULTIPLE,
    atr_window: int = ATR_WINDOW,
    include_pending: bool = False,
    atr: np.ndarray | None = None,
) -> list[SwingSegment]:
    """Cut one price series into alternating up and down legs.

    The leg whose reversal has not happened yet - the one still being written
    by the market - is only returned when ``include_pending`` is set, because
    its end point will move tomorrow.

    ``atr`` accepts a pre-computed ATR percentage series. Passing it lets a
    caller sweep several thresholds over one series without paying for the
    smoothing each time.
    """

    if reversal_multiple <= 0:
        raise ValueError("反转阈值倍数必须大于 0")
    if atr_window < 1:
        raise ValueError("ATR 窗口必须大于 0")
    missing = [name for name in ("high", "low", "close") if name not in frame.columns]
    if missing:
        raise ValueError(f"K 线缺少必要列: {', '.join(missing)}")

    high = np.asarray(frame["high"], dtype=float)
    low = np.asarray(frame["low"], dtype=float)
    close = np.asarray(frame["close"], dtype=float)
    count = len(close)
    if count < 2 or not np.isfinite(close[0]) or close[0] <= 0:
        return []

    if atr is None:
        atr = atr_percent(frame, window=atr_window)
    median = np.nanmedian(atr) if np.isfinite(atr).any() else np.nan
    fallback = float(median) if np.isfinite(median) else 0.0

    def threshold_at(index: int) -> float:
        value = atr[index] if 0 <= index < count else np.nan
        if not np.isfinite(value):
            value = fallback
        return reversal_multiple * value / 100.0

    def extreme_between(start: int, end: int, direction: SwingDirection) -> tuple[int, float]:
        if direction == "up":
            offset = int(np.argmax(high[start : end + 1]))
            return start + offset, float(high[start + offset])
        offset = int(np.argmin(low[start : end + 1]))
        return start + offset, float(low[start + offset])

    segments: list[SwingSegment] = []
    seg_start = 0
    threshold = threshold_at(seg_start)
    if threshold <= 0:
        return []
    direction: SwingDirection | None = None
    extreme_index = 0
    extreme_price = 0.0

    for index in range(1, count):
        if direction is None:
            if (high[index] - close[0]) / close[0] >= threshold:
                direction = "up"
            elif (close[0] - low[index]) / close[0] >= threshold:
                direction = "down"
            else:
                continue
            extreme_index, extreme_price = extreme_between(0, index, direction)
            continue

        if direction == "up":
            if high[index] > extreme_price:
                extreme_price = float(high[index])
                extreme_index = index
            if extreme_price > 0 and (extreme_price - low[index]) / extreme_price >= threshold:
                segments.append(
                    _close_leg(
                        "up",
                        seg_start,
                        extreme_index,
                        float(low[seg_start]),
                        extreme_price,
                        threshold,
                    )
                )
                seg_start = extreme_index
                direction = "down"
                threshold = threshold_at(seg_start)
                extreme_index, extreme_price = extreme_between(seg_start, index, "down")
        else:
            if low[index] < extreme_price:
                extreme_price = float(low[index])
                extreme_index = index
            if extreme_price > 0 and (high[index] - extreme_price) / extreme_price >= threshold:
                segments.append(
                    _close_leg(
                        "down",
                        seg_start,
                        extreme_index,
                        float(high[seg_start]),
                        extreme_price,
                        threshold,
                    )
                )
                seg_start = extreme_index
                direction = "up"
                threshold = threshold_at(seg_start)
                extreme_index, extreme_price = extreme_between(seg_start, index, "up")

    if direction is not None and include_pending and extreme_index > seg_start:
        start_price = float(low[seg_start]) if direction == "up" else float(high[seg_start])
        segments.append(
            _close_leg(
                direction,
                seg_start,
                extreme_index,
                start_price,
                extreme_price,
                threshold,
                pending=True,
            )
        )
    return segments


def _close_leg(
    direction: SwingDirection,
    start_index: int,
    end_index: int,
    start_price: float,
    end_price: float,
    threshold: float,
    *,
    pending: bool = False,
) -> SwingSegment:
    amplitude = (end_price - start_price) / start_price if start_price > 0 else float("nan")
    return SwingSegment(
        direction=direction,
        start_index=start_index,
        end_index=end_index,
        start_price=start_price,
        end_price=end_price,
        amplitude=float(amplitude),
        threshold=threshold,
        pending=pending,
    )


__all__ = [
    "ATR_WINDOW",
    "DEFAULT_MIN_BARS",
    "DEFAULT_REVERSAL_MULTIPLE",
    "SwingDirection",
    "SwingSegment",
    "atr_percent",
    "split_swings",
]
