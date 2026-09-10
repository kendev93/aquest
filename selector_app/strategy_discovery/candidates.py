"""Constrained candidate generation for strategy discovery."""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Mapping, Sequence
from typing import Literal, cast

from selector_app.screening.models import ScreenCondition
from selector_app.screening.rules import RuleGroup

from .catalog import default_indicator_catalog
from .models import DiscoveryRequest, RiskMode, StrategyCandidate

_OPERATORS = ("gt", "gte", "lt", "lte", "eq", "neq", "cross_up", "cross_down")
_DEFAULT_CONSTANTS = (-1.0, 0.0, 10.0, 30.0, 50.0, 70.0, 90.0)
_RISK_COOLDOWNS = (0, 3, 5, 10)
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
    use_indicator = len(indicators) > 1 and bool(_choice(rng, (False, True), False))
    if use_indicator:
        right_options = [item for item in indicators if item != left]
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
    item_count = len(required) + len(selected_signals) + len(conditions)
    combine_mode = (
        "all"
        if required
        else "all"
        if item_count < 2
        else str(_choice(rng, ("all", "any", "at_least"), "all"))
    )
    minimum_matches = (
        _int_choice(rng, tuple(range(1, item_count + 1)), 1) if combine_mode == "at_least" else None
    )
    return RuleGroup(
        signals=tuple(selected_signals),
        conditions=(*required, *conditions),
        combine_mode=combine_mode,
        minimum_matches=minimum_matches,
    )


def _risk_values(rng: object) -> tuple[RiskMode, float | None, float | None, int]:
    mode: RiskMode = "atr" if _choice(rng, ("pct", "atr"), "pct") == "atr" else "pct"
    cooldown = _as_int(_choice(rng, _RISK_COOLDOWNS, 0), 0)
    if mode == "atr":
        return mode, None, _as_float(_choice(rng, (1.0, 2.0, 3.0), 1.0), 1.0), cooldown
    return mode, _optional_float(_choice(rng, (None, 0.05, 0.08, 0.12, 0.2), None)), None, cooldown


def build_random_candidate(
    request: DiscoveryRequest, rng: object | None = None
) -> StrategyCandidate:
    generator = rng or random.Random()
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
    rank_order_value = str(_choice(generator, ("asc", "desc"), "desc"))
    rank_order = cast(
        _RankOrder,
        rank_order_value if rank_order_value in {"asc", "desc"} else "desc",
    )
    candidate = StrategyCandidate(
        buy_rule=buy_rule,
        sell_rule=sell_rule,
        ranking_value=str(_choice(generator, indicators, indicators[0])),
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
            + len(sell_rule.signals)
            + len(sell_rule.conditions)
            + sum(
                1
                for prefix in ("take_profit", "stop_loss", "trailing_stop")
                if risk[prefix][1] is not None or risk[prefix][2] is not None
            )
        ),
    )
    return candidate
