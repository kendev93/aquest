"""Cross-type comparison of archived research runs.

对外只需要两个东西：常量与数据结构（``models``）和编排入口（``service``）。
取数细节在 ``extract``，不对外暴露——它跟着各研究类型的结果结构走，改的人
本来就该同时改它的单测。
"""

from __future__ import annotations

from .models import (
    CURVE_KIND_EQUITY,
    CURVE_KIND_FACTOR_IC,
    CURVE_KIND_FACTOR_LONG_SHORT,
    MAX_COMPARE_ITEMS,
    MIN_COMPARE_ITEMS,
    TYPE_LABELS,
    CompareCurve,
    CompareItem,
    CompareMetric,
    HistoryCompareReport,
)
from .service import HistoryCompareService

__all__ = [
    "CURVE_KIND_EQUITY",
    "CURVE_KIND_FACTOR_IC",
    "CURVE_KIND_FACTOR_LONG_SHORT",
    "MAX_COMPARE_ITEMS",
    "MIN_COMPARE_ITEMS",
    "TYPE_LABELS",
    "CompareCurve",
    "CompareItem",
    "CompareMetric",
    "HistoryCompareReport",
    "HistoryCompareService",
]
