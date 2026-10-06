"""Constrained candidate generation for strategy discovery."""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Mapping, Sequence
from typing import Literal, cast

from selector_app.portfolio_backtest.models import SINGLE_SECURITY_RANKING_VALUE
from selector_app.screening.models import ScreenCondition
from selector_app.screening.rules import RuleGroup

from .catalog import _PRICE_SCALE_VALUE_IDS, IndicatorCatalog, default_indicator_catalog
from .models import DiscoveryRequest, RiskMode, StrategyCandidate

#: 刻意不提供 ``neq``：参与挖掘的取值几乎都是连续量（RSI、MA、VARO 这类），
#: ``指标 ≠ 30.0`` 在任何一天都近乎恒真，筛选力为零，却会抬高复杂度评分、稀释
#: 真正起作用的那个条件。历史上它贡献了一批看着复杂、实际什么都不做的策略
#: （``VAR1 ≠ 90.0 ALL MA10 ≠ 30.0 ALL MA5 ≤ 10.0``）。要表达"不在某个区间"，
#: 用两条 ``lt`` / ``gt`` 更诚实，也更容易被看清。
_OPERATORS = ("gt", "gte", "lt", "lte", "eq", "cross_up", "cross_down")
_DEFAULT_CONSTANTS = (-1.0, 0.0, 10.0, 30.0, 50.0, 70.0, 90.0)
_RISK_COOLDOWNS = (0, 3, 5, 10)
#: 最长持有交易日数的搜索网格。``None`` = 不限制，留着和时间止损做对照，
#: 否则看不出"限时"本身到底贡献了多少。单位是交易日，与冷却期同口径。
_MAX_HOLD_DAYS: tuple[object, ...] = (None, 20, 40, 60, 120)
_RebalanceFrequency = Literal["daily", "weekly", "monthly"]
_RankOrder = Literal["asc", "desc"]


def _as_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value) if isinstance(value, (int, float, str)) else default
    except (TypeError, ValueError):
        return default


def _as_float(value: object, default: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        return float(value) if isinstance(value, (int, float, str)) else default
    except (TypeError, ValueError):
        return default


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    number = _as_float(value, float("nan"))
    return number if number == number else None


def candidate_hash(value: Mapping[str, object]) -> str:
    payload = json.dumps(
        normalize_candidate_config(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_candidate_config(value: Mapping[str, object]) -> dict[str, object]:
    """Normalize commutative rule members before hashing or deduplication."""

    normalized = dict(value)
    for rule_key in ("buy_rule", "sell_rule"):
        raw_rule = normalized.get(rule_key)
        if not isinstance(raw_rule, Mapping):
            continue
        rule = dict(raw_rule)
        raw_signals = rule.get("signals")
        if isinstance(raw_signals, (list, tuple)):
            rule["signals"] = sorted({str(signal) for signal in raw_signals})
        raw_conditions = rule.get("conditions")
        if isinstance(raw_conditions, (list, tuple)):
            condition_rows = [
                dict(condition) for condition in raw_conditions if isinstance(condition, Mapping)
            ]
            rule["conditions"] = sorted(
                condition_rows,
                key=lambda condition: (
                    str(condition.get("left_value", "")),
                    str(condition.get("operator", "")),
                    str(condition.get("right_value") or ""),
                    str(condition.get("right_constant")),
                ),
            )
        normalized[rule_key] = rule
    return normalized


def _choice(rng: object, values: Sequence[object], default: object) -> object:
    options = list(values)
    if not options:
        return default
    choice = getattr(rng, "choice", None)
    if callable(choice):
        return choice(options)
    suggest = getattr(rng, "suggest_int", None)
    if callable(suggest):
        index = _as_int(suggest("candidate_choice", 0, len(options) - 1), 0)
        return options[index]
    return options[0]


def _int_choice(rng: object, values: Sequence[int], default: int) -> int:
    return _as_int(_choice(rng, values, default), default)


def _condition(
    request: DiscoveryRequest,
    rng: object,
    indicators: Sequence[str],
) -> ScreenCondition:
    left = str(_choice(rng, indicators, indicators[0]))
    operator = str(_choice(rng, _OPERATORS, "gt"))
    right_options = [item for item in indicators if item != left]
    # 价格量纲的输出不配常数右值：``MA60 < 90`` 在复权尺度下等价于按累计分红的
    # 多少筛股，而且这个含义还随历史累积逐年漂移 —— 同一条策略在回测期的 2015 年
    # 和 2026 年判断的根本不是同一把尺子。这类输出只允许和另一个指标比，于是自然
    # 产出 ``C > MA20``、``MA5 > MA20`` 这类价格位置关系（上穿、多头排列），那才
    # 是跨时间稳定的信号。真要表达"离均线多远"，用 bias20 / atr_pct 这些无量纲的
    # 对应量，它们的阈值不随复权漂移。
    if IndicatorCatalog.canonical_id(left) in _PRICE_SCALE_VALUE_IDS and right_options:
        return ScreenCondition(
            left_value=left,
            operator=operator,
            right_value=str(_choice(rng, right_options, indicators[0])),
        )
    use_indicator = bool(right_options) and bool(_choice(rng, (False, True), False))
    if use_indicator:
        return ScreenCondition(
            left_value=left,
            operator=operator,
            right_value=str(_choice(rng, right_options, indicators[0])),
        )
    constants = request.condition_constants.get(left, _DEFAULT_CONSTANTS)
    return ScreenCondition(
        left_value=left,
        operator=operator,
        right_constant=_as_float(_choice(rng, constants, 0.0), 0.0),
    )


def _rule(
    request: DiscoveryRequest,
    rng: object,
    indicators: Sequence[str],
    signals: Sequence[str],
    required_conditions: Sequence[ScreenCondition] = (),
) -> RuleGroup:
    max_conditions = request.max_conditions_per_side
    required = tuple(required_conditions)
    remaining = max(0, max_conditions - len(required))
    if required:
        count = _int_choice(rng, tuple(range(remaining + 1)), 0) if remaining else 0
    elif max_conditions == 0 and not signals:
        count = 1
    else:
        count = _int_choice(rng, tuple(range(1, max_conditions + 1)) or (0,), 1)
    selected_signals: list[str] = []
    conditions: list[ScreenCondition] = []
    for _ in range(count):
        if signals and bool(_choice(rng, (False, True), False)):
            signal = str(_choice(rng, signals, signals[0]))
            if signal not in selected_signals:
                selected_signals.append(signal)
            continue
        conditions.append(_condition(request, rng, indicators))
    if not selected_signals and not conditions and not required:
        conditions.append(_condition(request, rng, indicators))
    generated_count = len(selected_signals) + len(conditions)
    combinable_count = generated_count
    combine_mode = (
        "all" if combinable_count < 2 else str(_choice(rng, ("all", "any", "at_least"), "all"))
    )
    minimum_matches = (
        _int_choice(rng, tuple(range(1, combinable_count + 1)), 1)
        if combine_mode == "at_least"
        else None
    )
    return RuleGroup(
        signals=tuple(selected_signals),
        conditions=tuple(conditions),
        combine_mode=combine_mode,
        minimum_matches=minimum_matches,
        required_conditions=required,
    )


def _risk_values(rng: object) -> tuple[RiskMode, float | None, float | None, int]:
    mode: RiskMode = "atr" if _choice(rng, ("pct", "atr"), "pct") == "atr" else "pct"
    if mode == "atr":
        cooldown = _as_int(_choice(rng, _RISK_COOLDOWNS, 0), 0)
        return mode, None, _as_float(_choice(rng, (1.0, 2.0, 3.0), 1.0), 1.0), cooldown
    percentage = _optional_float(_choice(rng, (None, 0.05, 0.08, 0.12, 0.2), None))
    if percentage is None:
        return mode, None, None, 0
    cooldown = _as_int(_choice(rng, _RISK_COOLDOWNS, 0), 0)
    return mode, percentage, None, cooldown


def _max_hold_days(rng: object) -> int | None:
    value = _choice(rng, _MAX_HOLD_DAYS, None)
    if value is None:
        return None
    days = _as_int(value, 0)
    return days if days > 0 else None


def _build_random_risk_candidate(
    request: DiscoveryRequest,
    rng: object,
) -> StrategyCandidate:
    base_config = dict(request.base_config)
    risk = {prefix: _risk_values(rng) for prefix in ("stop_loss", "take_profit", "trailing_stop")}
    stop_mode, stop_pct, stop_atr, stop_cooldown = risk["stop_loss"]
    take_mode, take_pct, take_atr, take_cooldown = risk["take_profit"]
    trailing_mode, trailing_pct, trailing_atr, trailing_cooldown = risk["trailing_stop"]
    rank_order = str(base_config.get("rank_order") or "desc")
    if rank_order not in {"asc", "desc"}:
        rank_order = "desc"
    frequency = str(base_config.get("rebalance_frequency") or "daily")
    if frequency not in {"daily", "weekly", "monthly"}:
        frequency = "daily"
    max_hold = _max_hold_days(rng)
    base_config["max_hold_days"] = max_hold
    risk_summary = []
    for prefix, label in (
        ("stop_loss", "止损"),
        ("take_profit", "止盈"),
        ("trailing_stop", "移动止盈"),
    ):
        mode, percentage, atr_multiplier, cooldown = risk[prefix]
        if percentage is None and atr_multiplier is None:
            risk_summary.append(f"{label}关闭")
        else:
            setting = f"{percentage:.0%}" if percentage is not None else f"ATR×{atr_multiplier:g}"
            risk_summary.append(f"{label}{setting}/冷却{cooldown}日")
    risk_summary.append("不限持有期" if max_hold is None else f"最长持有{max_hold}交易日")
    return StrategyCandidate(
        # Risk-only candidates carry no generated rules.  ``preserve_base_config``
        # ensures the exact serialized buy/sell rules and ranking survive replay.
        buy_rule=RuleGroup(signals=("risk_optimization.fixed_buy_placeholder",)),
        sell_rule=RuleGroup(signals=("risk_optimization.fixed_sell_placeholder",)),
        ranking_value=(
            SINGLE_SECURITY_RANKING_VALUE
            if base_config.get("single_security_mode") is True
            else str(base_config.get("ranking_value") or "")
        ),
        rank_order=cast(_RankOrder, rank_order),
        max_positions=max(1, _as_int(base_config.get("max_positions"), 5)),
        rebalance_frequency=cast(_RebalanceFrequency, frequency),
        take_profit_mode=take_mode,
        take_profit_pct=take_pct,
        take_profit_atr_multiplier=take_atr,
        take_profit_cooldown_days=take_cooldown,
        stop_loss_mode=stop_mode,
        stop_loss_pct=stop_pct,
        stop_loss_atr_multiplier=stop_atr,
        stop_loss_cooldown_days=stop_cooldown,
        trailing_stop_mode=trailing_mode,
        trailing_stop_pct=trailing_pct,
        trailing_stop_atr_multiplier=trailing_atr,
        trailing_stop_cooldown_days=trailing_cooldown,
        atr_period=_as_int(base_config.get("atr_period"), 14),
        base_config=base_config,
        complexity=sum(
            percentage is not None or atr_multiplier is not None
            for _, percentage, atr_multiplier, _ in risk.values()
        ),
        human_readable_rules=(
            (
                "单股买卖规则；"
                if base_config.get("single_security_mode") is True
                else "固定买卖规则与排序；"
            )
            + "、".join(risk_summary)
        ),
        preserve_base_config=True,
    )


def build_random_candidate(
    request: DiscoveryRequest, rng: object | None = None
) -> StrategyCandidate:
    generator = rng or random.Random()
    if request.candidate_mode == "risk_only":
        return _build_random_risk_candidate(request, generator)
    catalog = default_indicator_catalog()
    indicators = tuple(
        catalog.canonical_id(value_id)
        for value_id in (request.indicator_ids or catalog.rankable_ids)
    )
    if not indicators:
        raise ValueError("没有可用的数值指标")
    signals = request.signal_ids or catalog.signal_ids
    base_config = dict(request.base_config)
    max_positions = _as_int(base_config.get("max_positions", 5), 5)
    frequency_value = str(
        _choice(
            generator,
            ("daily", "weekly", "monthly"),
            base_config.get("rebalance_frequency", "daily"),
        )
    )
    rebalance_frequency = cast(
        _RebalanceFrequency,
        frequency_value if frequency_value in {"daily", "weekly", "monthly"} else "daily",
    )
    risk: dict[str, tuple[RiskMode, float | None, float | None, int]] = {}
    for prefix in ("take_profit", "stop_loss", "trailing_stop"):
        risk[prefix] = _risk_values(generator)
    buy_rule = _rule(
        request,
        generator,
        indicators,
        signals,
        request.required_buy_conditions,
    )
    sell_rule = _rule(
        request,
        generator,
        indicators,
        signals,
        request.required_sell_conditions,
    )
    take_profit_mode, take_profit_pct, take_profit_atr, take_profit_cooldown = risk["take_profit"]
    stop_loss_mode, stop_loss_pct, stop_loss_atr, stop_loss_cooldown = risk["stop_loss"]
    trailing_mode, trailing_pct, trailing_atr, trailing_cooldown = risk["trailing_stop"]
    base_config["max_hold_days"] = _max_hold_days(generator)
    rank_order_value = str(_choice(generator, ("asc", "desc"), "desc"))
    rank_order = cast(
        _RankOrder,
        rank_order_value if rank_order_value in {"asc", "desc"} else "desc",
    )
    # 排序值只能取可排序的那些。请求自带的 indicator_ids 常常连货币计价的输出一起
    # 送来，拿它们排序就等于按股价排序；指标也算在同一份复权序列上，高分红标的被
    # 整体抬高几十倍后，排出来的先后其实比的是累计分红，与强弱无关。
    # 极端情况下（请求里的指标全都不可排序）退回原集合，宁可少一道防线也不要让
    # 候选构造失败。
    rankable = catalog.rankable_among(indicators) or indicators
    candidate = StrategyCandidate(
        buy_rule=buy_rule,
        sell_rule=sell_rule,
        ranking_value=str(_choice(generator, rankable, rankable[0])),
        rank_order=rank_order,
        max_positions=max(1, min(100, max_positions)),
        rebalance_frequency=rebalance_frequency,
        take_profit_mode=take_profit_mode,
        take_profit_pct=take_profit_pct,
        take_profit_atr_multiplier=take_profit_atr,
        take_profit_cooldown_days=take_profit_cooldown,
        stop_loss_mode=stop_loss_mode,
        stop_loss_pct=stop_loss_pct,
        stop_loss_atr_multiplier=stop_loss_atr,
        stop_loss_cooldown_days=stop_loss_cooldown,
        trailing_stop_mode=trailing_mode,
        trailing_stop_pct=trailing_pct,
        trailing_stop_atr_multiplier=trailing_atr,
        trailing_stop_cooldown_days=trailing_cooldown,
        atr_period=14,
        base_config=base_config,
        complexity=(
            len(buy_rule.signals)
            + len(buy_rule.conditions)
            + len(buy_rule.required_conditions)
            + len(sell_rule.signals)
            + len(sell_rule.conditions)
            + len(sell_rule.required_conditions)
            + sum(
                1
                for prefix in ("take_profit", "stop_loss", "trailing_stop")
                if risk[prefix][1] is not None or risk[prefix][2] is not None
            )
        ),
    )
    return candidate
