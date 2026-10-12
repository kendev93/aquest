"""Folded-row maths: what each group did over the last few weeks.

和 ``performance.py`` 的区别不是"算少一点"，是**换了可比的单位**：

* 那边算「入组至今」——每组起点不同，所以组与组之间不可比，而且每组都得单独
  扫一遍从入组日到今天的全市场行情；
* 这边算「近 N 个交易日」——所有组共用同一个窗口、同一份行情、一次算完。

折叠态要的正是后者：一眼扫过去谁涨谁跌，且可以横向比。展开才值得付「入组至今」
那份代价。两组数字不同源，UI 必须写明哪个是哪个，否则会被当成同一个东西对比。

窗口按**交易日**数而不是自然日：自然日会把假期算进去，春节那两周每周只有三个
交易日，"近 20 个交易日"和"近 20 天"就不是一个意思了。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

import pandas as pd

from .models import (
    MARKET_CURVE_LABEL,
    MIN_TRADING_DAYS,
    GroupMember,
)
from .performance import equal_weight_curve, excess_return

#: 折叠态默认给的两个窗口（交易日）。20 ≈ 一个月，60 ≈ 一个季度。
DEFAULT_SUMMARY_WINDOWS: Final[tuple[int, ...]] = (20, 60)
#: 窗口上限。窗口越长要读的行情越多，不加限制的话一个 ?windows=5000 就能把请求挂死。
MAX_SUMMARY_WINDOW: Final = 250
#: 一次最多算几个窗口。每多一个窗口只是多切一次同一份矩阵，成本不高，但也没必要
#: 让人一次要二十个。
MAX_SUMMARY_WINDOWS: Final = 3
WINDOW_LABELS: Final[dict[int, str]] = {5: "近 1 周", 20: "近 1 月", 60: "近 3 月"}


@dataclass(frozen=True)
class WindowReturn:
    """One group's equal-weight return over one trailing window."""

    window: int
    #: 窗口里实际拿到几个交易日。不足窗口长度时数字照给，但 ``complete`` 是 False。
    trading_days: int
    group_return: float | None
    market_return: float | None
    #: 组减全市场。**差值不是比值**：两条曲线都能取负值，负值相除的结论是错的。
    excess_return: float | None
    #: 真正进了等权曲线的只数（起点无价的被剔掉了）。
    counted: int
    complete: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "window": self.window,
            "trading_days": self.trading_days,
            "group_return": self.group_return,
            "market_return": self.market_return,
            "excess_return": self.excess_return,
            "counted": self.counted,
            "complete": self.complete,
        }


@dataclass(frozen=True)
class GroupSummary:
    """The folded-row numbers of one group."""

    group_id: str
    windows: tuple[WindowReturn, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "group_id": self.group_id,
            "windows": [item.to_dict() for item in self.windows],
        }


def normalize_windows(values: Sequence[int] | None) -> tuple[int, ...]:
    """Clamp requested windows instead of rejecting them.

    窗口只是"看多久"，不是用户的研究输入：给个离谱的值按上限截掉比报 422 有用。
    """

    if not values:
        return DEFAULT_SUMMARY_WINDOWS
    cleaned: list[int] = []
    for value in values:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number <= 0:
            continue
        clamped = min(number, MAX_SUMMARY_WINDOW)
        if clamped not in cleaned:
            cleaned.append(clamped)
        if len(cleaned) >= MAX_SUMMARY_WINDOWS:
            break
    return tuple(cleaned) if cleaned else DEFAULT_SUMMARY_WINDOWS


def window_return(
    market_prices: pd.DataFrame,
    members: Sequence[GroupMember],
    window: int,
) -> WindowReturn | None:
    """Equal-weight return of ``members`` over the last ``window`` trading days.

    全市场对照用的是同一个窗口、同一份矩阵的**全部**标的，所以两边可以直接相减；
    组那一侧先按成员取子集，起点无价的照样剔除（口径与 performance 一致）。
    """

    if market_prices.empty:
        return None
    window_prices = market_prices.tail(window)
    days = int(len(window_prices.index))
    if days < MIN_TRADING_DAYS:
        return None
    # reindex 而不是直接取列：成员里有库里根本没有的票时，缺列会让后面的等权
    # 静默少算几只。补成全 NaN 列，price_ratios 会当"起点无价"剔掉并如实计数。
    keys = [f"{member.market}:{member.code}" for member in members]
    group_curve = equal_weight_curve(window_prices.reindex(columns=keys), "组")
    market_curve = equal_weight_curve(window_prices, MARKET_CURVE_LABEL)
    return WindowReturn(
        window=window,
        trading_days=days,
        group_return=_total_return(group_curve),
        market_return=_total_return(market_curve),
        excess_return=excess_return(group_curve, market_curve),
        counted=group_curve.instrument_count if group_curve else 0,
        complete=days >= window,
    )


def summarize_groups(
    market_prices: pd.DataFrame,
    groups: Mapping[str, Sequence[GroupMember]],
    windows: Sequence[int],
) -> tuple[GroupSummary, ...]:
    """Every group's numbers, from one already-loaded price matrix."""

    summaries: list[GroupSummary] = []
    for group_id, members in groups.items():
        entries = [
            item
            for item in (window_return(market_prices, members, w) for w in windows)
            if item is not None
        ]
        summaries.append(GroupSummary(group_id=group_id, windows=tuple(entries)))
    return tuple(summaries)


def _total_return(curve: object) -> float | None:
    """从曲线里取累计收益。走 getattr 是因为 ``None`` 也是一种合法输入。"""

    performance = getattr(curve, "performance", None)
    if not isinstance(performance, Mapping):
        return None
    value = performance.get("total_return")
    if value is None:
        return None
    return float(value)


__all__ = [
    "DEFAULT_SUMMARY_WINDOWS",
    "MAX_SUMMARY_WINDOW",
    "MAX_SUMMARY_WINDOWS",
    "WINDOW_LABELS",
    "GroupSummary",
    "WindowReturn",
    "normalize_windows",
    "summarize_groups",
    "window_return",
]
