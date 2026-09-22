"""Named baskets of instruments (股票组) and their observation curves.

入口是 :class:`~selector_app.stock_groups.service.StockGroupService`；曲线数学在
``performance.py``，口径与组合回测的基准共用同一份实现。
"""

from __future__ import annotations

from .models import (
    GROUP_CURVE_LABEL,
    MARKET_CURVE_LABEL,
    MAX_ITEMS,
    MAX_LOOKBACK_DAYS,
    MIN_TRADING_DAYS,
    NAME_MAX_LENGTH,
    NOTE_MAX_LENGTH,
    SOURCE_LABELS,
    SOURCE_MANUAL,
    SOURCE_SCREEN,
    BasketCurve,
    GroupMember,
    GroupPerformance,
    MemberSpread,
    normalize_members,
)
from .performance import (
    equal_weight_curve,
    excess_return,
    member_spread,
    pivot_closes,
    price_ratios,
)
from .service import StockGroupService

__all__ = [
    "GROUP_CURVE_LABEL",
    "MARKET_CURVE_LABEL",
    "MAX_ITEMS",
    "MAX_LOOKBACK_DAYS",
    "MIN_TRADING_DAYS",
    "NAME_MAX_LENGTH",
    "NOTE_MAX_LENGTH",
    "SOURCE_LABELS",
    "SOURCE_MANUAL",
    "SOURCE_SCREEN",
    "BasketCurve",
    "GroupMember",
    "GroupPerformance",
    "MemberSpread",
    "StockGroupService",
    "equal_weight_curve",
    "excess_return",
    "member_spread",
    "normalize_members",
    "pivot_closes",
    "price_ratios",
]
