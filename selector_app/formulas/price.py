"""Raw K-line price outputs, exposed so conditions can pair prices with indicators."""

from __future__ import annotations

import pandas as pd

from .common import build_formula_result, numeric_column
from .types import FormulaResult

FORMULA_ID = "price"
DISPLAY_NAME = "K线价格"
MINIMUM_BARS = 1
RECOMMENDED_BARS = 1
VALUE_NAMES = ("open", "high", "low", "close")


def calculate_price_series(frame: pd.DataFrame) -> FormulaResult:
    """Return the raw OHLC columns as named numeric outputs.

    Prices need no warm-up, so every bar is a valid comparison operand.  They
    stay out of the rankable catalog because an absolute price is not
    comparable across stocks: prices are meant to be paired with a same-stock
    indicator (or constant) inside one condition, never used as a cross-stock
    threshold or ranking feature.
    """

    values = {name: numeric_column(frame, name) for name in VALUE_NAMES}
    return build_formula_result(
        formula_id=FORMULA_ID,
        display_name=DISPLAY_NAME,
        minimum_bars=MINIMUM_BARS,
        recommended_bars=RECOMMENDED_BARS,
        frame=frame,
        values=values,
        signals={},
    )
