"""33-day main-force absorption score and its two displayed components."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .common import build_formula_result, empty_formula_result
from .indicator_one import calculate_indicator_one
from .types import FormulaResult

FORMULA_ID = "main_force_absorption"
DISPLAY_NAME = "主力吸筹"
MINIMUM_BARS = 33
RECOMMENDED_BARS = 120
VALUE_NAMES = ("score_33", "entry_33", "washout_33")
SIGNAL_NAMES = ("entry", "washout")


def calculate_main_force_absorption(frame: pd.DataFrame) -> FormulaResult:
    """Reproduce the supplied VAR1–VAR5 formula using the existing TDX kernel."""

    if len(frame) < MINIMUM_BARS:
        return empty_formula_result(
            formula_id=FORMULA_ID,
            display_name=DISPLAY_NAME,
            minimum_bars=MINIMUM_BARS,
            recommended_bars=RECOMMENDED_BARS,
            signal_names=SIGNAL_NAMES,
            data_length=len(frame),
        )

    source = calculate_indicator_one(frame)
    score = np.asarray(source.values["var5"], dtype=float)
    entry_signal = np.asarray(source.signals["main_force_entry"], dtype=bool)
    washout_signal = np.asarray(source.signals["wash"], dtype=bool)
    signals = {"entry": entry_signal, "washout": washout_signal}
    values = {
        "score_33": score,
        "entry_33": np.where(entry_signal, score, 0.0),
        "washout_33": np.where(washout_signal, score, 0.0),
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


__all__ = ["calculate_main_force_absorption"]
