"""Translate swing-study findings into rules the condition research accepts.

The study answers "which indicator values accompany a swing", which is a
*correlation*, not a trading rule. Three things stand between the two, and the
translation says so instead of quietly picking a default:

**Attribution labels are not tradable.** ``up_leg_start`` marks the first bar of
a leg that later travelled far - but nothing on that bar announces it, so the
label is only knowable in hindsight. A condition mined from it is still legal
to backtest (its features are that day's indicators, no future data leaks in),
it just does not promise the leg's return, because the leg may have crawled for
weeks before running. Only the ``*_onset_ahead`` labels are observable in real
time. Both are emitted, with ``tradeable`` saying which is which.

**The threshold is a starting point, not an answer.** ``threshold_hint`` is the
median of each day's cross-sectional 80th percentile, so it drifts with market
regimes - the RSI that marks the top fifth in a bull market is not the one that
does in a bear market. The condition research re-searches thresholds around it
on train/validation/holdout splits, which is exactly the job it exists for.

**Strongly correlated indicators must not be stacked.** Many of the ranked
outputs are the same signal wearing different hats, so AND-ing the top three
usually yields almost no signals rather than a better one. The strongest single
condition is recommended; the rest are offered separately to try one at a time.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from selector_app.strategy_discovery.catalog import default_indicator_catalog

from .labels import LABEL_DISPLAY_NAMES
from .scoring import IndicatorScore

# Which side of a trade a label speaks to. A label about an upcoming up-leg is
# evidence for buying; one about an upcoming down-leg is evidence for selling.
LABEL_SIDES: dict[str, Literal["buy", "sell"]] = {
    "up_onset_ahead": "buy",
    "down_onset_ahead": "sell",
    "up_leg_start": "buy",
    "down_leg_start": "sell",
}

#: Labels whose onset is observable on the day, so a rule built from them can
#: actually be acted on. The rest are attribution and are emitted as such.
TRADEABLE_LABELS = frozenset({"up_onset_ahead", "down_onset_ahead"})

#: Preferred label per side, most actionable first.
BUY_LABEL_PRIORITY: tuple[str, ...] = ("up_onset_ahead", "up_leg_start")
SELL_LABEL_PRIORITY: tuple[str, ...] = ("down_onset_ahead", "down_leg_start")


def _operator(order: str) -> Literal["gte", "lte"]:
    """Comparison selecting the bucket the score was measured on.

    ``high`` means large indicator values accompanied the label, so the rule
    keeps values at or above the boundary; ``low`` is the mirror.
    """

    return "gte" if order == "high" else "lte"


def _rounded(value: float) -> float:
    """Six significant digits - enough to be exact, short enough to read."""

    return float(f"{value:.6g}")


def _condition_dict(value_id: str, operator: str, threshold: float) -> dict[str, Any]:
    return {
        "left_value": value_id,
        "operator": operator,
        "right_value": None,
        "right_constant": threshold,
    }


def _rule_dict(condition: dict[str, Any]) -> dict[str, Any]:
    """A single-condition RuleGroup, in the shape the research payload uses."""

    return {
        "signals": [],
        "conditions": [condition],
        "required_conditions": [],
        "combine_mode": "all",
        "minimum_matches": None,
    }


@dataclass(frozen=True)
class ConditionTemplate:
    """One study finding, expressed as something the research can take."""

    value_id: str
    display_name: str
    label: str
    label_display: str
    side: Literal["buy", "sell"]
    tradeable: bool
    operator: Literal["gte", "lte"]
    threshold: float
    ic_mean: float
    icir: float | None
    significant: bool
    condition: dict[str, Any]
    rule: dict[str, Any] | None
    #: False when the output is not registered as a screenable value, so the
    #: finding is still worth reading but cannot be pasted into a run.
    condition_supported: bool
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "value_id": self.value_id,
            "display_name": self.display_name,
            "label": self.label,
            "label_display": self.label_display,
            "side": self.side,
            "tradeable": self.tradeable,
            "operator": self.operator,
            "threshold": self.threshold,
            "ic_mean": self.ic_mean,
            "icir": self.icir,
            "significant": self.significant,
            "condition": self.condition,
            "rule": self.rule,
            "condition_supported": self.condition_supported,
            "note": self.note,
        }


def _note(score: IndicatorScore, *, side: str, tradeable: bool, condition_supported: bool) -> str:
    direction = "不高于" if score.order == "low" else "不低于"
    action = "买入" if side == "buy" else "卖出"
    threshold = _rounded(score.threshold_hint or 0.0)
    text = f"{score.display_name} {direction} {threshold} 时考虑{action}"
    text += f"；平均信息系数 {score.ic_mean:.3f}"
    if score.icir is not None:
        text += f"，信息比率 {score.icir:.2f}"
    if not tradeable:
        text = (
            text + "。注意：这条来自「段起点归因」，当天无法确认那是底部，"
            "只能作为参考，不能直接当成可交易信号"
        )
    if not condition_supported:
        text += "。该输出未登记为可筛选指标，暂时只能作为研究结论参考"
    if not tradeable:
        return text
    return (
        text + "。阈值取自横截面分位的中位数，会随行情强弱漂移，"
        "实际取值请让条件研究在自己的三段数据上重新搜索"
    )


def build_templates(scores: Sequence[IndicatorScore]) -> list[ConditionTemplate]:
    """Convert every scored finding that carries a usable threshold.

    Findings whose value is not a registered screenable output still come back
    - they are the ones saying "this custom lever does move with swings" - but
    without a rule, because the condition research would reject the payload.
    """

    screenable = set(default_indicator_catalog().value_ids)
    templates: list[ConditionTemplate] = []
    for score in scores:
        side = LABEL_SIDES.get(score.label)
        if side is None or score.threshold_hint is None:
            continue
        threshold = _rounded(score.threshold_hint)
        operator = _operator(score.order)
        condition = _condition_dict(score.value_id, operator, threshold)
        tradeable = score.label in TRADEABLE_LABELS
        supported = score.value_id in screenable
        templates.append(
            ConditionTemplate(
                value_id=score.value_id,
                display_name=score.display_name,
                label=score.label,
                label_display=LABEL_DISPLAY_NAMES.get(score.label, score.label),
                side=side,
                tradeable=tradeable,
                operator=operator,
                threshold=threshold,
                ic_mean=score.ic_mean,
                icir=score.icir,
                significant=score.significant,
                condition=condition,
                rule=_rule_dict(condition) if supported else None,
                condition_supported=supported,
                note=_note(score, side=side, tradeable=tradeable, condition_supported=supported),
            )
        )
    return templates


def _pick(
    templates: Sequence[ConditionTemplate],
    labels: Sequence[str],
) -> ConditionTemplate | None:
    """Strongest significant template, preferring labels that can be traded.

    Significance outranks raw IC: a large information coefficient that failed
    the noise floor is exactly the false positive this study exists to avoid.
    """

    for label in labels:
        matching = [
            item
            for item in templates
            if item.label == label and item.significant and item.rule is not None
        ]
        if matching:
            return max(matching, key=lambda item: (item.ic_mean, item.icir or 0.0))
    return None


def _rank_order(operator: str) -> Literal["asc", "desc"]:
    """Ranking direction that puts the favoured bucket first."""

    return "desc" if operator == "gte" else "asc"


def build_condition_plan(scores: Sequence[IndicatorScore]) -> dict[str, Any]:
    """Rules and a ranking hint ready to paste into a condition research run.

    The ranking hint comes from *swing* labels, while the condition research
    scores ranking values against realised *returns*. Those targets differ, so
    this is a prior to start from - the research re-scores every ranking value
    on its own signal days and that verdict is the one to trust.
    """

    templates = build_templates(scores)
    buy = _pick(templates, BUY_LABEL_PRIORITY)
    sell = _pick(templates, SELL_LABEL_PRIORITY)

    ranking: dict[str, Any] | None = None
    if buy is not None:
        ranking = {
            "value": buy.value_id,
            "order": _rank_order(buy.operator),
            "display_name": buy.display_name,
            "ic_mean": buy.ic_mean,
            "note": (
                f"按「{buy.label_display}」的信息系数给出先验建议，"
                "最终请以条件研究自己的排序评估为准（它按实际收益率打分，更贴近收益）"
            ),
        }

    reason: str | None = None
    if not templates:
        reason = "没有可转换的结论：所有指标都没算出可用的阈值"
    elif buy is None and sell is None:
        if any(item.significant and item.rule is None for item in templates):
            reason = (
                "最强的结论都来自尚未登记为可筛选指标的输出，暂不能直接带去条件研究；"
                "可以照着表格里的阈值手动配置，或先把该输出登记成指标"
            )
        else:
            reason = (
                "没有任何结论通过显著性检验，不建议把这次结果当作交易规则；"
                "可以放宽区间或改用更大的波段阈值再跑一次"
            )

    return {
        "templates": [item.to_dict() for item in templates],
        "buy_rule": buy.rule if buy is not None else None,
        "sell_rule": sell.rule if sell is not None else None,
        "ranking": ranking,
        "tradeable_count": sum(1 for item in templates if item.tradeable),
        "reason": reason,
    }


__all__ = [
    "BUY_LABEL_PRIORITY",
    "ConditionTemplate",
    "LABEL_SIDES",
    "SELL_LABEL_PRIORITY",
    "TRADEABLE_LABELS",
    "build_condition_plan",
    "build_templates",
]
