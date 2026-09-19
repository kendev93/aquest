from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from selector_app.formulas.indicator_one import calculate_indicator_one
from selector_app.formulas.indicator_three import calculate_indicator_three
from selector_app.formulas.indicator_two import calculate_indicator_two
from selector_app.formulas.tdx_math import BARSLAST, COUNT, CROSS, EMA, HHV, LLV, REF, SMA
from selector_app.formulas.technical import (
    RECOMMENDED_BARS,
    calculate_technical_indicators,
)


def make_bars(length: int = 180, *, descending: bool = False) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=length, freq="D")
    if descending:
        close = np.linspace(300.0, 20.0, length)
    else:
        close = 20.0 + np.sin(np.arange(length) / 7.0) * 2.0 + np.arange(length) * 0.04
    low = close - 1.0
    high = close + 1.0
    return pd.DataFrame(
        {
            "date": dates,
            "open": close - 0.2,
            "high": high,
            "low": low,
            "close": close,
            "volume": np.linspace(100_000.0, 200_000.0, length),
            "amount": close * np.linspace(100_000.0, 200_000.0, length),
        }
    )


def make_trend_bars(length: int = 320) -> pd.DataFrame:
    """Deterministic pseudo-random bars with a *varying* intraday range.

    ``make_bars`` keeps ``high = close + 1`` and ``low = close - 1``, which
    degenerates the RSRS regression: high is an exact shift of low, so beta is
    a constant 1, its rolling standard deviation is 0 and the standard score
    would be NaN everywhere.
    """

    rng = np.random.default_rng(20260917)
    close = 30.0 * np.cumprod(1.0 + rng.normal(0.0003, 0.016, length))
    spread = close * rng.uniform(0.004, 0.05, length)
    high = close + spread * rng.uniform(0.2, 0.9, length)
    low = close - spread * rng.uniform(0.2, 0.9, length)
    volume = rng.uniform(80_000.0, 900_000.0, length)
    return pd.DataFrame(
        {
            "date": pd.bdate_range("2023-01-02", periods=length),
            "open": close,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": close * volume,
        }
    )


def test_mytt_core_functions_have_expected_warmup_and_cross_semantics() -> None:
    values = np.arange(1.0, 8.0)

    assert np.isnan(REF(values, 1)[0])
    assert not np.isnan(SMA(values, 3, 1)[0])
    assert not np.isnan(EMA(values, 3)[0])
    assert np.isnan(LLV(values, 3)[1])
    assert np.isnan(HHV(values, 3)[1])
    assert CROSS(np.array([1.0, 1.0, 3.0]), np.array([2.0, 2.0, 2.0])).tolist() == [
        False,
        False,
        True,
    ]
    assert BARSLAST(np.array([False, False, True, False])).tolist() == [1, 2, 0, 1]
    assert COUNT(np.array([True, False, True, True]), 3)[-1] == 2


@pytest.mark.parametrize(
    ("calculator", "minimum_bars", "expected_signals"),
    [
        (
            calculate_indicator_one,
            33,
            {"main_force_entry", "wash", "main_force_raise", "distribution"},
        ),
        (
            calculate_indicator_two,
            34,
            {
                "start",
                "end",
                "saturation_hot",
                "new_high_breakout",
                "short_above_mid_long",
                "short_below_mid_long",
            },
        ),
        (
            calculate_indicator_three,
            34,
            {"prepare_rally", "suppress_main", "accumulation_zone", "begin_zone", "end_zone"},
        ),
    ],
)
def test_each_formula_returns_structured_intermediates_and_signals(
    calculator, minimum_bars: int, expected_signals: set[str]
) -> None:
    result = calculator(make_bars())

    assert result.minimum_bars == minimum_bars
    assert result.sufficient_data is True
    assert expected_signals.issubset(result.signals)
    assert expected_signals == set(result.last_signal_state)
    assert len(result.values) >= 5
    assert result.last_indicator_values
    assert all(len(array) == 180 for array in result.values.values())


def test_formula_results_mark_empty_and_insufficient_data_as_skipped() -> None:
    empty = make_bars(0)
    short = make_bars(10)

    for calculator in (calculate_indicator_one, calculate_indicator_two, calculate_indicator_three):
        empty_result = calculator(empty)
        short_result = calculator(short)
        assert empty_result.sufficient_data is False
        assert short_result.sufficient_data is False
        assert all(value is False for value in empty_result.last_signal_state.values())
        assert all(value is False for value in short_result.last_signal_state.values())


def test_constant_price_data_does_not_produce_infinite_values() -> None:
    """一字板行情（最高=最低）会让位置类指标的分母为 0，必须产出 NaN 而非 inf。"""

    bars = make_bars()
    for column in ("open", "high", "low", "close"):
        bars[column] = 10.0

    calculators = (
        calculate_indicator_one,
        calculate_indicator_two,
        calculate_indicator_three,
        calculate_technical_indicators,
    )
    for calculator in calculators:
        result = calculator(bars)
        assert all(not np.isinf(array).any() for array in result.values.values())


def test_indicator_one_keeps_the_negative_min_denominator() -> None:
    result = calculate_indicator_one(make_bars(descending=True))
    denominator = result.values["var21_denominator"]
    var21 = result.values["var21"]

    negative_indexes = np.flatnonzero(denominator < 0)
    assert negative_indexes.size > 0
    finite_negative_indexes = [i for i in negative_indexes if np.isfinite(var21[i])]
    assert finite_negative_indexes
    assert var21[finite_negative_indexes[-1]] < 0


def test_indicator_two_keeps_the_explicit_ref_twenty_gap() -> None:
    bars = make_bars()
    result = calculate_indicator_two(bars)
    a = result.values["a"]
    expected = np.zeros(len(a))
    for index in range(len(a)):
        if index < 20:
            expected[index] = np.nan
            continue
        expected[index] = sum((20 - offset) * a[index - offset] for offset in range(19))
        expected[index] += a[index - 20]
        expected[index] /= 210

    assert np.allclose(result.values["x"][20:], expected[20:], equal_nan=True)
    with_ref_19 = result.values["x"] + 19 * REF(a, 19)
    assert not np.allclose(result.values["x"][30:], with_ref_19[30:], equal_nan=True)


def test_indicator_three_uses_fractional_sma_period_3_2() -> None:
    bars = make_bars()
    result = calculate_indicator_three(bars)
    base = result.values["stochastic_base"]
    expected_inner = SMA(base, 5, 1)
    expected = 4 * expected_inner - 3 * SMA(expected_inner, 3.2, 1)

    assert np.allclose(result.values["var1"], expected, equal_nan=True)


def test_screenshot_named_outputs_are_present() -> None:
    one = calculate_indicator_one(make_bars())
    two = calculate_indicator_two(make_bars())

    assert np.all(one.values["z"] == 0)
    assert all(name in two.values for name in ("short_line", "mid_line", "bottom_line"))
    assert np.all(two.values["warning_line"] == 85)


def test_formulas_do_not_use_future_rows() -> None:
    original = make_bars()
    changed = original.copy(deep=True)
    changed.loc[len(changed) - 1, ["open", "high", "low", "close", "amount"]] = [
        999.0,
        1000.0,
        998.0,
        999.5,
        999_500_000.0,
    ]

    for calculator in (
        calculate_indicator_one,
        calculate_indicator_two,
        calculate_indicator_three,
        # KDJ/布林/威廉/CCI 都是滚动窗口，同样不能回看未来。
        calculate_technical_indicators,
    ):
        before = calculator(original)
        after = calculator(changed)
        for name in before.values:
            assert np.allclose(before.values[name][:-1], after.values[name][:-1], equal_nan=True)


def test_only_zero_centred_outputs_are_marked_bidirectional() -> None:
    """页面用 ``> 0`` / ``< 0`` 做默认买卖条件，只有零轴输出两侧才都成立。

    ``indicator_one.wash`` 实测 22,886 根 bar 中从不为负，因此旧的默认卖出
    条件 ``wash < 0`` 恒不成立；这条测试锁住“哪些输出可以安全地这样用”。
    """

    from selector_app.formulas.registry import BIDIRECTIONAL_VALUE_IDS, FORMULA_REGISTRY

    metadata = FORMULA_REGISTRY.metadata()
    flagged = {
        value["id"]
        for indicator in metadata
        for value in indicator["values"]
        if value["bidirectional"]
    }

    assert flagged == set(BIDIRECTIONAL_VALUE_IDS)
    assert flagged == {
        "technical.macd",
        "technical.macd_signal",
        "technical.macd_histogram",
        # 围绕 0 的百分比，两侧由构造保证而非实测。
        "technical.bias20",
        "technical.return20",
        "technical.cci14",
        # 趋势质量族：净流向可正可负，年化趋势/趋势质量继承斜率与收益的符号，
        # RSRS 标准分按构造零均值。
        "technical.obv_flow20",
        "technical.annual_trend24",
        "technical.trend_quality",
        "technical.rsrs_zscore",
    }
    # 一侧恒为 0 的输出绝不能进入默认条件。
    assert "indicator_one.wash" not in flagged
    assert "indicator_one.distribution" not in flagged
    # 恒为正的比值/有界位置同样只能单向。
    for one_sided in (
        "technical.atr_pct",
        "technical.volume_ratio5",
        "technical.position20",
        "technical.boll_bandwidth",
        # WR 与 KDJ 的 K/D 都是 0~100 的有界量，没有负侧。
        "technical.wr14",
        "technical.kdj_k",
        "technical.kdj_d",
        # VR 是上涨量/下跌量之比，R² 恒在 0~1，都不跨零。
        "technical.vr24",
        "technical.r2_24",
    ):
        assert one_sided not in flagged


def test_dimensionless_outputs_match_hand_computed_values() -> None:
    """五个无量纲输出的手工核对。

    ``make_bars`` 的 high/low 恒为 close ± 1，且日内涨幅远小于 1，因此真实波幅
    ``max(high-low, |high-prev_close|, |low-prev_close|)`` 恒等于 2 —— 这让 ATR
    有一个不需要复制实现就能算出的精确期望值。
    """

    bars = make_bars(180)
    result = calculate_technical_indicators(bars)
    close = bars["close"]
    last = close.iloc[-1]

    ma20 = close.rolling(20).mean().iloc[-1]
    assert result.values["bias20"][-1] == pytest.approx((last - ma20) / ma20 * 100)
    assert result.values["return20"][-1] == pytest.approx((last / close.iloc[-21] - 1) * 100)

    window_low = bars["low"].iloc[-20:].min()
    window_high = bars["high"].iloc[-20:].max()
    assert result.values["position20"][-1] == pytest.approx(
        (last - window_low) / (window_high - window_low) * 100
    )
    # 真实波幅恒为 2，ATR 收敛到 2。
    assert result.values["atr_pct"][-1] == pytest.approx(2.0 / last * 100)

    volume = bars["volume"]
    # 均量窗口不含当日：这是量比不被自身成交抬高的关键。
    assert result.values["volume_ratio5"][-1] == pytest.approx(
        volume.iloc[-1] / volume.iloc[-6:-1].mean()
    )


def test_volume_ratio_ignores_the_current_bar_and_never_looks_ahead() -> None:
    """当日放量只应改变当日的量比，不能回改历史。"""

    bars = make_bars(180)
    before = calculate_technical_indicators(bars)

    inflated = bars.copy(deep=True)
    inflated.loc[len(inflated) - 1, "volume"] = bars["volume"].iloc[-1] * 10
    after = calculate_technical_indicators(inflated)

    assert after.values["volume_ratio5"][-1] == pytest.approx(
        before.values["volume_ratio5"][-1] * 10
    )
    assert np.allclose(
        before.values["volume_ratio5"][:-1],
        after.values["volume_ratio5"][:-1],
        equal_nan=True,
    )


def test_kdj_bollinger_wr_and_cci_match_hand_computed_values() -> None:
    """第二批无量纲输出的手工核对，期望值全部用 pandas 原语独立算出。

    KDJ 的 K/D 用递推平滑，抄实现就失去意义，所以这里只复算 RSV 再交给
    ``ewm``；CCI 的分母刻意用 ``rolling().apply`` 复算，与实现里的滑动窗口
    版本互为交叉验证。
    """

    bars = make_bars(180)
    result = calculate_technical_indicators(bars)
    close = bars["close"]
    high = bars["high"]
    low = bars["low"]

    kdj_low = low.rolling(9).min()
    kdj_high = high.rolling(9).max()
    rsv = (close - kdj_low) / (kdj_high - kdj_low) * 100
    expected_k = rsv.ewm(alpha=1 / 3, adjust=False).mean()
    expected_d = expected_k.ewm(alpha=1 / 3, adjust=False).mean()
    assert np.allclose(result.values["kdj_k"], expected_k, equal_nan=True)
    assert np.allclose(result.values["kdj_d"], expected_d, equal_nan=True)
    assert np.allclose(result.values["kdj_j"], 3 * expected_k - 2 * expected_d, equal_nan=True)
    # 前 8 根没有 9 日区间，必须是 NaN 而不是 0。
    assert np.isnan(result.values["kdj_k"][:8]).all()

    middle = close.rolling(20).mean()
    deviation = close.rolling(20).std()
    upper = middle + 2 * deviation
    lower = middle - 2 * deviation
    assert np.allclose(
        result.values["boll_percent_b"],
        (close - lower) / (upper - lower) * 100,
        equal_nan=True,
    )
    assert np.allclose(
        result.values["boll_bandwidth"], (upper - lower) / middle * 100, equal_nan=True
    )

    wr_low = low.rolling(14).min()
    wr_high = high.rolling(14).max()
    assert np.allclose(
        result.values["wr14"], (wr_high - close) / (wr_high - wr_low) * 100, equal_nan=True
    )

    typical = (high + low + close) / 3
    mean_deviation = typical.rolling(14).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    assert np.allclose(
        result.values["cci14"],
        (typical - typical.rolling(14).mean()) / (0.015 * mean_deviation),
        equal_nan=True,
    )


def test_volume_pressure_and_trend_quality_match_hand_computed_values() -> None:
    """第三批（量能 + 趋势质量）手工核对。

    期望值全部用与实现无关的办法复算：回归用 ``np.polyfit`` /
    ``np.corrcoef``（实现用的是闭式解 + 滑动窗口），RSRS 用逐窗口
    ``polyfit(low, high)``（实现用的是 ``corr × std比``），互为交叉验证。
    """

    bars = make_trend_bars()
    result = calculate_technical_indicators(bars)
    close = bars["close"]
    high = bars["high"]
    low = bars["low"]
    volume = bars["volume"]

    change = close.diff()
    up = volume.where(change > 0, 0.0).rolling(24).sum()
    down = volume.where(change < 0, 0.0).rolling(24).sum()
    flat = volume.where(change == 0, 0.0).rolling(24).sum()
    assert np.allclose(
        result.values["vr24"], (up + 0.5 * flat) / (down + 0.5 * flat) * 100, equal_nan=True
    )

    signed = (np.sign(change.fillna(0.0)) * volume).rolling(20).sum()
    assert np.allclose(
        result.values["obv_flow20"], signed / volume.rolling(20).sum() * 100, equal_nan=True
    )
    # 净流向是有界的：无论流通盘多大都落在 ±100 之间。
    assert np.nanmin(result.values["obv_flow20"]) >= -100.0
    assert np.nanmax(result.values["obv_flow20"]) <= 100.0

    x = np.arange(24)
    expected_slope = close.rolling(24).apply(lambda y: np.polyfit(x, np.log(y), 1)[0], raw=True)
    expected_r2 = close.rolling(24).apply(lambda y: np.corrcoef(x, np.log(y))[0, 1] ** 2, raw=True)
    assert np.allclose(result.values["r2_24"], expected_r2, equal_nan=True, atol=1e-9)
    assert np.allclose(
        result.values["annual_trend24"],
        (np.exp(expected_slope * 252.0) - 1.0) * expected_r2,
        equal_nan=True,
    )

    momentum = close.pct_change(24)
    volatility = close.pct_change().rolling(60).std() * np.sqrt(252.0)
    assert np.allclose(
        result.values["trend_quality"], momentum * expected_r2 / volatility, equal_nan=True
    )

    expected_beta = np.full(len(close), np.nan)
    low_windows = np.lib.stride_tricks.sliding_window_view(low.to_numpy(), 18)
    high_windows = np.lib.stride_tricks.sliding_window_view(high.to_numpy(), 18)
    for index, (window_low, window_high) in enumerate(zip(low_windows, high_windows)):
        expected_beta[index + 17] = np.polyfit(window_low, window_high, 1)[0]
    assert np.allclose(result.values["rsrs_slope"], expected_beta, equal_nan=True)

    beta = pd.Series(expected_beta)
    expected_zscore = (beta - beta.rolling(250).mean()) / beta.rolling(250).std()
    assert np.allclose(result.values["rsrs_zscore"], expected_zscore, equal_nan=True)
    # 标准分要 18 根回归预热 + 250 根标准化窗口，第一个有效值在第 267 根（索引 266）。
    assert np.isnan(result.values["rsrs_zscore"][:266]).all()
    assert not np.isnan(result.values["rsrs_zscore"][266:]).any()


def test_flat_window_yields_nan_instead_of_inf_for_kdj_and_bollinger() -> None:
    """一字板（区间宽度为 0）时所有带分母的输出必须是 NaN。"""

    bars = make_bars(60)
    bars.loc[:, ["open", "high", "low", "close"]] = 10.0

    result = calculate_technical_indicators(bars)

    for name in (
        "kdj_k",
        "kdj_d",
        "kdj_j",
        "boll_percent_b",
        "wr14",
        "cci14",
        # 无量纲方差为 0：R²、年化趋势、趋势质量、RSRS 全部无定义。
        "r2_24",
        "annual_trend24",
        "trend_quality",
        "rsrs_slope",
        "rsrs_zscore",
    ):
        values = result.values[name]
        assert np.isnan(values).all(), name
        assert not np.isinf(values).any(), name
    # 带宽是 (上轨 − 下轨) / 中轨，无波动时答案就是 0，不是除零。
    assert np.all(result.values["boll_bandwidth"][19:] == 0.0)
    # VR 把平盘量各分一半给分子分母，所以完全平盘时恰好是 100（买卖平衡），
    # 不是除零；净流向是 0/总量 = 0。
    assert np.all(result.values["vr24"][23:] == 100.0)
    assert np.all(result.values["obv_flow20"][19:] == 0.0)


def test_dimensionless_outputs_are_usable_as_cross_stock_ranking_features() -> None:
    """补这批指标的目的就是让挖掘池和排序有可跨股票比较的输入。"""

    from selector_app.strategy_discovery.catalog import default_indicator_catalog

    catalog = default_indicator_catalog()
    rankable = set(catalog.rankable_ids)
    for name in (
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
    ):
        assert f"technical.{name}" in rankable
        # 短名别名也必须能解析，否则校验放行但解析失败。
        assert catalog.canonical_id(name) == f"technical.{name}"

    # 价格量纲的均线/MACD 仍然被排除。
    for excluded in ("technical.ma20", "technical.macd"):
        assert excluded not in rankable


def test_recommended_bars_covers_the_longest_warmup() -> None:
    """建议预热根数必须覆盖最长窗口，否则会有输出长时间为空。

    RSRS 标准分是瓶颈：18 根回归出 beta，再要 250 根才能标准化。以后加更长
    窗口的输出而忘了同步 ``RECOMMENDED_BARS``，这个断言会失败。
    """

    for bars in (make_bars(RECOMMENDED_BARS), make_trend_bars(RECOMMENDED_BARS)):
        result = calculate_technical_indicators(bars)
        unfinished = sorted(name for name, values in result.values.items() if np.isnan(values[-1]))
        assert not unfinished, unfinished


def test_annual_trend_is_capped_before_compounding() -> None:
    """24 根各 +5% 外推一年会得 21 万倍，必须封顶。

    这种「连续涨停」恰恰是最不可持续的形态，不封顶时按分值降序排会长期
    霸占第一，也会让分位阈值挖出「annual_trend24 > 1000」这种废条件。
    """

    length = 300
    close = np.concatenate([np.full(length - 24, 10.0), 10.0 * np.cumprod(np.full(24, 1.05))])
    bars = pd.DataFrame(
        {
            "date": pd.bdate_range("2023-01-02", periods=length),
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": np.full(length, 1_000_000.0),
            "amount": close * 1_000_000.0,
        }
    )

    values = calculate_technical_indicators(bars).values["annual_trend24"]

    assert np.isfinite(values[-1])
    # 上界 +1000%（log(11) 取整后略有余量）。
    assert values[-1] == pytest.approx(10.0, abs=0.01)
    assert np.nanmax(values) <= 10.01
    # 下界 -95%，不会外推出小于 -100% 的收益。
    assert np.nanmin(values) >= -0.96


def test_custom_formula_continuous_outputs_are_screenable() -> None:
    """三个自定义公式的连续无量纲输出要能在选股页和条件研究里直接用。

    常量、脉冲、价纲那部分已经排除在外，但连续部分是实测最强的横截面特征
    （建仓强度在波段研究里拿过全场第一），不进目录就只能看、不能用。
    """

    from selector_app.strategy_discovery.catalog import default_indicator_catalog

    catalog = default_indicator_catalog()
    rankable = set(catalog.rankable_ids)
    for value_id in (
        "indicator_one.var3",
        "indicator_two.saturation",
        "indicator_two.holding",
        "indicator_three.stochastic_base",
        "indicator_three.varo7",
    ):
        assert value_id in rankable
        # 目录不带中文名的话，下拉里会露出一串 VARO7 之类的原名。
        assert catalog.display_name(value_id) != value_id.split(".", 1)[1]
