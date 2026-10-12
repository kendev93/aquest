"""Benchmark curves: what the strategy has to beat, not just a number to look at.

口径（自己定的，别抄别人的）：**标的池等权买入持有**。

- 标的池 = 本次回测真正参与模拟的那批标的（候选池），不是某个外部指数。
- 在回测窗口首日按收盘价等权买入全部标的，一直持有到窗口最后一天。
- 不计佣金、印花税、滑点，也不考虑整手与现金拖累：基准是**研究标尺**，
  不是可交易策略。给它加摩擦只会让比较两边口径不一致。
- 窗口首日没有价格的标的直接排除（新股、停牌），之后缺失的交易日沿用前收盘价。
- **价格用分红送转校正后的序列**（分红再投资口径的总回报）：持有者会收到现金
  分红，用不复权价会把这部分收益全部漏掉，基准被系统性低估，"跑赢基准"就成了
  虚言。基准也因此与模拟侧刻意保持不同口径——模拟撮合、整手、ATR 一律用不复权
  价，那是另一件事，两边各答各的问题。

为什么不用沪深300：库里已经没有指数数据（本系统只研究股票），而且"跑赢一个
自己挑的指数"很容易变成幸存者偏差式的自我安慰——真正该回答的问题是「这套
选股规则，是不是比把这批股票随便各买一点然后不动要强」。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

import numpy as np
import pandas as pd

from aquest.backtest.performance import PerformanceAnalyzer

from .context import _StockSeries

BENCHMARK_LABEL = "标的池等权买入持有"

#: 基准绩效只保留与策略可比的那几项。成交笔数、胜率这类对"买入不动"没有意义，
#: 留着只会让人误以为基准也在交易。
_BENCHMARK_METRIC_KEYS = (
    "total_return",
    "annual_return",
    "max_drawdown",
    "max_dd_duration",
    "sharpe",
    "sortino",
    "calmar",
    "volatility",
    "start_cash",
    "end_value",
)

_EMPTY_TRADES = pd.DataFrame(
    columns=[
        "datetime",
        "direction",
        "size",
        "price",
        "commission",
        "slippage",
        "pnl",
        "cost_basis",
        "rejected",
    ]
)


@dataclass(frozen=True)
class BenchmarkCurve:
    """一条基准净值曲线，与策略的资金曲线同起点、同交易日。"""

    label: str
    instrument_count: int
    dates: tuple[int, ...]
    totals: tuple[float, ...]
    performance: Mapping[str, float | None]

    def to_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "instrument_count": self.instrument_count,
            "dates": list(self.dates),
            "totals": list(self.totals),
            "performance": dict(self.performance),
        }


def equal_weight_buy_hold(
    contexts: Sequence[_StockSeries],
    dates: Sequence[int],
    initial_cash: float,
) -> BenchmarkCurve | None:
    """等权买入持有基准。算不出来（没有可用标的 / 窗口太短）就返回 None。"""

    if not contexts or len(dates) < 2 or not np.isfinite(initial_cash) or initial_cash <= 0:
        return None
    grid = np.asarray(dates, dtype=np.int64)
    accumulated = np.zeros(len(grid), dtype=float)
    count = 0
    for context in contexts:
        ratio = _price_ratio(context, grid)
        if ratio is None:
            continue
        accumulated += ratio
        count += 1
    if count == 0:
        return None
    totals = accumulated / count * float(initial_cash)
    return BenchmarkCurve(
        label=BENCHMARK_LABEL,
        instrument_count=count,
        dates=tuple(int(value) for value in grid),
        totals=tuple(float(value) for value in totals),
        performance=curve_performance(totals),
    )


def _price_ratio(context: _StockSeries, grid: np.ndarray) -> np.ndarray | None:
    """把一只股票的价格按窗口交易日对齐，再归一化成"相对首日的倍数"。

    取 :attr:`_StockSeries.adjusted_closes`（分红送转校正后）而不是成交用的
    ``closes``：基准要回答"一直拿着会怎样"，而持有者会拿到分红。用不复权价会让
    基准漏掉全部分红，被系统性低估——策略相对它的"超额"因此虚高。这是刻意让
    基准与模拟分走两个口径：**模拟成交必须用不复权价**（股数、整手、ATR 才成立），
    **基准必须用复权价**（总回报才成立），两边各答各的问题。
    """

    own_dates = np.asarray(context.dates, dtype=np.int64)
    if own_dates.size == 0:
        return None
    adjusted = context.adjusted_closes
    closes = np.asarray(context.closes if adjusted is None else adjusted, dtype=float)
    positions = np.searchsorted(grid, own_dates)
    inside = positions < len(grid)
    if not bool(inside.any()):
        return None
    safe = np.clip(positions, 0, len(grid) - 1)
    matched = inside & (grid[safe] == own_dates)
    if not bool(matched.any()):
        return None
    series = np.full(len(grid), np.nan, dtype=float)
    series[positions[matched]] = closes[matched]

    # 首日必须有价。窗口开始之后才出现的标的（新股、长期停牌）不进基准池：
    # 用后面才有的价格回填，等于默认"上市之前就买得到"，把幸存者偏差塞进基准。
    first = float(series[0])
    if not np.isfinite(first) or first <= 0:
        return None
    valid = np.isfinite(series) & (series > 0)
    # 缺失日沿用前一个有效价（停牌期间价格不变，不是收益为零）。
    indices = np.where(valid, np.arange(len(series)), 0)
    np.maximum.accumulate(indices, out=indices)
    return np.asarray(series[indices], dtype=float) / first


def curve_performance(totals: np.ndarray) -> Mapping[str, float | None]:
    """绩效口径的唯一出处。

    股票组的观察曲线走的也是等权买入持有，必须和这里的数字能直接对上——两边各
    算一套就会出现「同一只票在两页上最大回撤不一样」，而这种差异没人会去查。
    """

    frame = pd.DataFrame({"total": np.asarray(totals, dtype=float)})
    metrics = PerformanceAnalyzer(frame, _EMPTY_TRADES).compute()
    return {key: _finite_number(metrics.get(key)) for key in _BENCHMARK_METRIC_KEYS}


def _finite_number(value: object) -> float | None:
    number = float(cast(float, value))
    return number if np.isfinite(number) else None


__all__ = ["BENCHMARK_LABEL", "BenchmarkCurve", "curve_performance", "equal_weight_buy_hold"]
