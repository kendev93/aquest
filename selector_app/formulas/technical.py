"""Common price/volume indicators exposed to strategy and chart consumers."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .common import build_formula_result, numeric_column
from .types import FormulaResult

FORMULA_ID = "technical"
DISPLAY_NAME = "K线技术指标"
MINIMUM_BARS = 1
RECOMMENDED_BARS = 60
SIGNAL_NAMES: tuple[str, ...] = ()
VALUE_NAMES = (
    "ma5",
    "ma10",
    "ma20",
    "ma60",
    "rsi14",
    "macd",
    "macd_signal",
    "macd_histogram",
)


def calculate_technical_indicators(frame: pd.DataFrame) -> FormulaResult:
    close = pd.Series(numeric_column(frame, "close"), index=frame.index)
    values: dict[str, object] = {
        f"ma{window}": close.rolling(window, min_periods=window).mean().to_numpy()
        for window in (5, 10, 20, 60)
    }

    delta = close.diff()
    gains = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    losses = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    relative_strength = gains / losses.replace(0, np.nan)
    rsi = 100 - (100 / (1 + relative_strength))
    rsi = rsi.mask(losses.eq(0) & gains.gt(0), 100)
    rsi = rsi.mask(losses.eq(0) & gains.eq(0), 50)
    values["rsi14"] = rsi.to_numpy()

    ema12 = close.ewm(span=12, adjust=False, min_periods=12).mean()
    ema26 = close.ewm(span=26, adjust=False, min_periods=26).mean()
    macd = ema12 - ema26
    macd_signal = macd.ewm(span=9, adjust=False, min_periods=9).mean()
    values["macd"] = macd.to_numpy()
    values["macd_signal"] = macd_signal.to_numpy()
    values["macd_histogram"] = (macd - macd_signal).to_numpy()
    return build_formula_result(
        formula_id=FORMULA_ID,
        display_name=DISPLAY_NAME,
        minimum_bars=MINIMUM_BARS,
        recommended_bars=RECOMMENDED_BARS,
        frame=frame,
        values=values,
        signals={},
    )
