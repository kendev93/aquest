"""Supervision targets derived from swing segments.

The same segmentation supports two different questions, and they need
different labels:

* **Attribution** - is *today* the first bar of a leg that went on to travel
  far? The features are legitimate (they are today's indicators) but the label
  is only knowable in hindsight: nothing on a start bar announces that it is
  the bottom. This answers "what does a launch pad look like".

* **Anticipation** - does a leg *start running* within the next ``horizon``
  bars? The onset bar is the first day price travelled the reversal threshold
  away from the leg's origin, which is observable in real time, so a model
  trained on this label can actually be traded. This answers "is a move about
  to start".

Both keep the bar's own features strictly in the past or present; only the
label may look ahead, which is what makes it a prediction target rather than a
leak.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd

from .segments import SwingSegment

LabelKind = Literal[
    "up_leg_start",
    "down_leg_start",
    "up_onset_ahead",
    "down_onset_ahead",
]

ALL_LABELS: tuple[LabelKind, ...] = (
    "up_leg_start",
    "down_leg_start",
    "up_onset_ahead",
    "down_onset_ahead",
)

LABEL_DISPLAY_NAMES: dict[str, str] = {
    "up_leg_start": "大涨段起点（归因）",
    "down_leg_start": "大跌段起点（归因）",
    "up_onset_ahead": "近期将启动上涨",
    "down_onset_ahead": "近期将启动下跌",
}

DEFAULT_HORIZON = 10


def segment_onset(frame: pd.DataFrame, segment: SwingSegment) -> int | None:
    """Bar on which the leg's direction was first *confirmed*.

    A leg starts at its origin bar, but nobody can see that on the day - the
    leg is only a candidate until price has actually travelled the reversal
    threshold away from the origin. This returns that first bar, which is the
    earliest day the move can be traded.

    ``None`` when the leg never travelled far enough, which happens for the
    trailing leg of a series that simply ended.
    """

    if segment.direction == "up":
        prices = np.asarray(frame["high"], dtype=float)
        travel = (prices[segment.start_index :] - segment.start_price) / segment.start_price
    else:
        prices = np.asarray(frame["low"], dtype=float)
        travel = (segment.start_price - prices[segment.start_index :]) / segment.start_price
    if not np.isfinite(segment.start_price) or segment.start_price <= 0:
        return None
    hits = np.nonzero(np.nan_to_num(travel, nan=-np.inf) >= segment.threshold)[0]
    if hits.size == 0:
        return None
    return segment.start_index + int(hits[0])


def _ahead(events: np.ndarray, horizon: int) -> np.ndarray:
    """Whether each bar is followed by an event within ``horizon`` bars.

    The trailing ``horizon`` bars cannot answer the question - the future they
    would need does not exist yet - so they are marked unknown rather than
    negative. Marking them negative is a classic way to manufacture a fake
    edge, because the label then becomes easier exactly where the data ends.
    """

    count = len(events)
    result = np.full(count, np.nan, dtype=float)
    if count == 0 or horizon < 1:
        return result
    # Suffix sums give the event count of any forward window in O(1).
    suffix = np.concatenate([np.cumsum(events[::-1])[::-1], [0.0]])
    starts = np.arange(count) + 1
    # Only a window that fits entirely inside the series counts as answered;
    # a partial one is unknown rather than negative.
    complete = starts + horizon <= count
    result[complete] = (suffix[starts[complete]] - suffix[starts[complete] + horizon]) > 0
    return result


def bar_labels(
    frame: pd.DataFrame,
    segments: list[SwingSegment],
    *,
    min_bars: int = 0,
    horizon: int = DEFAULT_HORIZON,
) -> dict[str, np.ndarray]:
    """Per-bar targets for one stock, keyed by :data:`ALL_LABELS`.

    Legs shorter than ``min_bars`` never contribute: the study asked for moves
    of a certain size, and a two-bar blip is not one of them even though the
    segmenter happily reports it.
    """

    count = len(frame)
    up_start = np.zeros(count, dtype=float)
    down_start = np.zeros(count, dtype=float)
    up_onset = np.zeros(count, dtype=float)
    down_onset = np.zeros(count, dtype=float)

    for segment in segments:
        if segment.pending or segment.duration < min_bars:
            continue
        if segment.direction == "up":
            if 0 <= segment.start_index < count:
                up_start[segment.start_index] = 1.0
        elif 0 <= segment.start_index < count:
            down_start[segment.start_index] = 1.0
        onset = segment_onset(frame, segment)
        if onset is None or not 0 <= onset < count:
            continue
        if segment.direction == "up":
            up_onset[onset] = 1.0
        else:
            down_onset[onset] = 1.0

    return {
        "up_leg_start": up_start,
        "down_leg_start": down_start,
        "up_onset_ahead": _ahead(up_onset, horizon),
        "down_onset_ahead": _ahead(down_onset, horizon),
    }


__all__ = [
    "ALL_LABELS",
    "DEFAULT_HORIZON",
    "LABEL_DISPLAY_NAMES",
    "LabelKind",
    "bar_labels",
    "segment_onset",
]
