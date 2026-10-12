"""Supervision targets derived from swing segments.

The same segmentation supports two different questions, and they need
different labels:

* **Attribution** - is *today* the pivot a leg started from? Adjacent legs
  share their pivot: a leg closes at its extreme bar and the next one starts
  there, so the bar this marks is the *extreme of the previous leg* -
  ``up_leg_start`` sits on the lowest low of the preceding down leg and
  ``down_leg_start`` on the highest high of the preceding up leg. Despite the
  name, attribution studies describe what tops and bottoms look like. The
  features are legitimate (they are today's indicators) but the label is only
  knowable in hindsight: nothing on a pivot bar announces that it is the
  bottom. This answers "what does a launch pad look like".

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

#: 名字里带「最高/最低点」而不是「段起点」：段起点听起来像研究一段行情怎么开始，
#: 而它实际打在**前一波的极值点**上（相邻两段共用那个转折点）。叫「段起点」时没人
#: 看得出这是在研究顶和底。
LABEL_DISPLAY_NAMES: dict[str, str] = {
    "up_leg_start": "波段最低点（事后归因）",
    "down_leg_start": "波段最高点（事后归因）",
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

    # 只在本段内找确认点。ZigZag 允许「推进幅度没到阈值、回撤已经到阈值」的腿，
    # 不截断的话命中会落在后一段甚至无关的 bar 上——*onset_ahead 是唯一被标注为
    # 「实时可交易」的口径，错标会直接产出错误的交易条件。
    stop = segment.end_index + 1
    if segment.direction == "up":
        prices = np.asarray(frame["high"], dtype=float)
        travel = (prices[segment.start_index : stop] - segment.start_price) / segment.start_price
    else:
        prices = np.asarray(frame["low"], dtype=float)
        travel = (segment.start_price - prices[segment.start_index : stop]) / segment.start_price
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
    segmenter happily reports it. Neither does the very first leg - see the
    comment in the loop.
    """

    count = len(frame)
    up_start = np.zeros(count, dtype=float)
    down_start = np.zeros(count, dtype=float)
    up_onset = np.zeros(count, dtype=float)
    down_onset = np.zeros(count, dtype=float)

    for position, segment in enumerate(segments):
        if segment.pending or segment.duration < min_bars:
            continue
        # 第一段不参与打标：split_swings 从 seg_start=0 起步，所以第一段的起点恒为
        # 数据窗口的第一根 bar，不是由反转确认出来的极值点。其余每一段的起点都等于
        # 前一段的终点，也就是真正的转折点。留着第一段会让「最低点」这个口径里混进
        # 一个随手取的坐标——它不含任何关于转折的信息。
        if position == 0:
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
