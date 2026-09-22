"""Deflated Sharpe 一脉的数学：MinTRL 的边界与公式形状。

这里锁的是三件容易改错、改错了又不报错的事：

1. **退出点必须是 None 而不是 0 或无穷**——SR ≤ 0、零波动、样本太少都属于
   "算不出来"，调用方要靠 None 退回保守判断。返回 0 会被读成"不需要样本"。
2. **峰度用的是非超额（正态 = 3）**——``scipy`` 默认给超额峰度，传错参数会让
   门槛系统性偏低，而且不会抛异常。
3. **公式形状**——中括号里的 (γ₄−1)/4 写成 γ₄/4 也能跑，只是数不对。
"""

from __future__ import annotations

import numpy as np
import pytest
from scipy.stats import kurtosis, norm, skew

from selector_app.statistics.deflated import (
    MIN_PERIODS_FOR_SHAPE,
    daily_returns,
    min_track_record_length,
)

#: 八个点的手写序列，均值、方差、偏度、峰度都能独立复算。
_SAMPLE = (0.01, -0.02, 0.03, 0.0, 0.02, -0.01, 0.04, -0.03)


def _expected_min_trl(returns: tuple[float, ...] | list[float], *, alpha: float) -> float:
    """独立实现：不用被测函数，直接按论文公式算一遍。"""

    values = np.asarray(returns, dtype=float)
    sharpe = float(values.mean()) / float(values.std(ddof=1))
    gamma3 = float(skew(values, bias=False))
    gamma4 = float(kurtosis(values, fisher=False, bias=False))
    critical = float(norm.ppf(1.0 - alpha))
    return (
        1.0
        + (1.0 - gamma3 * sharpe + ((gamma4 - 1.0) / 4.0) * sharpe**2) * (critical / sharpe) ** 2
    )


def test_formula_matches_an_independent_computation() -> None:
    got = min_track_record_length(_SAMPLE)
    assert got == pytest.approx(_expected_min_trl(_SAMPLE, alpha=0.05))


def test_kurtosis_is_not_the_excess_one() -> None:
    """传成 ``fisher=True`` 会让 γ₄ 少 3，门槛偏低——这里把差异钉住。"""

    got = min_track_record_length(_SAMPLE)
    assert got is not None
    values = np.asarray(_SAMPLE, dtype=float)
    sharpe = float(values.mean()) / float(values.std(ddof=1))
    gamma3 = float(skew(values, bias=False))
    critical = float(norm.ppf(0.95))
    excess_version = (
        1.0
        + (1.0 - gamma3 * sharpe + (float(kurtosis(values, bias=False)) - 1.0) / 4.0 * sharpe**2)
        * (critical / sharpe) ** 2
    )
    assert got == pytest.approx(_expected_min_trl(_SAMPLE, alpha=0.05))
    assert got != pytest.approx(excess_version)


def test_stricter_alpha_needs_more_periods() -> None:
    loose = min_track_record_length(_SAMPLE, alpha=0.10)
    strict = min_track_record_length(_SAMPLE, alpha=0.01)
    assert loose is not None and strict is not None
    assert strict > loose


def test_negative_average_return_has_no_answer() -> None:
    """观察段本来就是亏的，"要多久才能说它显著为正"没有对象。"""

    assert min_track_record_length([-0.01, 0.02, -0.03, 0.01]) is None


def test_zero_volatility_has_no_sharpe() -> None:
    assert min_track_record_length([0.01, 0.01, 0.01, 0.01]) is None


@pytest.mark.parametrize("count", [0, 1, 2, MIN_PERIODS_FOR_SHAPE - 1])
def test_too_few_periods_to_estimate_shape(count: int) -> None:
    assert min_track_record_length([0.01] * count) is None


@pytest.mark.parametrize("alpha", [0.0, 0.5, 1.0, -0.1])
def test_alpha_outside_range_is_rejected(alpha: float) -> None:
    with pytest.raises(ValueError, match="alpha"):
        min_track_record_length(_SAMPLE, alpha=alpha)


def test_non_finite_returns_are_dropped() -> None:
    """一个 NaN 混进曲线会一路画到页面上，这里必须先滤掉。"""

    clean = min_track_record_length(_SAMPLE)
    dirty = min_track_record_length([float("nan"), *_SAMPLE, float("inf")])
    assert clean == pytest.approx(dirty)  # type: ignore[arg-type]


def test_magnitude_is_years_not_days() -> None:
    """年化夏普 0.6 需要约 7.5 年——这个数量级本身就是产品信息。

    有人"优化"公式把它算成几十天，多半是把年化夏普直接当成了日夏普。
    """

    rng = np.random.default_rng(11)
    returns = rng.normal(0.0, 1.0, 6000)
    returns = returns / returns.std(ddof=1) * 0.01
    returns = returns - returns.mean() + (0.6 / np.sqrt(252)) * 0.01
    got = min_track_record_length(returns)
    assert got is not None
    assert 1200 < got < 3000


def test_daily_returns_skips_non_positive_equity() -> None:
    assert daily_returns([100.0, 110.0, 99.0]) == pytest.approx([0.1, -0.1])
    # 从 0 出发算不出收益率，那一段跳过；跌到 0 的那一段（-100%）仍然是有效事实。
    assert daily_returns([100.0, 0.0, 50.0]) == pytest.approx([-1.0])
    assert daily_returns([100.0]) == []
