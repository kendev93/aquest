"""Common price/volume indicators exposed to strategy and chart consumers."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .common import build_formula_result, numeric_column, safe_divide
from .types import FormulaResult

FORMULA_ID = "technical"
DISPLAY_NAME = "K线技术指标"
MINIMUM_BARS = 1
SIGNAL_NAMES: tuple[str, ...] = ()
# The trailing entries are dimensionless (percentages, ratios, bounded scores)
# and are the reason this module exists beyond charting: unlike MA/MACD they
# stay comparable across stocks, so the mining pool and cross-section ranking
# can consume them. Keep new entries appended — the UI defaults a fresh
# condition to the first available value.
#
# They come in three families: volatility/position (atr_pct … cci14), volume
# pressure (vr24, obv_flow20) and trend quality (r2_24, annual_trend24,
# trend_quality, rsrs_*). New entries add longer momentum/regime windows,
# amount-based activity, volatility expansion, and opening-gap risk. The
# regression family is deliberately separate: oscillator readings say where
# a stock sits, trend quality says how cleanly it has been moving.
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
    "vr24",
    "obv_flow20",
    "r2_24",
    "annual_trend24",
    "trend_quality",
    "rsrs_slope",
    "rsrs_zscore",
    "return60",
    "return120",
    "bias60",
    "bias120",
    "amount_ratio20",
    "volatility_ratio20_60",
    "gap_pct",
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
_VR_PERIOD = 24
_OBV_PERIOD = 20
# Window of the log-price regression that feeds R², the annualised trend and
# the trend-quality score. The reference implementation uses the same 24 for
# all three.
_TREND_PERIOD = 24
# Window of the realised volatility used as the trend-quality denominator.
_VOLATILITY_PERIOD = 60
_ANNUALISATION = 252.0
# Bounds on the *annualised log* return before it is compounded, i.e. the trend
# score lands in [exp(floor)-1, exp(ceiling)-1] = [-95%, +1000%].
_ANNUAL_TREND_FLOOR = -3.0
_ANNUAL_TREND_CEILING = 2.398  # log(11)
_RSRS_PERIOD = 18
# The original RSRS standardises the beta over 600 bars (~2.4 years). A-share
# backtests usually cover 2-3 years, where 600 would leave most of the range
# undefined, so the standardisation window is one trading year instead.
_RSRS_ZSCORE_PERIOD = 250
# Warm-up before *every* output carries a value. RSRS is the binding
# constraint: its standard score needs the 18-bar regression to yield a beta
# and then a further 250 bars to standardise that beta, so roughly a trading
# year. The volatility/trend-quality family is ready within 60 bars, but
# quoting 60 here would promise a column that is still NaN across most of a
# one-year backtest. Raising this only changes the advertised warm-up — the
# cache and ``sufficient_data`` gate on ``minimum_bars``, not on this.
RECOMMENDED_BARS = _RSRS_PERIOD + _RSRS_ZSCORE_PERIOD


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


def _rolling_regression(values: np.ndarray, window: int) -> tuple[np.ndarray, np.ndarray]:
    """Rolling OLS of each window against ``0 .. window - 1``.

    Returns ``(slope, r_squared)``. The regressor is a fixed arithmetic
    sequence, so the closed-form solution ``cov(x, y) / var(x)`` needs only
    windowed sums and the whole thing stays in NumPy; the reference
    implementation computes it with ``rolling().apply``, which calls back into
    Python once per bar and is too slow for precomputing the full universe.
    """

    source = np.asarray(values, dtype=float)
    empty = np.full(source.shape, np.nan, dtype=float)
    if source.size < window:
        return empty, empty.copy()

    x_centered = np.arange(window, dtype=float) - (window - 1) / 2.0
    denominator = float((x_centered**2).sum())
    windows = np.lib.stride_tricks.sliding_window_view(source, window)
    slope = (windows * x_centered).sum(axis=1) / denominator
    centered = windows - windows.mean(axis=1, keepdims=True)
    residuals = centered - slope[:, None] * x_centered
    total = (centered**2).sum(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        r_squared = 1.0 - (residuals**2).sum(axis=1) / total

    slope_output = empty.copy()
    r2_output = empty.copy()
    # A perfectly flat window has no variance to explain, so R² is undefined
    # rather than 1 (or 0/0).
    r_squared = np.where(total > 0, r_squared, np.nan)
    slope_output[window - 1 :] = slope
    r2_output[window - 1 :] = r_squared
    return slope_output, r2_output


def _rolling_standard_score(values: pd.Series, window: int) -> pd.Series:
    """Subtract the rolling mean and divide by the rolling standard deviation.

    Used for RSRS: the raw beta hovers around 1 for every stock, so only its
    distance from its own recent distribution carries information, and that
    distance is comparable across stocks while the level is not.
    """

    mean = values.rolling(window, min_periods=window).mean()
    deviation = values.rolling(window, min_periods=window).std()
    return (values - mean) / deviation.replace(0, np.nan)


def calculate_technical_indicators(frame: pd.DataFrame) -> FormulaResult:
    close = pd.Series(numeric_column(frame, "close"), index=frame.index)
    open_ = pd.Series(numeric_column(frame, "open"), index=frame.index)
    high = pd.Series(numeric_column(frame, "high"), index=frame.index)
    low = pd.Series(numeric_column(frame, "low"), index=frame.index)
    volume = pd.Series(numeric_column(frame, "volume"), index=frame.index)
    amount = pd.Series(numeric_column(frame, "amount"), index=frame.index)
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

    # KDJ (9, 3, 3), ported from the reference implementation's ``kdj``. RSV is
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
    # The reference implementation returns (Hn - C) / (Hn - Ln) * 100, so
    # 0 = at the window high and 100 = at the window low. Kept as-is to stay
    # comparable with it.
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

    # --- Volume pressure: VR (24) and the dimensionless OBV derivative ---
    # VR is already a ratio of up-volume to down-volume, so it transfers
    # across stocks unchanged.
    change = close.diff()
    up_volume = volume.where(change > 0, 0.0)
    down_volume = volume.where(change < 0, 0.0)
    flat_volume = volume.where(change == 0, 0.0)
    up_sum = up_volume.rolling(_VR_PERIOD, min_periods=_VR_PERIOD).sum()
    down_sum = down_volume.rolling(_VR_PERIOD, min_periods=_VR_PERIOD).sum()
    flat_sum = flat_volume.rolling(_VR_PERIOD, min_periods=_VR_PERIOD).sum()
    values["vr24"] = (
        safe_divide(
            (up_sum + 0.5 * flat_sum).to_numpy(),
            (down_sum + 0.5 * flat_sum).to_numpy(),
        )
        * 100
    )

    # Raw OBV is a *cumulative* volume total, so its level tracks the size of
    # the float and the reference implementation lists it as non-comparable
    # across symbols for exactly that reason. What is comparable is the share of the window's
    # volume that flowed in on up days: (Σ sign·volume) / Σ volume lands in
    # [-100, 100] no matter how big the stock is. It is also just the 20-day
    # OBV increment, so it keeps the "energy" reading OBV is used for.
    direction = np.sign(change.fillna(0.0))
    signed_volume = (direction * volume).rolling(_OBV_PERIOD, min_periods=_OBV_PERIOD).sum()
    window_volume = volume.rolling(_OBV_PERIOD, min_periods=_OBV_PERIOD).sum()
    values["obv_flow20"] = safe_divide(signed_volume.to_numpy(), window_volume.to_numpy()) * 100

    # --- Trend quality: log-price regression, then three scores built on it ---
    with np.errstate(divide="ignore", invalid="ignore"):
        log_close = np.log(close_values)
    trend_slope, trend_r2 = _rolling_regression(log_close, _TREND_PERIOD)
    values["r2_24"] = trend_r2

    # Annualised exponential trend times its own fit quality: how fast the
    # stock has been compounding, discounted by how noisy the path was.
    #
    # The 24-day slope is extrapolated 252 days out, so a clean run of limit-ups
    # compounds into absurd values (24 bars of +5% → 218,625). That is both a
    # meaningless magnitude and the least sustainable pattern in the market,
    # yet it would dominate a descending ranking. Clip the *log* return before
    # compounding, which bounds the result to [-95%, +1000%] while leaving
    # ordinary trends untouched.
    with np.errstate(over="ignore", invalid="ignore"):
        annualised = (
            np.exp(
                np.clip(trend_slope * _ANNUALISATION, _ANNUAL_TREND_FLOOR, _ANNUAL_TREND_CEILING)
            )
            - 1
        )
    annual_trend = annualised * trend_r2
    values["annual_trend24"] = np.where(np.isfinite(annual_trend), annual_trend, np.nan)

    # The reference trend_quality_momentum: 24-day return × R² / annualised
    # 60-day volatility. Same ingredients, but risk-adjusted, so a volatile
    # stock has to trend that much harder to score well.
    # fill_method=None：缺口/停牌不应被前向填充后当成真实涨跌，这与自定义公式里
    # MA/REF 遇缺口变 NaN 的语义一致；pandas 2.x 的默认 'pad' 也会告警。
    momentum = close.pct_change(_TREND_PERIOD, fill_method=None)
    volatility = (
        close.pct_change(fill_method=None)
        .rolling(_VOLATILITY_PERIOD, min_periods=_VOLATILITY_PERIOD)
        .std()
        * np.sqrt(_ANNUALISATION)
    ).to_numpy()
    values["trend_quality"] = safe_divide((momentum * trend_r2).to_numpy(), volatility)

    # --- RSRS: regress the high on the low, then standardise the slope ---
    # beta = dHigh/dLoy measures how much of a day's range is kept as support
    # rises; it is dimensionless because both axes are prices. Its level is
    # not comparable between stocks though, hence the standard score.
    rolling_corr = high.rolling(_RSRS_PERIOD, min_periods=_RSRS_PERIOD).corr(low)
    beta = rolling_corr * (
        high.rolling(_RSRS_PERIOD, min_periods=_RSRS_PERIOD).std()
        / low.rolling(_RSRS_PERIOD, min_periods=_RSRS_PERIOD).std().replace(0, np.nan)
    )
    values["rsrs_slope"] = beta.to_numpy()
    values["rsrs_zscore"] = _rolling_standard_score(beta, _RSRS_ZSCORE_PERIOD).to_numpy()

    # Longer momentum and trend location complement the existing 20-day views.
    # Keep them as percentages so thresholds remain comparable across symbols.
    values["return60"] = (safe_divide(close_values, close.shift(60).to_numpy()) - 1) * 100
    values["return120"] = (safe_divide(close_values, close.shift(120).to_numpy()) - 1) * 100
    ma60 = values["ma60"]
    ma120 = close.rolling(120, min_periods=120).mean().to_numpy()
    values["bias60"] = safe_divide(close_values - ma60, ma60) * 100
    values["bias120"] = safe_divide(close_values - ma120, ma120) * 100

    # Amount ratio normalises traded money for each symbol. As with
    # volume_ratio5, the reference average excludes today's amount.
    average_amount20 = amount.shift(1).rolling(20, min_periods=20).mean()
    values["amount_ratio20"] = safe_divide(amount.to_numpy(), average_amount20.to_numpy())

    # A rising ratio signals volatility expansion; a falling ratio signals
    # contraction. The common annualisation factor cancels in this ratio.
    daily_returns = close.pct_change(fill_method=None)
    volatility20 = daily_returns.rolling(20, min_periods=20).std()
    volatility60 = daily_returns.rolling(60, min_periods=60).std()
    values["volatility_ratio20_60"] = safe_divide(volatility20.to_numpy(), volatility60.to_numpy())

    # Opening gaps are relevant to next-open execution and are price-scale free.
    values["gap_pct"] = (safe_divide(open_.to_numpy(), close.shift(1).to_numpy()) - 1) * 100

    return build_formula_result(
        formula_id=FORMULA_ID,
        display_name=DISPLAY_NAME,
        minimum_bars=MINIMUM_BARS,
        recommended_bars=RECOMMENDED_BARS,
        frame=frame,
        values=values,
        signals={},
    )
