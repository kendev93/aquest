"""Common price/volume indicators exposed to strategy and chart consumers."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .common import build_formula_result, numeric_column, safe_divide
from .types import FormulaResult

FORMULA_ID = "technical"
DISPLAY_NAME = "K线技术指标"
MINIMUM_BARS = 1
RECOMMENDED_BARS = 60
SIGNAL_NAMES: tuple[str, ...] = ()
# The trailing five are dimensionless (percentages or ratios) and are the
# reason this module exists beyond charting: unlike MA/MACD they stay
# comparable across stocks, so the mining pool and cross-section ranking can
# consume them. Keep new entries appended — the UI defaults a fresh condition
# to the first available value.
VALUE_NAMES = (
    "ma5",
    "ma10",
    "ma20",
    "ma60",
    "rsi14",
    "macd",
    "macd_signal",
    "macd_histogram",
    "atr_pct",
    "volume_ratio5",
    "bias20",
    "return20",
    "position20",
)


def calculate_technical_indicators(frame: pd.DataFrame) -> FormulaResult:
    close = pd.Series(numeric_column(frame, "close"), index=frame.index)
    high = pd.Series(numeric_column(frame, "high"), index=frame.index)
    low = pd.Series(numeric_column(frame, "low"), index=frame.index)
    volume = pd.Series(numeric_column(frame, "volume"), index=frame.index)
    close_values = close.to_numpy()
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

    # Dimensionless derivatives below. They are the cross-stock comparable
    # counterparts of the price-scale outputs above: MA/MACD inherit the share
    # price (a 200 CNY stock always outranks a 3 CNY one), while these are
    # ratios or percentages and stay comparable across any universe that mixes
    # stocks with ETFs.
    previous_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()],
        axis=1,
    ).max(axis=1)
    atr14 = true_range.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    # ATR is an absolute price span; only ATR/close survives a mixed universe.
    values["atr_pct"] = safe_divide(atr14.to_numpy(), close_values) * 100

    # The average deliberately excludes the current bar (shift(1)) so a stock
    # cannot inflate its own ratio with the very volume being measured.
    average_volume = volume.shift(1).rolling(5, min_periods=5).mean()
    values["volume_ratio5"] = safe_divide(volume.to_numpy(), average_volume.to_numpy())

    ma20 = values["ma20"]
    values["bias20"] = safe_divide(close_values - ma20, ma20) * 100
    values["return20"] = (safe_divide(close_values, close.shift(20).to_numpy()) - 1) * 100

    # Where the close sits inside the 20-bar range, 0 = at the low, 100 = at
    # the high. Bounded, so it is the friendliest target for mined thresholds.
    range_low = low.rolling(20, min_periods=20).min()
    range_high = high.rolling(20, min_periods=20).max()
    values["position20"] = (
        safe_divide(
            close_values - range_low.to_numpy(),
            range_high.to_numpy() - range_low.to_numpy(),
        )
        * 100
    )
    return build_formula_result(
        formula_id=FORMULA_ID,
        display_name=DISPLAY_NAME,
        minimum_bars=MINIMUM_BARS,
        recommended_bars=RECOMMENDED_BARS,
        frame=frame,
        values=values,
        signals={},
    )
