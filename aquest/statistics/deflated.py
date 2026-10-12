"""Deflated Sharpe Ratio 一脉（Bailey & López de Prado）的数学。

两件事同出一源，所以放在同一个模块里，避免长出两份不一致的实现：

* **MinTRL（最小跟踪记录长度）**——策略追踪用（候选池 #21）。反解「按你现在的
  表现，还要多少期才能在 95% 下说它显著为正」。
* **期望最优 Sharpe / PSR**——累计试验次数校正用（候选池 #20）。回答「试过这么
  多次之后，这个成绩还值钱吗」。#20 接线时在这里加。

为什么不直接用年化 Sharpe 去比：公式里的 SR̂ 必须与**观测频率一致**（日频收益
率就配日频 Sharpe），否则 n 的量纲对不上。年化是展示层的换算，进公式前要还原。
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from scipy.stats import kurtosis, norm, skew

#: 偏度、峰度至少要这么多期才勉强可估。四阶矩在小样本下不稳定到没有意义，
#: 低于这个数一律给 None——给个算出来的数会让人误以为算过了。
MIN_PERIODS_FOR_SHAPE = 4

#: 显著性的默认一侧水平。95% 是这一族文献的惯例，也和我们在用的置换门槛一致。
DEFAULT_ALPHA = 0.05


def min_track_record_length(
    returns: Sequence[float],
    *,
    alpha: float = DEFAULT_ALPHA,
) -> float | None:
    """按当前表现反解「还要多少期」才谈得上显著（MinTRL）。

    :param returns: 与观测频率一致的**收益率**序列（日频就传日收益率）。
        权益曲线要先自己转成收益率再传进来，本函数不猜频率。
    :param alpha: 允许的假阳性水平，默认 0.05。
    :returns: 需要的观测期数（与 ``returns`` 同频率）；``None`` 表示算不出来
        ——样本太少、零波动、或者平均收益不为正。调用方拿到 ``None`` 必须退回
        保守判断，不能当成「不需要样本」。

    ``MinTRL = 1 + [1 − γ₃·SR̂ + (γ₄−1)/4·SR̂²] · (Z_α / SR̂)²``

    三个退出点都是有意的，不是异常：

    * **SR̂ ≤ 0**——观察段本来就没跑赢无风险利率，谈「显著为正」没有对象。
      公式在 SR̂→0 时会发散到无穷，直接返回 None 比返回一个天文数字有用。
    * **零波动**——分母为 0，Sharpe 无定义（一条直线没有「显著」这回事）。
    * **形状项为负**——极端偏度/峰度下中括号可能翻负，此时公式失效，宁可不给。

    注意这里用的是**非超额**峰度（正态 = 3），``scipy`` 默认给的是超额峰度
    （正态 = 0），所以要显式传 ``fisher=False``。传错会让门槛系统性偏低。
    """

    if not 0.0 < alpha < 0.5:
        raise ValueError(f"alpha 必须在 (0, 0.5) 内，收到 {alpha}")

    values = np.asarray(list(returns), dtype=float)
    values = values[np.isfinite(values)]
    if values.size < MIN_PERIODS_FOR_SHAPE:
        return None

    spread = float(values.std(ddof=1))
    if not np.isfinite(spread) or spread <= 0.0:
        return None

    sharpe = float(values.mean()) / spread
    if not np.isfinite(sharpe) or sharpe <= 0.0:
        return None

    gamma3 = float(skew(values, bias=False))
    gamma4 = float(kurtosis(values, fisher=False, bias=False))
    shape = 1.0 - gamma3 * sharpe + ((gamma4 - 1.0) / 4.0) * sharpe**2
    if not np.isfinite(shape) or shape <= 0.0:
        return None

    critical = float(norm.ppf(1.0 - alpha))
    needed = 1.0 + shape * (critical / sharpe) ** 2
    if not np.isfinite(needed) or needed < 1.0:
        return None
    return float(needed)


def daily_returns(totals: Sequence[float]) -> list[float]:
    """权益曲线 → 日收益率。权益非正的区段没有收益率可言，直接跳过。"""

    values = [float(value) for value in totals]
    out: list[float] = []
    for previous, current in zip(values, values[1:]):
        if previous > 0.0:
            out.append(current / previous - 1.0)
    return out
