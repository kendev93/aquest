"""Parent-candidate reconstruction and local mutation for strategy refinement."""

from __future__ import annotations

import random
from collections.abc import Mapping
from dataclasses import replace
from typing import Literal, cast

from selector_app.screening.models import ScreenCondition
from selector_app.screening.rules import RuleGroup

from .candidates import _as_float, _as_int, _choice, _condition
from .catalog import default_indicator_catalog
from .models import CandidateMode, DiscoveryRequest, RiskMode, StrategyCandidate

RefinementRadius = Literal["medium"]


def discovery_request_from_dict(value: Mapping[str, object]) -> DiscoveryRequest:
    """Rebuild the normalized parent request stored in experiment history."""

    raw_base = value.get("base_config")
    raw_splits = value.get("splits")
    raw_constants = value.get("condition_constants")
    raw_indicators = value.get("indicator_ids")
    raw_signals = value.get("signal_ids")
    indicator_values = raw_indicators if isinstance(raw_indicators, (list, tuple)) else ()
    signal_values = raw_signals if isinstance(raw_signals, (list, tuple)) else ()
    required_buy = value.get("required_buy_conditions")
    required_sell = value.get("required_sell_conditions")

    def conditions(raw: object) -> tuple[ScreenCondition, ...]:
        if not isinstance(raw, (list, tuple)):
            return ()
        return tuple(
            condition
            for condition in (_condition_from_dict(item) for item in raw)
            if condition is not None
        )

    splits: dict[str, dict[str, str]] = {}
    if isinstance(raw_splits, Mapping):
        for name, split in raw_splits.items():
            if isinstance(split, Mapping):
                splits[str(name)] = {str(key): str(item) for key, item in split.items()}
    constants: dict[str, tuple[float, ...]] = {}
    if isinstance(raw_constants, Mapping):
        for key, values in raw_constants.items():
            if isinstance(values, (list, tuple)):
                constants[str(key)] = tuple(float(item) for item in values)

    return DiscoveryRequest(
        base_config=dict(raw_base) if isinstance(raw_base, Mapping) else {},
        splits=splits,
        indicator_ids=tuple(str(item) for item in indicator_values),
        signal_ids=tuple(str(item) for item in signal_values),
        required_buy_conditions=conditions(required_buy),
        required_sell_conditions=conditions(required_sell),
        condition_constants=constants,
        max_conditions_per_side=_as_int(value.get("max_conditions_per_side"), 3),
        max_trials=_as_int(value.get("max_trials"), 1000),
        timeout_seconds=_as_int(value.get("timeout_seconds"), 7200),
        validation_candidates=_as_int(value.get("validation_candidates"), 50),
        holdout_candidates=_as_int(value.get("holdout_candidates"), 20),
        min_trades=_as_int(value.get("min_trades"), 20),
        max_drawdown=_optional_float(value.get("max_drawdown")),
        max_turnover=_optional_float(value.get("max_turnover")),
        max_complexity=_as_int(value.get("max_complexity"), 12),
        multifidelity_enabled=bool(value.get("multifidelity_enabled", True)),
        seed=_as_int(value.get("seed"), 0),
        candidate_mode=cast(
            CandidateMode,
            str(value.get("candidate_mode") or "strategy"),
        ),
    )


def _condition_from_dict(value: object) -> ScreenCondition | None:
    if not isinstance(value, Mapping):
        return None
    left = value.get("left_value")
    operator = value.get("operator")
    right_value = value.get("right_value")
    right_constant = value.get("right_constant")
    if not isinstance(left, str) or not isinstance(operator, str):
        return None
    if right_value is not None and not isinstance(right_value, str):
        return None
    if right_constant is not None and not isinstance(right_constant, (int, float)):
        return None
    if (right_value is None) == (right_constant is None):
        return None
    return ScreenCondition(
        left_value=left,
        operator=operator,
        right_value=right_value,
        right_constant=float(right_constant) if right_constant is not None else None,
    )


def _condition_list(value: object) -> tuple[ScreenCondition, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(
        condition
        for condition in (_condition_from_dict(item) for item in value)
        if condition is not None
    )


def rule_from_dict(value: object) -> RuleGroup:
    if not isinstance(value, Mapping):
        raise ValueError("父候选缺少有效交易规则")
    raw_signals = value.get("signals", ())
    signals = (
        tuple(str(item).strip() for item in raw_signals if str(item).strip())
        if isinstance(raw_signals, (list, tuple))
        else ()
    )
    conditions = _condition_list(value.get("conditions", ()))
    required_conditions = _condition_list(value.get("required_conditions", ()))
    if not signals and not conditions and not required_conditions:
        raise ValueError("父候选交易规则不能为空")
    combine_mode = str(value.get("combine_mode") or "all")
    minimum_matches = value.get("minimum_matches")
    return RuleGroup(
        signals=signals,
        conditions=conditions,
        combine_mode=combine_mode,
        minimum_matches=(_as_int(minimum_matches, 1) if minimum_matches is not None else None),
        required_conditions=required_conditions,
    )


def candidate_from_payload(
    payload: Mapping[str, object],
    *,
    base_config: Mapping[str, object],
) -> StrategyCandidate:
    """Rebuild a candidate stored in a discovery history record."""

    raw_config = payload.get("config")
    if not isinstance(raw_config, Mapping):
        raise ValueError("父候选配置不存在")
    candidate_config = dict(raw_config)
    metrics = payload.get("metrics")
    metrics_mapping = metrics if isinstance(metrics, Mapping) else {}
    return StrategyCandidate(
        buy_rule=rule_from_dict(candidate_config.get("buy_rule")),
        sell_rule=rule_from_dict(candidate_config.get("sell_rule")),
        ranking_value=str(candidate_config.get("ranking_value") or ""),
        rank_order=cast(
            Literal["asc", "desc"],
            str(candidate_config.get("rank_order") or "desc"),
        ),
        max_positions=_as_int(candidate_config.get("max_positions"), 1),
        rebalance_frequency=cast(
            Literal["daily", "weekly", "monthly"],
            str(candidate_config.get("rebalance_frequency") or "daily"),
        ),
        take_profit_mode=cast(RiskMode, str(candidate_config.get("take_profit_mode") or "pct")),
        take_profit_pct=_optional_float(candidate_config.get("take_profit_pct")),
        take_profit_atr_multiplier=_optional_float(
            candidate_config.get("take_profit_atr_multiplier")
        ),
        take_profit_cooldown_days=_as_int(candidate_config.get("take_profit_cooldown_days"), 0),
        stop_loss_mode=cast(RiskMode, str(candidate_config.get("stop_loss_mode") or "pct")),
        stop_loss_pct=_optional_float(candidate_config.get("stop_loss_pct")),
        stop_loss_atr_multiplier=_optional_float(candidate_config.get("stop_loss_atr_multiplier")),
        stop_loss_cooldown_days=_as_int(candidate_config.get("stop_loss_cooldown_days"), 0),
        trailing_stop_mode=cast(RiskMode, str(candidate_config.get("trailing_stop_mode") or "pct")),
        trailing_stop_pct=_optional_float(candidate_config.get("trailing_stop_pct")),
        trailing_stop_atr_multiplier=_optional_float(
            candidate_config.get("trailing_stop_atr_multiplier")
        ),
        trailing_stop_cooldown_days=_as_int(candidate_config.get("trailing_stop_cooldown_days"), 0),
        atr_period=_as_int(candidate_config.get("atr_period"), 14),
        base_config=dict(base_config),
        complexity=_as_int(payload.get("complexity"), 0),
        candidate_hash=str(payload.get("candidate_hash") or ""),
        metrics={
            str(key): dict(value)
            for key, value in metrics_mapping.items()
            if isinstance(value, Mapping)
        },
        human_readable_rules=str(payload.get("human_readable_rules") or ""),
    )


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _items(rule: RuleGroup) -> list[tuple[str, object]]:
    return [("signal", signal) for signal in rule.signals] + [
        ("condition", condition) for condition in rule.conditions
    ]


def _new_item(request: DiscoveryRequest, rng: object) -> tuple[str, object]:
    indicators = tuple(request.indicator_ids)
    signals = tuple(request.signal_ids)
    if signals and bool(_choice(rng, (False, True), False)):
        return "signal", str(_choice(rng, signals, signals[0]))
    return "condition", _condition(request, rng, indicators)


def _mutate_rule(
    rule: RuleGroup,
    request: DiscoveryRequest,
    rng: object,
    *,
    force_change: bool,
) -> RuleGroup:
    rows = _items(rule)
    max_items = max(0, request.max_conditions_per_side - len(rule.required_conditions))
    operation_count = _as_int(_choice(rng, (1, 2), 1), 1) if force_change else 0
    changed = False
    for _ in range(operation_count):
        options = ["replace", "add", "remove"]
        if len(rows) >= max_items:
            options.remove("add")
        if len(rows) <= 1:
            options.remove("remove")
        operation = str(_choice(rng, options, "replace"))
        if operation == "add" and len(rows) < max_items:
            insert_at = _as_int(_choice(rng, tuple(range(len(rows) + 1)), len(rows)), len(rows))
            rows.insert(insert_at, _new_item(request, rng))
            changed = True
        elif operation == "remove" and len(rows) > 1:
            rows.pop(_as_int(_choice(rng, tuple(range(len(rows))), 0), 0))
            changed = True
        elif operation == "replace" and rows:
            index = _as_int(_choice(rng, tuple(range(len(rows))), 0), 0)
            rows[index] = _new_item(request, rng)
            changed = True
    if force_change and not changed and rows:
        index = 0
        rows[index] = _new_item(request, rng)
    signals = tuple(str(value) for kind, value in rows if kind == "signal")
    conditions = tuple(
        value for kind, value in rows if kind == "condition" and isinstance(value, ScreenCondition)
    )
    if not signals and not conditions and not rule.required_conditions:
        conditions = (_condition(request, rng, tuple(request.indicator_ids)),)
    count = len(signals) + len(conditions)
    combine_mode = (
        "all" if count < 2 else str(_choice(rng, ("all", "any", "at_least"), rule.combine_mode))
    )
    minimum_matches = (
        _as_int(_choice(rng, tuple(range(1, count + 1)), 1), 1)
        if combine_mode == "at_least"
        else None
    )
    return RuleGroup(
        signals=signals,
        conditions=conditions,
        combine_mode=combine_mode,
        minimum_matches=minimum_matches,
        required_conditions=rule.required_conditions,
    )


def _neighbor(
    current: object,
    values: tuple[object, ...],
    rng: object,
    *,
    radius: int = 2,
) -> object:
    if current not in values:
        return _choice(rng, values, values[0])
    index = values.index(current)
    choices = values[max(0, index - radius) : min(len(values), index + radius + 1)]
    return _choice(rng, choices, current)


def _local_risk(
    mode: RiskMode,
    percentage: float | None,
    atr_multiplier: float | None,
    cooldown_days: int,
    rng: object,
) -> tuple[RiskMode, float | None, float | None, int]:
    cooldowns = (0, 3, 5, 10)
    target_mode = cast(
        RiskMode,
        _choice(rng, (mode, mode, "atr" if mode == "pct" else "pct"), mode),
    )
    if target_mode == "atr":
        multiplier = _neighbor(
            atr_multiplier if atr_multiplier is not None else 1.0,
            (1.0, 2.0, 3.0),
            rng,
        )
        return (
            "atr",
            None,
            _as_float(multiplier, 1.0),
            _as_int(_neighbor(cooldown_days, cooldowns, rng), cooldown_days),
        )
    value = _neighbor(percentage if percentage is not None else 0.08, (0.05, 0.08, 0.12, 0.2), rng)
    return (
        "pct",
        _as_float(value, 0.0) if value is not None else None,
        None,
        _as_int(_neighbor(cooldown_days, cooldowns, rng), cooldown_days),
    )


def build_refined_candidate(
    parent: StrategyCandidate,
    request: DiscoveryRequest,
    rng: object | None = None,
    *,
    radius: RefinementRadius = "medium",
    force_change: bool = True,
) -> StrategyCandidate:
    """Create one bounded mutation around a parent candidate."""

    if radius != "medium":
        raise ValueError("当前只支持 medium 二次优化邻域")
    generator = rng or random.Random()
    buy_rule = _mutate_rule(parent.buy_rule, request, generator, force_change=force_change)
    sell_rule = _mutate_rule(parent.sell_rule, request, generator, force_change=force_change)
    max_positions = max(
        1,
        min(
            100,
            parent.max_positions + _as_int(_choice(generator, (-2, -1, 0, 1, 2), 0), 0),
        ),
    )
    rank_order = cast(
        Literal["asc", "desc"],
        _choice(
            generator,
            (parent.rank_order, "asc" if parent.rank_order == "desc" else "desc"),
            parent.rank_order,
        ),
    )
    frequency = cast(
        Literal["daily", "weekly", "monthly"],
        _choice(
            generator,
            (parent.rebalance_frequency, "daily", "weekly", "monthly"),
            parent.rebalance_frequency,
        ),
    )
    risk_values = {
        "take_profit": _local_risk(
            parent.take_profit_mode,
            parent.take_profit_pct,
            parent.take_profit_atr_multiplier,
            parent.take_profit_cooldown_days,
            generator,
        ),
        "stop_loss": _local_risk(
            parent.stop_loss_mode,
            parent.stop_loss_pct,
            parent.stop_loss_atr_multiplier,
            parent.stop_loss_cooldown_days,
            generator,
        ),
        "trailing_stop": _local_risk(
            parent.trailing_stop_mode,
            parent.trailing_stop_pct,
            parent.trailing_stop_atr_multiplier,
            parent.trailing_stop_cooldown_days,
            generator,
        ),
    }
    take_profit_mode, take_profit_pct, take_profit_atr, take_profit_cooldown = risk_values[
        "take_profit"
    ]
    stop_loss_mode, stop_loss_pct, stop_loss_atr, stop_loss_cooldown = risk_values["stop_loss"]
    trailing_mode, trailing_pct, trailing_atr, trailing_cooldown = risk_values["trailing_stop"]
    catalog = default_indicator_catalog()
    indicators = tuple(catalog.canonical_id(value) for value in request.indicator_ids)
    # 父代的排序值可能继承自一个有问题的历史候选（按股价排序），不能直接沿用，
    # 要和指标池一起过 rankable 这道筛。筛完为空时退回父代取值，避免候选退化。
    rankable = catalog.rankable_among((catalog.canonical_id(parent.ranking_value), *indicators))
    ranking_value = str(_choice(generator, rankable, parent.ranking_value))
    candidate = StrategyCandidate(
        buy_rule=buy_rule,
        sell_rule=sell_rule,
        ranking_value=ranking_value,
        rank_order=rank_order,
        max_positions=max_positions,
        rebalance_frequency=frequency,
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
        atr_period=parent.atr_period,
        base_config=parent.base_config,
    )
    if force_change and candidate.candidate_hash == parent.candidate_hash:
        candidate = replace(candidate, rank_order="asc" if parent.rank_order == "desc" else "desc")
    return candidate
