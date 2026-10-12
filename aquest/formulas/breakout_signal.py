"""19-bar range-normalised breakout/breakdown lines."""

from __future__ import annotations

import pandas as pd

from .common import MYTT, build_formula_result, empty_formula_result, numeric_column, safe_divide
from .types import FormulaResult

FORMULA_ID = "breakout_signal"
DISPLAY_NAME = "突破信号"
MINIMUM_BARS = 19
RECOMMENDED_BARS = 120
VALUE_NAMES = (
    "resistance_19_21",
    "operation_19_5",
    "spread",
    "breakthrough",
    "breakdown",
)
SIGNAL_NAMES = ("breakthrough", "breakdown")


def calculate_breakout_signal(frame: pd.DataFrame) -> FormulaResult:
    """Calculate the supplied HHV/LLV, EMA and CROSS formula without lookahead."""

    if len(frame) < MINIMUM_BARS:
        return empty_formula_result(
            formula_id=FORMULA_ID,
            display_name=DISPLAY_NAME,
            minimum_bars=MINIMUM_BARS,
            recommended_bars=RECOMMENDED_BARS,
            signal_names=SIGNAL_NAMES,
            data_length=len(frame),
        )

    high = numeric_column(frame, "high")
    low = numeric_column(frame, "low")
    close = numeric_column(frame, "close")
    range_high = MYTT.HHV(high, 19)
    range_low = MYTT.LLV(low, 19)
    normalized_close = safe_divide(close - range_low, range_high - range_low)
    resistance = MYTT.EMA(normalized_close, 21) - 0.5
    operation = MYTT.EMA(normalized_close, 5) - 0.5

    breakthrough = MYTT.CROSS(operation, resistance) & (resistance < 0.1)
    breakdown = MYTT.CROSS(resistance, operation) & (resistance > 0.25)
    signals = {
        "breakthrough": breakthrough,
        "breakdown": breakdown,
    }
    values = {
        "resistance_19_21": resistance,
        "operation_19_5": operation,
        "spread": operation - resistance,
        "breakthrough": breakthrough.astype(float),
        "breakdown": breakdown.astype(float),
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


__all__ = ["calculate_breakout_signal"]
