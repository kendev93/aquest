"""Basket observation maths: what a group of stocks did after it was saved.

口径（和组合回测的基准严格一致，见 ``portfolio_backtest/benchmark.py``）：

- **等权买入持有**：起点按收盘价每只各买等额金额，之后不动，一直拿到最后一天。
  不是每日再平衡——再平衡会让权重每天往回拉，那是另一种策略，不该混进观察里。
- **起点必须有价**：起点当天没有收盘价的标的直接出局（新股、停牌）。用后面才有的
  价格回填等于默认「停牌之前也买得到」，把幸存者偏差塞进结论。
- **缺失日沿用前收盘价**：停牌期间价格没动，收益就是 0，不是跳过。
- **不计摩擦**：这是观察标尺不是可交易策略，加佣金印花税只会让两条曲线的口径
  不一致。

全市场对照用的是同一个函数、同一套规则，所以两边的数字可以直接相减。这条不是
洁癖：两边各算一套的话，同一只票在「组合回测」和「股票组」两页上会给出不同的
最大回撤，而这种差异没人会去查。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Final

import numpy as np
import pandas as pd

from selector_app.portfolio_backtest.benchmark import curve_performance

from .models import (
    MIN_TRADING_DAYS,
    PERMANENT_GAP_DAYS,
    BasketCurve,
    GroupMember,
    MemberSpread,
)

_INITIAL_CASH: Final = 1.0


def pivot_closes(frame: pd.DataFrame) -> pd.DataFrame:
    """Turn a long ``market/code/trade_date/close`` frame into a date x symbol matrix.

    列键用 ``市场:代码`` 拼成字符串而不是 MultiIndex：后面要按成员列表取子集、
    要跟成员的名字对上，字符串键比元组键省一堆转换。
    """

    # 存储层统一把 trade_date 重命名成 date（见 store_base._frame_from_query），
    # 两种名字都认，免得换了调用入口就静默返回空。
    if frame.empty:
        return pd.DataFrame()
    date_column = "date" if "date" in frame.columns else "trade_date"
    if date_column not in frame.columns:
        return pd.DataFrame()
    work = pd.DataFrame(
        {
            "day": _as_day_numbers(frame[date_column]),
            "key": frame["market"].astype(str).str.upper() + ":" + frame["code"].astype(str),
            "close": pd.to_numeric(frame["close"], errors="coerce"),
        }
    )
    work = work.dropna(subset=["day", "key"])
    if work.empty:
        return pd.DataFrame()
    wide = work.groupby(["day", "key"], sort=True)["close"].mean().unstack("key")
    return wide.sort_index()


def equal_weight_curve(prices: pd.DataFrame, label: str) -> BasketCurve | None:
    """等权买入持有曲线。算不出来（无可用标的 / 窗口太短）返回 None。"""

    ratios = price_ratios(prices)
    if ratios.empty or len(ratios.index) < MIN_TRADING_DAYS:
        return None
    totals = ratios.mean(axis=1).to_numpy(dtype=float) * _INITIAL_CASH
    return BasketCurve(
        label=label,
        instrument_count=int(ratios.shape[1]),
        dates=tuple(int(value) for value in ratios.index),
        totals=tuple(float(value) for value in totals),
        performance=curve_performance(totals),
    )


def price_ratios(prices: pd.DataFrame) -> pd.DataFrame:
    """每只票的价格归一化成「相对起点收盘价的倍数」，并剔除起点无价的。"""

    if prices.empty or len(prices.index) < MIN_TRADING_DAYS:
        return pd.DataFrame()
    first = prices.iloc[0]
    usable = first.notna() & (first > 0)
    if not bool(usable.any()):
        return pd.DataFrame()
    kept = prices.loc[:, usable]
    # 缺失日沿用前一个有效价：停牌期间价格不变，不是收益为零也不是跳过。
    filled = kept.ffill()
    return filled.divide(kept.iloc[0], axis=1)


def member_spread(
    prices: pd.DataFrame,
    members: Sequence[GroupMember],
) -> MemberSpread | None:
    """组内每只票区间涨跌幅的分布。"""

    ratios = price_ratios(prices)
    if ratios.empty:
        return None
    names = {f"{member.market}:{member.code}": member.name for member in members}
    last = ratios.iloc[-1]
    returns = (last - 1.0).dropna()
    if returns.empty:
        return None
    best_key = str(returns.idxmax())
    worst_key = str(returns.idxmin())
    return MemberSpread(
        counted=int(returns.size),
        advanced=int((returns > 0).sum()),
        declined=int((returns < 0).sum()),
        median_return=_finite(float(returns.median())),
        best=_extreme(best_key, names, float(returns.max())),
        worst=_extreme(worst_key, names, float(returns.min())),
    )


def stalled_members(
    prices: pd.DataFrame,
    members: Sequence[GroupMember],
) -> tuple[dict[str, object], ...]:
    """末次有价之后连续缺失 ``PERMANENT_GAP_DAYS`` 个交易日以上的标的。

    曲线**不因此改数**：摘牌的票按末次收盘价挂着是「那笔钱还在里面、且变现不了」
    的诚实写法，改写成「按末次价卖出、份额转给剩下的票」等于替用户做了一笔他没
    做的交易——这和「起点无价就出局」是同一条原则的镜像（不假设没发生的成交）。
    自由度只在披露：让「这组里有 2 只是已经没有价的」这件事看得见，要不要把它们
    移出组由用户决定。

    只判末段：中间的长空档是停牌，价格确实没动，沿用前收盘价是对的。
    """

    if prices.empty:
        return ()
    has_price = np.asarray(prices.notna().to_numpy(dtype=bool))
    total_rows = has_price.shape[0]
    names = {f"{member.market}:{member.code}": member.name for member in members}
    found: list[dict[str, object]] = []
    for column, key in enumerate(prices.columns):
        rows = np.flatnonzero(has_price[:, column])
        if rows.size == 0:
            # 整段都没有价：那是「起点无价」，归 excluded 管，不是这里的事。
            continue
        missing = total_rows - 1 - int(rows[-1])
        if missing < PERMANENT_GAP_DAYS:
            continue
        market, _, code = str(key).partition(":")
        found.append(
            {
                "market": market,
                "code": code,
                "name": names.get(str(key)),
                "last_date": format_day(prices.index[int(rows[-1])]),
                "missing_days": missing,
            }
        )
    return tuple(found)


def excess_return(
    group_curve: BasketCurve | None,
    market_curve: BasketCurve | None,
) -> float | None:
    """组相对全市场的超额，**用差值不是比值**。

    两条曲线的累计收益都可能为负：负 5% 与负 10% 相除会读出「差两倍」这种毫无
    意义甚至符号翻转的结论，相减才是「多赚/少亏了 5 个百分点」。
    """

    if group_curve is None or market_curve is None:
        return None
    group_total = group_curve.performance.get("total_return")
    market_total = market_curve.performance.get("total_return")
    if group_total is None or market_total is None:
        return None
    return _finite(float(group_total) - float(market_total))


def format_day(day: object) -> str | None:
    """统一成 ``YYYY-MM-DD``。

    入参有两种来源：行情矩阵的索引是 YYYYMMDD 整数，窗口计算出来的起止却是
    ``date``。只认前者会让后者一律变成 ``None``——页面上就少一段日期，看不出观察
    的是哪一段，所以两种都得认。
    """

    if isinstance(day, datetime):
        return day.date().isoformat()
    if isinstance(day, date):
        return day.isoformat()
    text = str(day).strip()
    if not text.isdigit():
        return None
    padded = text.zfill(8)
    return f"{padded[:4]}-{padded[4:6]}-{padded[6:8]}"


def _extreme(key: str, names: Mapping[str, str | None], value: float) -> dict[str, object]:
    market, _, code = key.partition(":")
    return {
        "market": market,
        "code": code,
        "name": names.get(key),
        "return": _finite(value),
    }


def _finite(value: float) -> float | None:
    return value if np.isfinite(value) else None


def _as_day_numbers(values: pd.Series) -> pd.Series:
    """把各种日期形态统一成 YYYYMMDD 整数，跟其他页面的曲线轴保持一致。"""

    if pd.api.types.is_numeric_dtype(values):
        return pd.to_numeric(values, errors="coerce")
    parsed = pd.to_datetime(values, errors="coerce")
    return (parsed.dt.year * 10000 + parsed.dt.month * 100 + parsed.dt.day).astype("Int64")


__all__ = [
    "equal_weight_curve",
    "excess_return",
    "format_day",
    "member_spread",
    "pivot_closes",
    "price_ratios",
    "stalled_members",
]
