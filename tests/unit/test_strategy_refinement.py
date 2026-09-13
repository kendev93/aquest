from __future__ import annotations

import random

from selector_app.screening.models import ScreenCondition
from selector_app.screening.rules import RuleGroup
from selector_app.strategy_discovery.candidates import build_random_candidate
from selector_app.strategy_discovery.models import DiscoveryRequest, StrategyCandidate
from selector_app.strategy_discovery.refinement import build_refined_candidate


def _request() -> DiscoveryRequest:
    return DiscoveryRequest(
        base_config={
            "universe": "all",
            "max_positions": 3,
            "start_date": "2024-01-01",
            "end_date": "2026-09-08",
        },
        indicator_ids=("technical.ma5", "technical.ma20", "technical.rsi14"),
        signal_ids=("indicator_three.begin_zone", "indicator_three.end_zone"),
        max_conditions_per_side=3,
        max_trials=8,
        validation_candidates=2,
        holdout_candidates=1,
        min_trades=0,
        max_drawdown=None,
        multifidelity_enabled=False,
        seed=19,
    )


def _parent(request: DiscoveryRequest) -> StrategyCandidate:
    return StrategyCandidate(
        buy_rule=RuleGroup(
            conditions=(
                ScreenCondition(
                    left_value="technical.ma5",
                    operator="gt",
                    right_value="technical.ma20",
                ),
            )
        ),
        sell_rule=RuleGroup(
            conditions=(
                ScreenCondition(
                    left_value="technical.rsi14",
                    operator="gte",
                    right_constant=70.0,
                ),
            )
        ),
        ranking_value="technical.ma5",
        rank_order="desc",
        max_positions=3,
        base_config=request.base_config,
    )


def test_refined_candidate_mutates_parent_within_medium_neighborhood() -> None:
    request = _request()
    parent = _parent(request)

    refined = build_refined_candidate(parent, request, random.Random(7), radius="medium")

    assert refined.candidate_hash != parent.candidate_hash
    assert refined.buy_rule.signals or refined.buy_rule.conditions
    assert refined.sell_rule.signals or refined.sell_rule.conditions
    assert len(refined.buy_rule.signals) + len(refined.buy_rule.conditions) <= 3
    assert len(refined.sell_rule.signals) + len(refined.sell_rule.conditions) <= 3
    assert refined.max_positions in {1, 2, 3, 4, 5}


def test_refinement_keeps_required_conditions_outside_mutable_rule_group() -> None:
    request = _request()
    required_buy = ScreenCondition(
        left_value="technical.ma20",
        operator="gte",
        right_constant=15.0,
    )
    required_sell = ScreenCondition(
        left_value="technical.rsi14",
        operator="lt",
        right_constant=80.0,
    )
    parent = StrategyCandidate(
        buy_rule=RuleGroup(
            conditions=(
                ScreenCondition(
                    left_value="technical.ma5",
                    operator="gt",
                    right_value="technical.ma20",
                ),
            ),
            required_conditions=(required_buy,),
        ),
        sell_rule=RuleGroup(
            conditions=(
                ScreenCondition(
                    left_value="technical.rsi14",
                    operator="gte",
                    right_constant=70.0,
                ),
            ),
            required_conditions=(required_sell,),
        ),
        ranking_value="technical.ma5",
        rank_order="desc",
        max_positions=3,
        base_config=request.base_config,
    )

    refined = build_refined_candidate(parent, request, random.Random(7), radius="medium")

    assert refined.buy_rule.required_conditions == (required_buy,)
    assert refined.sell_rule.required_conditions == (required_sell,)
    assert len(refined.buy_rule.required_conditions) + len(refined.buy_rule.conditions) <= 3
    assert len(refined.sell_rule.required_conditions) + len(refined.sell_rule.conditions) <= 3


def test_refinement_candidate_keeps_parent_when_requested_as_anchor() -> None:
    request = _request()
    parent = build_random_candidate(request, random.Random(3))

    refined = build_refined_candidate(
        parent,
        request,
        random.Random(11),
        radius="medium",
        force_change=False,
    )

    assert refined.base_config == parent.base_config
    assert refined.buy_rule.conditions or refined.buy_rule.signals
    assert refined.sell_rule.conditions or refined.sell_rule.signals
