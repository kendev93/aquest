"""通达信指标一：主力进场/洗盘/拉高/出货。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .common import MYTT, build_formula_result, empty_formula_result, numeric_column
from .types import FormulaResult

FORMULA_ID = "indicator_one"
DISPLAY_NAME = "主力清洗"
MINIMUM_BARS = 33
RECOMMENDED_BARS = 120
SIGNAL_NAMES = ("main_force_entry", "wash", "main_force_raise", "distribution")


def calculate_indicator_one(frame: pd.DataFrame) -> FormulaResult:
    if len(frame) < MINIMUM_BARS:
        return empty_formula_result(
            formula_id=FORMULA_ID,
            display_name=DISPLAY_NAME,
            minimum_bars=MINIMUM_BARS,
            recommended_bars=RECOMMENDED_BARS,
            signal_names=SIGNAL_NAMES,
            data_length=len(frame),
        )

    low = numeric_column(frame, "low")
    open_ = numeric_column(frame, "open")
    close = numeric_column(frame, "close")
    high = numeric_column(frame, "high")
    var1 = MYTT.REF((low + open_ + close + high) / 4, 1)
    var2_numerator = MYTT.SMA(MYTT.ABS(low - var1), 13, 1)
    var2_denominator = MYTT.SMA(MYTT.MAX(low - var1, 0), 10, 1)
    var2 = np.divide(
        var2_numerator, var2_denominator, out=np.full(len(low), np.nan), where=var2_denominator != 0
    )
    var3 = MYTT.EMA(var2, 10)
    var4 = MYTT.LLV(low, 33)
    var5 = MYTT.EMA(np.where(low <= var4, var3, 0), 3)
    var5_previous = MYTT.REF(var5, 1)

    # Preserve the source formula exactly: MIN(HIGH-VAR1, 0), not ABS(...).
    var21_denominator_input = MYTT.MIN(high - var1, 0)
    var21_numerator = MYTT.SMA(MYTT.ABS(high - var1), 13, 1)
    var21_denominator = MYTT.SMA(var21_denominator_input, 10, 1)
    var21 = np.divide(
        var21_numerator,
        var21_denominator,
        out=np.full(len(high), np.nan),
        where=var21_denominator != 0,
    )
    var31 = MYTT.EMA(var21, 10)
    var41 = MYTT.HHV(high, 33)
    var51 = MYTT.EMA(np.where(high >= var41, var31, 0), 3)
    var51_previous = MYTT.REF(var51, 1)

    signals = {
        # Signal ids retain the Chinese formula's named outputs in English.
        "main_force_entry": var5 > var5_previous,  # 主力进场
        "wash": var5 < var5_previous,  # 洗盘
        "main_force_raise": var51 < var51_previous,  # 主力拉高
        "distribution": var51 > var51_previous,  # 出货
    }
    values = {
        "z": np.zeros(len(low), dtype=float),
        "var1": var1,
        "var2_numerator": var2_numerator,
        "var2_denominator": var2_denominator,
        "var2": var2,
        "var3": var3,
        "var4": var4,
        "var5": var5,
        "main_force_entry": var5,
        "wash": np.where(signals["wash"], var5, 0),
        "var21_denominator_input": var21_denominator_input,
        "var21_numerator": var21_numerator,
        "var21_denominator": var21_denominator,
        "var21": var21,
        "var31": var31,
        "var41": var41,
        "var51": var51,
        "main_force_raise": np.where(signals["main_force_raise"], var51, 0),
        "distribution": np.where(signals["distribution"], var51, 0),
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
