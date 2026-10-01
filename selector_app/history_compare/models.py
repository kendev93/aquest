"""Shared shapes for the cross-type backtest history comparison.

这里只放数据结构与口径常量，不放任何取数逻辑——取数在 ``extract.py``，
编排在 ``service.py``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# 曲线口径。不同口径绝不叠绘：把「资金曲线」和「累计 IC」画进同一张图，读图的
# 人一定会把它们当成同一件事，而它们的量纲和含义都不相同。
# ---------------------------------------------------------------------------
CURVE_KIND_EQUITY = "equity"
CURVE_KIND_FACTOR_LONG_SHORT = "factor_long_short"
CURVE_KIND_FACTOR_IC = "factor_ic"

CURVE_KIND_LABELS: dict[str, str] = {
    CURVE_KIND_EQUITY: "资金曲线",
    CURVE_KIND_FACTOR_LONG_SHORT: "多空累计价差",
    CURVE_KIND_FACTOR_IC: "累计 IC",
}
#: y 轴单位。资金曲线归一后是「相对本金的倍数」，界面上再换算成百分比。
CURVE_UNIT_LABELS: dict[str, str] = {
    CURVE_KIND_EQUITY: "累计收益率",
    CURVE_KIND_FACTOR_LONG_SHORT: "价差",
    CURVE_KIND_FACTOR_IC: "累计 IC",
}

NormalizeMode = Literal["capital", "first"]
XAxisMode = Literal["date", "index"]

#: 少于 2 条没有对比可言；多于 5 条曲线叠在一起已经读不出来了（颜色都不够分）。
MIN_COMPARE_ITEMS = 2
MAX_COMPARE_ITEMS = 5

#: 样本长度差到这个倍数就提醒。长样本天然更容易撞上极端回撤，不提示的话
#: 「回撤更大」会被读成「策略更差」，而它可能只是跑得更久。
SAMPLE_LENGTH_WARNING_RATIO = 3.0

#: 单条曲线回传的点数上限。组合回测跑十年是两千多点，五条一起就是上万点，
#: 浏览器端画到那个密度已经看不出差别了——等距抽样并注明。
MAX_CURVE_POINTS = 1200

#: 键覆盖 BacktestHistoryType 的全部成员。这里刻意不标注成
#: dict[BacktestHistoryType, str]：历史记录的 backtest_type 从库里读出来是 str，
#: 收紧键类型会让每个 .get() 调用点都要先做窄化，代价远大于收益。改用
#: tests/unit/test_backtest_history_types.py 断言两处集合一致。
TYPE_LABELS: dict[str, str] = {
    "single_stock": "单股回测",
    "portfolio": "组合回测",
    "strategy_fitness": "策略适配性",
    "condition_research": "条件研究",
    "swing_research": "波段研究",
    "individual_research": "个股研究",
    "factor_test": "因子检验",
    "factor_attribution": "因子归因",
}

# 统一指标行：行顺序即界面顺序。跨类型时这些行**不是同一个口径**，
# 每个值都带 source，界面必须把来源显示出来。
METRIC_TOTAL_RETURN = "total_return"
METRIC_ANNUAL_RETURN = "annual_return"
METRIC_MAX_DRAWDOWN = "max_drawdown"
METRIC_SHARPE = "sharpe"
METRIC_WIN_RATE = "win_rate"
METRIC_TRADE_COUNT = "trade_count"
METRIC_SAMPLE_DAYS = "sample_days"
METRIC_INSTRUMENT_COUNT = "instrument_count"
METRIC_SIGNIFICANT_COUNT = "significant_count"

METRIC_LABELS: dict[str, str] = {
    METRIC_TOTAL_RETURN: "总收益",
    METRIC_ANNUAL_RETURN: "年化收益",
    METRIC_MAX_DRAWDOWN: "最大回撤",
    METRIC_SHARPE: "夏普",
    METRIC_WIN_RATE: "胜率",
    METRIC_TRADE_COUNT: "交易笔数",
    METRIC_SAMPLE_DAYS: "样本交易日",
    METRIC_INSTRUMENT_COUNT: "标的数",
    METRIC_SIGNIFICANT_COUNT: "显著/通过数",
}
METRIC_UNITS: dict[str, str] = {
    METRIC_TOTAL_RETURN: "percent",
    METRIC_ANNUAL_RETURN: "percent",
    METRIC_MAX_DRAWDOWN: "percent",
    METRIC_SHARPE: "ratio",
    METRIC_WIN_RATE: "percent",
    METRIC_TRADE_COUNT: "count",
    METRIC_SAMPLE_DAYS: "count",
    METRIC_INSTRUMENT_COUNT: "count",
    METRIC_SIGNIFICANT_COUNT: "count",
}
METRIC_ORDER: tuple[str, ...] = (
    METRIC_TOTAL_RETURN,
    METRIC_ANNUAL_RETURN,
    METRIC_MAX_DRAWDOWN,
    METRIC_SHARPE,
    METRIC_WIN_RATE,
    METRIC_TRADE_COUNT,
    METRIC_SAMPLE_DAYS,
    METRIC_INSTRUMENT_COUNT,
    METRIC_SIGNIFICANT_COUNT,
)

UNIT_PERCENT = "percent"
UNIT_RATIO = "ratio"
UNIT_COUNT = "count"
UNIT_TEXT = "text"


@dataclass(frozen=True)
class CompareMetric:
    """One number with the context needed to read it honestly."""

    key: str
    label: str
    value: float | None
    unit: str
    source: str
    text: str | None = None
    note: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "unit": self.unit,
            "source": self.source,
        }
        if self.text is not None:
            payload["text"] = self.text
        if self.note is not None:
            payload["note"] = self.note
        return payload


@dataclass(frozen=True)
class CompareCurve:
    """One normalized series ready to be drawn.

    ``points`` 里同时带了 ``date`` 和 ``index``，前端切换 x 轴不用再请求一次——
    两种坐标只是取数方式不同，数据本身没变。
    """

    kind: str
    kind_label: str
    unit: str
    label: str
    normalize: str
    points: tuple[dict[str, object], ...]
    note: str | None = None
    truncated: bool = False

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "kind": self.kind,
            "kind_label": self.kind_label,
            "unit": self.unit,
            "label": self.label,
            "normalize": self.normalize,
            "points": [dict(point) for point in self.points],
            "truncated": self.truncated,
        }
        if self.note is not None:
            payload["note"] = self.note
        return payload


@dataclass(frozen=True)
class CompareItem:
    """One history record projected into what the comparison page needs."""

    history_id: str
    #: 历史 ID 的前 8 位。同一个区间、同一批标的的回测可以跑很多次，标题会一字不差
    #: （真机上六条组合回测的标题完全一样），图例上没有这个短码就分不清谁是谁。
    short_id: str
    backtest_type: str
    type_label: str
    label: str
    subtitle: str
    created_at: str
    finished_at: str
    date_range: dict[str, object]
    curve: CompareCurve | None
    curve_unavailable_reason: str | None
    metrics: tuple[CompareMetric, ...]
    native_metrics: tuple[CompareMetric, ...]

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "history_id": self.history_id,
            "short_id": self.short_id,
            "backtest_type": self.backtest_type,
            "type_label": self.type_label,
            "label": self.label,
            "subtitle": self.subtitle,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "date_range": dict(self.date_range),
            "curve": self.curve.to_dict() if self.curve is not None else None,
            "curve_unavailable_reason": self.curve_unavailable_reason,
            "metrics": [metric.to_dict() for metric in self.metrics],
            "native_metrics": [metric.to_dict() for metric in self.native_metrics],
        }
        return payload


@dataclass(frozen=True)
class HistoryCompareReport:
    """The whole comparison: items, what got plotted, and what to warn about."""

    normalize: str
    x_axis: str
    items: tuple[CompareItem, ...]
    curve_group: dict[str, object]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "normalize": self.normalize,
            "x_axis": self.x_axis,
            "items": [item.to_dict() for item in self.items],
            "curve_group": dict(self.curve_group),
            "warnings": list(self.warnings),
        }
