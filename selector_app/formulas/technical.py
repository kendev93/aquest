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
    "kdj_k",
    "kdj_d",
    "kdj_j",
    "boll_percent_b",
    "boll_bandwidth",
    "wr14",
    "cci14",
)

_KDJ_PERIOD = 9
_KDJ_SMOOTHING = 3
_BOLL_PERIOD = 20
_BOLL_STD = 2.0
_WR_PERIOD = 14
_CCI_PERIOD = 14
# CCI divides by ``0.015 * mean absolute deviation``; the constant is the
# original Lambert factor that puts roughly 70-80% of values inside ±100.
_CCI_SCALE = 0.015


def _rolling_mean_abs_deviation(values: np.ndarray, window: int) -> np.ndarray:
    """Rolling ``mean(|x - mean(x)|)`` computed without a Python callback.

    CCI and AVEDEV need the mean absolute deviation of every window.  Going
    through ``Series.rolling().apply`` would call back into Python once per
    bar, which matters because the indicator cache precomputes every formula
    for the whole universe.  A sliding-window view keeps it in NumPy while
    preserving the NaN semantics of ``min_periods=window``: a window that
    contains a NaN yields NaN.
    """

    source = np.asarray(values, dtype=float)
    result = np.full(source.shape, np.nan, dtype=float)
    if source.size < window:
        return result
    windows = np.lib.stride_tricks.sliding_window_view(source, window)
    deviations = np.abs(windows - windows.mean(axis=1, keepdims=True))
    result[window - 1 :] = deviations.mean(axis=1)
    return result


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

    # KDJ (9, 3, 3), ported from ETFQuest ``core/indicators.py::kdj``. RSV is
    # already a 0-100 position inside the 9-bar range, so K/D inherit that
    # scale and stay comparable across stocks. J = 3K - 2D is not bounded but
    # is a pure function of the same two series, so it is dimensionless too.
    kdj_low = low.rolling(_KDJ_PERIOD, min_periods=_KDJ_PERIOD).min()
    kdj_high = high.rolling(_KDJ_PERIOD, min_periods=_KDJ_PERIOD).max()
    raw_stochastic = (
        safe_divide(
            close_values - kdj_low.to_numpy(),
            kdj_high.to_numpy() - kdj_low.to_numpy(),
        )
        * 100
    )
    # A flat window (一字板/停牌) makes RSV NaN; EWM skips NaN rather than
    # propagating inf, so the rest of the series stays usable.
    k_line = (
        pd.Series(raw_stochastic, index=frame.index)
        .ewm(alpha=1 / _KDJ_SMOOTHING, adjust=False)
        .mean()
    )
    d_line = k_line.ewm(alpha=1 / _KDJ_SMOOTHING, adjust=False).mean()
    values["kdj_k"] = k_line.to_numpy()
    values["kdj_d"] = d_line.to_numpy()
    values["kdj_j"] = (3 * k_line - 2 * d_line).to_numpy()

    # Bollinger (20, 2). Only the two dimensionless derivatives are exposed:
    # the bands themselves are prices, and a condition on them is exactly
    # equivalent to a condition on %B (%B > 100 means the close is above the
    # upper band, %B < 0 below the lower one), so the raw bands would only add
    # three non-rankable price-scale outputs.
    boll_middle = close.rolling(_BOLL_PERIOD, min_periods=_BOLL_PERIOD).mean()
    boll_deviation = close.rolling(_BOLL_PERIOD, min_periods=_BOLL_PERIOD).std()
    boll_upper = boll_middle + (_BOLL_STD * boll_deviation)
    boll_lower = boll_middle - (_BOLL_STD * boll_deviation)
    boll_width = (boll_upper - boll_lower).to_numpy()
    values["boll_percent_b"] = safe_divide(close_values - boll_lower.to_numpy(), boll_width) * 100
    # Band width over the middle band: a volatility level in percent, so a
    # squeeze is comparable between a 3 CNY stock and a 300 CNY one.
    values["boll_bandwidth"] = safe_divide(boll_width, boll_middle.to_numpy()) * 100

    # Williams %R (14). Mirrored relative to the usual -100..0 convention:
    # ETFQuest returns (Hn - C) / (Hn - Ln) * 100, so 0 = at the window high
    # and 100 = at the window low. Kept as-is to stay comparable with the
    # upstream project.
    wr_low = low.rolling(_WR_PERIOD, min_periods=_WR_PERIOD).min()
    wr_high = high.rolling(_WR_PERIOD, min_periods=_WR_PERIOD).max()
    values["wr14"] = (
        safe_divide(wr_high.to_numpy() - close_values, wr_high.to_numpy() - wr_low.to_numpy()) * 100
    )

    # CCI (14) on the typical price. It is dimensionless because both the
    # numerator and the denominator scale with the price, and it straddles
    # zero, so it is one of the few outputs that suits a ``> 0`` / ``< 0``
    # default condition pair.
    typical_price = ((high + low + close) / 3).to_numpy()
    typical_mean = (
        pd.Series(typical_price, index=frame.index)
        .rolling(_CCI_PERIOD, min_periods=_CCI_PERIOD)
        .mean()
        .to_numpy()
    )
    mean_deviation = _rolling_mean_abs_deviation(typical_price, _CCI_PERIOD)
    values["cci14"] = safe_divide(typical_price - typical_mean, _CCI_SCALE * mean_deviation)
    return build_formula_result(
        formula_id=FORMULA_ID,
        display_name=DISPLAY_NAME,
        minimum_bars=MINIMUM_BARS,
        recommended_bars=RECOMMENDED_BARS,
        frame=frame,
        values=values,
        signals={},
    )
