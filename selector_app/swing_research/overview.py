"""Distribution summaries for swing legs.

The point of this module is parameter calibration, not reporting. Before any
indicator is scored against "big moves", somebody has to answer what a big move
actually is in this market: how many legs does a 3-ATR threshold produce, how
long do they last, and how far do they travel? If the median leg lasts two
days, the threshold is too tight and no indicator can meaningfully predict it;
if a stock sees one leg per year, the threshold is too loose to study.

Histogram edges are fixed rather than data-derived on purpose: comparing three
thresholds only makes sense when every one of them is bucketed the same way.

Amplitudes are reported as absolute percentages - a down leg that travelled
-12% is reported as 12 - because the interesting question is how far a leg
moved, and the direction is already carried by which side it is counted on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .segments import DEFAULT_MIN_BARS, SwingSegment

DURATION_BUCKETS: tuple[float, ...] = (0, 3, 5, 10, 20, 40, 80)
AMPLITUDE_BUCKETS: tuple[float, ...] = (0, 3, 5, 10, 20, 40, 80)
QUANTILE_LEVELS: tuple[tuple[str, float], ...] = (
    ("p10", 10),
    ("p25", 25),
    ("p50", 50),
    ("p75", 75),
    ("p90", 90),
)


@dataclass(frozen=True)
class SwingDistribution:
    """Duration and travel of the legs that cleared the minimum length."""

    count: int
    duration: dict[str, float | None]
    amplitude: dict[str, float | None]
    duration_histogram: list[dict[str, Any]]
    amplitude_histogram: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "duration": self.duration,
            "amplitude": self.amplitude,
            "duration_histogram": self.duration_histogram,
            "amplitude_histogram": self.amplitude_histogram,
        }


@dataclass(frozen=True)
class SwingSummary:
    """One reversal threshold, summarised over a whole universe."""

    reversal_multiple: float
    min_bars: int
    stock_count: int
    segment_count: int
    long_segment_count: int
    segments_per_stock_per_year: float | None
    up: SwingDistribution
    down: SwingDistribution

    def to_dict(self) -> dict[str, Any]:
        return {
            "reversal_multiple": self.reversal_multiple,
            "min_bars": self.min_bars,
            "stock_count": self.stock_count,
            "segment_count": self.segment_count,
            "long_segment_count": self.long_segment_count,
            "segments_per_stock_per_year": self.segments_per_stock_per_year,
            "up": self.up.to_dict(),
            "down": self.down.to_dict(),
        }


class SwingAccumulator:
    """Collect legs stock by stock without keeping them all alive.

    Every added batch is reduced to two float arrays (duration and absolute
    amplitude) per direction, so a full-market scan holds a few megabytes
    instead of a few hundred thousand dataclass instances.
    """

    def __init__(
        self,
        reversal_multiple: float,
        *,
        min_bars: int = DEFAULT_MIN_BARS,
    ) -> None:
        if min_bars < 0:
            raise ValueError("最小持续天数不能为负数")
        self._reversal_multiple = reversal_multiple
        self._min_bars = min_bars
        self._stock_count = 0
        self._segment_count = 0
        self._durations: dict[str, list[np.ndarray]] = {"up": [], "down": []}
        self._amplitudes: dict[str, list[np.ndarray]] = {"up": [], "down": []}

    def add(self, segments: list[SwingSegment]) -> None:
        """Fold one stock's legs into the running totals."""

        self._stock_count += 1
        self._segment_count += len(segments)
        for direction in ("up", "down"):
            picked = [
                item
                for item in segments
                if item.direction == direction and item.duration >= self._min_bars
            ]
            if not picked:
                continue
            self._durations[direction].append(
                np.array([float(item.duration) for item in picked], dtype=float)
            )
            self._amplitudes[direction].append(
                np.array([abs(item.amplitude) * 100.0 for item in picked], dtype=float)
            )

    def summary(self, *, years: float | None = None) -> SwingSummary:
        """Reduce everything collected so far into percentiles and histograms."""

        up = self._distribution("up")
        down = self._distribution("down")
        per_year: float | None = None
        if years is not None and years > 0 and self._stock_count:
            per_year = self._segment_count / (self._stock_count * years)
        return SwingSummary(
            reversal_multiple=self._reversal_multiple,
            min_bars=self._min_bars,
            stock_count=self._stock_count,
            segment_count=self._segment_count,
            long_segment_count=up.count + down.count,
            segments_per_stock_per_year=per_year,
            up=up,
            down=down,
        )

    def _distribution(self, direction: str) -> SwingDistribution:
        durations = (
            np.concatenate(self._durations[direction])
            if self._durations[direction]
            else np.array([], dtype=float)
        )
        amplitudes = (
            np.concatenate(self._amplitudes[direction])
            if self._amplitudes[direction]
            else np.array([], dtype=float)
        )
        return SwingDistribution(
            count=int(durations.size),
            duration=_describe(durations),
            amplitude=_describe(amplitudes),
            duration_histogram=_histogram(durations, DURATION_BUCKETS),
            amplitude_histogram=_histogram(amplitudes, AMPLITUDE_BUCKETS),
        )


def _describe(values: np.ndarray) -> dict[str, float | None]:
    if values.size == 0:
        empty: dict[str, float | None] = {name: None for name, _ in QUANTILE_LEVELS}
        empty["mean"] = None
        return empty
    described: dict[str, float | None] = {
        name: float(np.percentile(values, level)) for name, level in QUANTILE_LEVELS
    }
    described["mean"] = float(np.mean(values))
    return described


def _histogram(values: np.ndarray, edges: tuple[float, ...]) -> list[dict[str, Any]]:
    labels = [f"{int(low)}-{int(high)}" for low, high in zip(edges, edges[1:])]
    labels.append(f"{int(edges[-1])}+")
    if values.size == 0:
        return [{"bucket": label, "count": 0} for label in labels]
    counts, _ = np.histogram(values, bins=[*edges, np.inf])
    return [
        {"bucket": label, "count": int(count)} for label, count in zip(labels, counts, strict=True)
    ]


__all__ = [
    "AMPLITUDE_BUCKETS",
    "DURATION_BUCKETS",
    "SwingAccumulator",
    "SwingDistribution",
    "SwingSummary",
]
