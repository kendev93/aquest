"""Turning one stored candidate into a replayable portfolio strategy.

Publishing is deliberately mechanical: it takes the configuration of the study
that produced the candidate, drops the candidate's condition into the side it
was mined for, and saves the result as a strategy - **offline**. Nothing here
re-runs a backtest, re-scores the evidence or decides whether the candidate is
any good.

The request is validated with the real ``PortfolioBacktestRequest`` model rather
than a hand-rolled checklist, so anything that cannot be derived from the study
surfaces as a blocker instead of being silently defaulted.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from selector_app.candidate_library.models import (
    SIDE_BUY,
    SIDE_LABELS,
    SIDE_SELL,
    PublishPlan,
    as_mapping,
)

#: 从条件研究的请求里能直接搬到组合回测的字段。研究专属的 min_trades /
#: significance_permutations / candidate_limit 之类刻意不搬——它们描述的是"怎么
#: 挖"，不是"怎么交易"。
_PORTABLE_FIELDS: tuple[str, ...] = (
    "vipdoc_path",
    "universe",
    "instrument_types",
    "boards",
    "formula_text",
    "formula_parameters",
    "date_mode",
    "start_date",
    "end_date",
    "initial_cash",
    "commission",
    "min_commission",
    "stamp_tax",
    "slippage",
    "execution",
    "position_mode",
    "fixed_size",
    "exclude_st",
    "buy_rule",
    "sell_rule",
    "ranking_value",
    "rank_order",
)

#: 发布时允许改的字段。只开这几个：它们决定"买什么、买几只、用多少钱、什么时候
#: 离场"，而研究那边的取值未必是用户想用在策略上的。其余一律沿用来源研究，避免
#: 发布变成第二个配置界面。
#:
#: 止损/止盈之所以必须开放：条件研究允许「持有到期末」当卖出基准（研究里这是有
#: 意义的对比基准），但组合回测不设离场规则就永远不卖、回测没有意义。这种情况
#: 下我们不替人猜一个离场点，而是把它变成一条拦着的提示让人自己填。
OVERRIDE_FIELDS: tuple[str, ...] = (
    "ranking_value",
    "rank_order",
    "max_positions",
    "initial_cash",
    "stop_loss_pct",
    "take_profit_pct",
)


_RISK_FIELDS: tuple[str, ...] = (
    "stop_loss_pct",
    "stop_loss_atr_multiplier",
    "take_profit_pct",
    "take_profit_atr_multiplier",
    "trailing_stop_pct",
    "trailing_stop_atr_multiplier",
)


def _has_no_exit(base: Mapping[str, object]) -> bool:
    """Whether the assembled request would never sell anything.

    研究可以拿「持有到期末」当卖出基准——研究里它是对照组，有意义。策略不行：
    不设离场的回测等于只买不卖，净值曲线说明不了任何事。
    """

    if base.get("sell_rule") is not None or base.get("sell_signal") is not None:
        return False
    return not any(base.get(field) is not None for field in _RISK_FIELDS)


def _condition_key(condition: Mapping[str, object]) -> tuple[object, ...]:
    return (
        condition.get("left_value"),
        condition.get("operator"),
        condition.get("right_value"),
        condition.get("right_constant"),
    )


def _sequence(value: object) -> list[object]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _is_empty_rule(rule: Mapping[str, object]) -> bool:
    if not rule:
        return True
    for key in ("signals", "conditions", "required_conditions"):
        value = rule.get(key)
        if isinstance(value, (list, tuple)) and value:
            return False
    return True


def _rule_with_condition(
    rule: Mapping[str, object] | None,
    condition: Mapping[str, object],
    *,
    side: str,
) -> tuple[dict[str, object], list[str]]:
    """Merge one candidate condition into the rule of the side it was mined for.

    买入候选是 AND 到原有买入条件上的（收窄入场），卖出候选是 OR 上去的（多一个
    离场触发）——后端引擎对卖出口径就是"任一满足"，界面与存储必须同口径，否则发布
    出来的策略跑的不是当初被评估的那条规则。
    """

    label = SIDE_LABELS.get(side, side)
    notes: list[str] = []
    if rule is None or _is_empty_rule(as_mapping(rule)):
        baseline = "持续满仓" if side == SIDE_BUY else "持有到期末"
        notes.append(
            f"来源研究没设{label}规则（当初是{baseline}），这条条件将直接作为{label}规则。"
        )
        return (
            {
                "signals": [],
                "conditions": [dict(condition)],
                "required_conditions": [],
                "combine_mode": "all" if side == SIDE_BUY else "any",
                "minimum_matches": None,
            },
            notes,
        )
    signals = _sequence(rule.get("signals"))
    conditions: list[dict[str, object]] = [
        dict(item) for item in _sequence(rule.get("conditions")) if isinstance(item, Mapping)
    ]
    required: list[dict[str, object]] = [
        dict(item)
        for item in _sequence(rule.get("required_conditions"))
        if isinstance(item, Mapping)
    ]
    merged: dict[str, object] = {
        "signals": signals,
        "conditions": conditions,
        "required_conditions": required,
        "combine_mode": str(rule.get("combine_mode") or "all"),
        "minimum_matches": rule.get("minimum_matches"),
    }
    if any(
        _condition_key(item) == _condition_key(condition)
        for item in conditions
        if isinstance(item, Mapping)
    ):
        notes.append(f"这条条件已经在{label}规则里了，不再重复添加。")
        return merged, notes
    conditions.append(dict(condition))
    if side == SIDE_SELL and merged["combine_mode"] != "any":
        notes.append(
            "卖出规则的组合方式改为「任一满足」：多一个离场触发，而不是要求与原条件同时成立。"
        )
        merged["combine_mode"] = "any"
    else:
        notes.append(f"这条条件已追加到{label}规则（保持原有组合方式）。")
    return merged, notes


def build_publish_plan(
    *,
    research_request: Mapping[str, object],
    condition: Mapping[str, object],
    side: str,
    overrides: Mapping[str, object] | None = None,
) -> PublishPlan:
    """Assemble (and validate) the portfolio request publishing this candidate."""

    from selector_app.web.schemas import PortfolioBacktestRequest

    source = as_mapping(research_request)
    given: Mapping[str, object] = as_mapping(overrides)
    base: dict[str, object] = {}
    for field in _PORTABLE_FIELDS:
        if field in source:
            base[field] = source[field]
    # 研究的「排序取前 N 只」就是组合回测的槽位数；研究允许不带排序规则，组合
    # 回测必须有，所以这一项单独处理（见 blockers）。
    top_n = source.get("ranking_top_n")
    base["max_positions"] = top_n if isinstance(top_n, int) and top_n > 0 else 5

    notes: list[str] = []
    rule_field = "buy_rule" if side == SIDE_BUY else "sell_rule"
    source_rule = source.get(rule_field)
    merged, merge_notes = _rule_with_condition(
        source_rule if isinstance(source_rule, Mapping) else None,
        condition,
        side=side,
    )
    base[rule_field] = merged
    notes.extend(merge_notes)

    for field in OVERRIDE_FIELDS:
        if field in given and given[field] is not None:
            notes.append(f"{field} 由本次发布指定，未沿用来源研究的取值。")
            base[field] = given[field]

    start_date = source.get("start_date")
    end_date = source.get("end_date")
    if start_date and end_date:
        notes.append(f"回测区间沿用来源研究的 {start_date} ~ {end_date}（已固化成绝对日期）。")
    notes.append(f"初始资金沿用来源研究的 {source.get('initial_cash', '默认')}。")

    blockers: list[str] = []
    if not base.get("ranking_value"):
        blockers.append(
            "这次研究没有指定排序规则，发布必须指定一个：组合回测靠它决定命中多只时买哪几只。"
        )
    if _has_no_exit(base):
        blockers.append(
            "这次研究没设卖出规则（当初是持有到期末），但组合回测不设离场规则就永远不卖。"
            "请在发布时指定一个止损或止盈比例——离场点怎么定是策略决定，不替你猜。"
        )
    request_payload: dict[str, Any] = {}
    try:
        # model_validate 而不是 **base：字段是从研究请求、用户覆盖两处拼出来的，
        # 类型只有运行时才确定，逐个参数展开只会让静态检查失去意义。
        model = PortfolioBacktestRequest.model_validate(base)
    except ValidationError as error:
        for item in error.errors():
            message = str(item.get("msg") or "配置无效")
            field = ".".join(str(part) for part in item.get("loc") or ())
            blockers.append(f"{field}: {message}" if field else message)
        return PublishPlan(
            request={}, notes=tuple(notes), blockers=tuple(blockers), base_request=base
        )
    request_payload = model.model_dump(mode="json")
    return PublishPlan(
        request=request_payload,
        notes=tuple(notes),
        blockers=tuple(blockers),
        base_request=base,
    )
