"""通达信指标三：准备拉升、压住庄家和建仓区。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .common import MYTT, build_formula_result, empty_formula_result, numeric_column, safe_divide
from .types import FormulaResult

FORMULA_ID = "indicator_three"
DISPLAY_NAME = "准备拉升"
MINIMUM_BARS = 34
RECOMMENDED_BARS = 120
SIGNAL_NAMES = ("prepare_rally", "suppress_main", "accumulation_zone", "begin_zone", "end_zone")


def calculate_indicator_three(frame: pd.DataFrame) -> FormulaResult:
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
    n = 5
    stochastic_base = (
        safe_divide(close - MYTT.LLV(low, n), MYTT.HHV(high, n) - MYTT.LLV(low, n)) * 100
    )
    sma_inner = MYTT.SMA(stochastic_base, 5, 1)
    # The fractional 3.2 period is intentional and matches the source formula.
    var1 = 4 * sma_inner - 3 * MYTT.SMA(sma_inner, 3.2, 1)
    var2 = np.full(len(close), 8.0)

    varo5 = MYTT.LLV(low, 27)
    varo6 = MYTT.HHV(high, 34)
    varo7 = MYTT.EMA(safe_divide(close - varo5, varo6 - varo5) * 4, 4) * 25

    signals = {
        "prepare_rally": MYTT.CROSS(var1, var2),
        "suppress_main": var1 <= 8,
        "accumulation_zone": varo7 < 10,
        "begin_zone": var1 < 10,
        "end_zone": var1 > 90,
    }
    values = {
        "n": np.full(len(close), float(n)),
        "stochastic_base": stochastic_base,
        "sma_inner": sma_inner,
        "var1": var1,
        "var2": var2,
        "varo5": varo5,
        "varo6": varo6,
        "varo7": varo7,
        "prepare_rally": np.where(signals["prepare_rally"], 80, 0),
        "suppress_main": np.where(signals["suppress_main"], 25, 0),
        "accumulation_zone": np.where(signals["accumulation_zone"], 80, 100),
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
