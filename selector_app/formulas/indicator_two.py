"""通达信指标二：成本、饱和度与中短期线。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .common import (
    MYTT,
    build_formula_result,
    dynamic_ref,
    empty_formula_result,
    numeric_column,
    safe_divide,
)
from .types import FormulaResult

FORMULA_ID = "indicator_two"
DISPLAY_NAME = "庄家顶底"
MINIMUM_BARS = 34
RECOMMENDED_BARS = 120
SIGNAL_NAMES = (
    "start",
    "end",
    "saturation_hot",
    "new_high_breakout",
    "short_above_mid_long",
    "short_below_mid_long",
)


def _weighted_x(a: np.ndarray) -> np.ndarray:
    """Keep the source's skipped REF(A,19) and explicit REF(A,20)."""

    result = np.zeros(len(a), dtype=float)
    for offset in range(19):  # A through REF(A,18): weights 20 through 2
        result += (20 - offset) * MYTT.REF(a, offset)
    result += MYTT.REF(a, 20)  # deliberately no REF(A,19)
    return result / 210


def calculate_indicator_two(frame: pd.DataFrame) -> FormulaResult:
    if len(frame) < MINIMUM_BARS:
        return empty_formula_result(
            formula_id=FORMULA_ID,
            display_name=DISPLAY_NAME,
            minimum_bars=MINIMUM_BARS,
            recommended_bars=RECOMMENDED_BARS,
            signal_names=SIGNAL_NAMES,
            data_length=len(frame),
        )

    open_ = numeric_column(frame, "open")
    high = numeric_column(frame, "high")
    low = numeric_column(frame, "low")
    close = numeric_column(frame, "close")
    amount = numeric_column(frame, "amount")

    short_cost = MYTT.EMA(close, 17)
    a = (3 * close + low + open_ + high) / 6
    x = _weighted_x(a)
    mid_cost = MYTT.EMA(x, 13)

    var1 = np.ones(len(close))
    var2 = MYTT.LLV(low, 10)
    var3 = MYTT.HHV(high, 25)
    n1 = np.full(len(close), 5.0)
    n4 = np.full(len(close), 34.0)

    cq_denominator = MYTT.HHV(close, 34) - MYTT.LLV(low, 34)
    cq = 100 * safe_divide(close - MYTT.LLV(low, 34), cq_denominator)
    amount_over_close = safe_divide(amount, close)
    amount_high_over_close_high = safe_divide(MYTT.HHV(amount, 34), MYTT.HHV(close, 34))
    sat = safe_divide(amount_over_close, amount_high_over_close_high)
    saturation = np.minimum(np.where(sat > 1, 1, sat), 1) * 100

    w1 = close == MYTT.HHV(close, 20)
    w2 = MYTT.BARSLAST(w1)
    # Both source IF branches are REF(C,W2), so dynamic_ref is equivalent and clearer.
    w3 = dynamic_ref(close, w2)
    w4 = MYTT.CROSS(close, MYTT.REF(w3, 1))

    mid_term = cq  # same source expression as CQ; reuse the calculated array.
    holding_base = 100 * safe_divide(
        close - MYTT.LLV(low, 27), MYTT.HHV(high, 27) - MYTT.LLV(low, 27)
    )
    holding_sma = MYTT.SMA(holding_base, 5, 1)
    holding = 3 * holding_sma - 2 * MYTT.SMA(holding_sma, 3, 1)
    support = MYTT.LLV(holding, 3)
    short_line = MYTT.EMA(safe_divide(close - var2, var3 - var2) * 4, 4) * var1 * 30
    mid_line = MYTT.MA(holding, 12)

    signals = {
        "start": MYTT.CROSS(short_line, mid_line),
        "end": short_line > 90,
        "saturation_hot": (saturation >= 97) & (cq > 90),
        "new_high_breakout": w4 & (MYTT.COUNT(w4, 5) == 1),
        "short_above_mid_long": MYTT.BARSLAST(short_cost < mid_cost) > 30,
        "short_below_mid_long": MYTT.BARSLAST(short_cost > mid_cost) > 30,
    }
    values = {
        "short_cost": short_cost,
        "a": a,
        "x": x,
        "mid_cost": mid_cost,
        "var1": var1,
        "var2": var2,
        "var3": var3,
        "n1": n1,
        "n4": n4,
        "cq": cq,
        "mid_term": mid_term,
        "sat": sat,
        "saturation": saturation,
        "w1": w1.astype(float),
        "w2": w2,
        "w3": w3,
        "w4": w4.astype(float),
        "holding_base": holding_base,
        "holding": holding,
        "support": support,
        "bottom_line": support,
        "short_line": short_line,
        "mid_line": mid_line,
        "warning_line": np.full(len(close), 85.0),
        "start": np.where(signals["start"], short_line, 0),
        "end": np.where(signals["end"], short_line, 0),
    }
    return build_formula_result(
        formula_id=FORMULA_ID,
        display_name=DISPLAY_NAME,
        minimum_bars=MINIMUM_BARS,
        recommended_bars=RECOMMENDED_BARS,
        frame=frame,
        values=values,
        signals=signals,
    )
