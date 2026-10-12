"""Shared vocabulary for named baskets of instruments (股票组).

一个组解决的是「选完了然后呢」：公式选股一次能选出几十只票，导出成 CSV 之后
这批票就再没有下文了——它到底涨没涨、有没有跑赢大盘，只能靠人自己隔几天回来
对一遍。组把这批票留下来，并给出一条可核对的观察曲线。

两个刻意的边界：

* **组不交易。** 它不产生买卖信号、不模拟调仓，只回答「这批票从入组那天的收盘
  价算起，拿到现在是什么样」。想看带调仓的结果那是组合回测的事。
* **起点是入组那天，不是信号日。** 观察的是「我什么时候决定关注它」，而不是
  「它什么时候满足条件」。后者在组里混了不同来源的股票时根本无法定义。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

NAME_MAX_LENGTH: Final = 60
NOTE_MAX_LENGTH: Final = 300
#: 一组最多放多少只。与组合回测 codes 的上限取同一个数。
MAX_ITEMS: Final = 200

SOURCE_MANUAL: Final = "manual"
SOURCE_SCREEN: Final = "screen"
SOURCE_LABELS: Final[dict[str, str]] = {
    SOURCE_MANUAL: "手动勾选",
    SOURCE_SCREEN: "选股结果",
}

#: 观察窗口的自然日上限。组可以放很久，但每次算曲线都要扫一遍全市场，不加限制
#: 的话一个放了五年的组能把请求挂死。截断会明说，不悄悄缩短。
MAX_LOOKBACK_DAYS: Final = 365 * 5
#: 曲线至少要两个交易日才有「涨跌」可言。
MIN_TRADING_DAYS: Final = 2
#: 末次有价之后连续缺这么多交易日，就判定这只票**不再有价**（摘牌，或长期停牌不归）。
#:
#: 为什么是 20 个交易日（≈ 一个自然月）：
#: * A 股退市整理期是 15 个交易日，摘牌之后行情里就再也没有它了——这段空白必然
#:   超过 20 个交易日，一定会被判出来。
#: * 反向要防的是误判：重大资产重组停牌制度上限 10 个交易日，其他停牌也很少超过
#:   一个月。20 留了一倍缓冲，正常停牌不会被当成消失。
#: * 数**交易日**不是自然日：行情表里压根没有停牌日的行，数自然日会把假期算进去。
#: * 只看**末段**不看中间：中间的长空档（停牌几个月又复牌）价格确实没动，沿用前
#:   收盘价是对的；只有末段的空档才说明它不再回来。
PERMANENT_GAP_DAYS: Final = 20

GROUP_CURVE_LABEL: Final = "本组等权买入持有"
MARKET_CURVE_LABEL: Final = "全市场等权买入持有"


@dataclass(frozen=True)
class GroupMember:
    """One instrument inside a group."""

    market: str
    code: str
    name: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.market, self.code)

    def to_dict(self) -> dict[str, object]:
        return {"market": self.market, "code": self.code, "name": self.name}


def normalize_members(items: Sequence[Mapping[str, object]]) -> tuple[GroupMember, ...]:
    """Turn raw API members into de-duplicated members, keeping input order.

    顺序有意义：用户勾选的先后就是他想看到的样子，不能被 SQL 的返回顺序改写。
    重复按 (market, code) 去掉——同一只票出现两次会让等权权重悄悄翻倍。
    """

    members: list[GroupMember] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        market = str(item.get("market") or "").strip().upper()
        code = str(item.get("code") or "").strip()
        if not market or not code:
            continue
        key = (market, code)
        if key in seen:
            continue
        seen.add(key)
        raw_name = item.get("name")
        name = None if raw_name is None else (str(raw_name).strip() or None)
        members.append(GroupMember(market=market, code=code, name=name))
    return tuple(members)


@dataclass(frozen=True)
class BasketCurve:
    """一条等权买入持有净值曲线。"""

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


@dataclass(frozen=True)
class MemberSpread:
    """组内各标的区间涨跌幅的分布。

    给这个是因为「整体涨了 8%」有两种完全不同的成因：30 只票每只涨 8%，或者 2 只
    涨了 120%、28 只在跌。不看分布的话这两种长得一模一样。
    """

    counted: int
    advanced: int
    declined: int
    median_return: float | None
    best: Mapping[str, object] | None
    worst: Mapping[str, object] | None

    def to_dict(self) -> dict[str, object]:
        return {
            "counted": self.counted,
            "advanced": self.advanced,
            "declined": self.declined,
            "median_return": self.median_return,
            "best": dict(self.best) if self.best else None,
            "worst": dict(self.worst) if self.worst else None,
        }


@dataclass(frozen=True)
class GroupPerformance:
    """What one group looks like from the day it was saved until now."""

    group_id: str
    start_date: str | None
    end_date: str | None
    trading_days: int
    group_curve: BasketCurve | None
    market_curve: BasketCurve | None
    #: 组相对全市场的超额。**差值不是比值**：两条曲线都能取负值，负值相除会读出
    #: 完全错误的倍数关系（详见 docs 里的统计口径）。
    excess_return: float | None
    spread: MemberSpread | None
    #: 入组首日没有收盘价、因此没进曲线的标的。不能静默丢掉——「这一组算了多少只」
    #: 是判断结论可信度的一部分。
    excluded: tuple[dict[str, object], ...]
    #: 中途不再有价的标的（摘牌、或长期停牌不归）。曲线仍然沿用末次收盘价：
    #: 摘牌后进老三板那个价变现不了，改写成「按末次价卖出」等于替用户做了一笔他
    #: 没做的交易。所以这里只点名、不改数，让它可见。
    stalled: tuple[dict[str, object], ...]
    truncated: bool
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "group_id": self.group_id,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "trading_days": self.trading_days,
            "group_curve": self.group_curve.to_dict() if self.group_curve else None,
            "market_curve": self.market_curve.to_dict() if self.market_curve else None,
            "excess_return": self.excess_return,
            "spread": self.spread.to_dict() if self.spread else None,
            "excluded": [dict(item) for item in self.excluded],
            "stalled": [dict(item) for item in self.stalled],
            "truncated": self.truncated,
            "notes": list(self.notes),
        }


__all__ = [
    "GROUP_CURVE_LABEL",
    "MARKET_CURVE_LABEL",
    "MAX_ITEMS",
    "MAX_LOOKBACK_DAYS",
    "MIN_TRADING_DAYS",
    "NAME_MAX_LENGTH",
    "NOTE_MAX_LENGTH",
    "PERMANENT_GAP_DAYS",
    "SOURCE_LABELS",
    "SOURCE_MANUAL",
    "SOURCE_SCREEN",
    "BasketCurve",
    "GroupMember",
    "GroupPerformance",
    "MemberSpread",
    "normalize_members",
]
