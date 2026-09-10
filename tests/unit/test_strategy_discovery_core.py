from __future__ import annotations

import random

from selector_app.strategy_discovery.catalog import default_indicator_catalog
from selector_app.strategy_discovery.candidates import build_random_candidate, candidate_hash
from selector_app.strategy_discovery.models import DiscoveryRequest
from selector_app.strategy_discovery.search import HybridSearchEngine


def test_default_catalog_contains_existing_formula_and_chart_outputs() -> None:
    catalog = default_indicator_catalog()

    assert "indicator_three.var1" in catalog.numeric_ids
    assert "indicator_two.short_line" in catalog.numeric_ids
    assert "ma5" in catalog.numeric_ids
    assert "rsi14" in catalog.numeric_ids
    assert "macd_signal" in catalog.numeric_ids
    assert "indicator_one.z" not in catalog.rankable_ids
    assert len(catalog.signal_ids) == 15


def test_candidate_generator_can_create_indicator_and_constant_conditions() -> None:
    request = DiscoveryRequest(
        base_config={"max_positions": 3},
        indicator_ids=("ma5", "ma20"),
        signal_ids=(),
        condition_constants={"ma5": (10.0, 20.0)},
        max_conditions_per_side=1,
    )

    candidates = [
        build_random_candidate(request, random.Random(seed)) for seed in range(20)
    ]
    conditions = [
        condition
        for candidate in candidates
        for rule in (candidate.buy_rule, candidate.sell_rule)
        for condition in rule.conditions
    ]
    assert conditions
    assert any(condition.right_value is not None for condition in conditions)
    assert any(condition.right_constant is not None for condition in conditions)
    assert all(candidate.max_positions == 3 for candidate in candidates)


def test_candidate_hash_is_stable_for_mapping_order() -> None:
    first = {"b": 2, "a": {"d": 4, "c": 3}}
    second = {"a": {"c": 3, "d": 4}, "b": 2}

    assert candidate_hash(first) == candidate_hash(second)


def test_hybrid_search_returns_separate_sharpe_and_return_boards() -> None:
    request = DiscoveryRequest(
        base_config={"max_positions": 2},
        indicator_ids=("ma5",),
        signal_ids=(),
        max_conditions_per_side=0,
    )
    seen: list[str] = []

    def evaluate(candidate, _stage):
        seen.append(candidate.candidate_hash)
        value = float(candidate.rank_order == "desc")
        return {
            "annual_return": value + len(seen) / 100.0,
            "sharpe": len(seen) / 10.0,
            "total_trades": 10,
            "max_drawdown": 0.1,
        }

    result = HybridSearchEngine().search(
        request,
        candidate_builder=build_random_candidate,
        evaluator=evaluate,
        max_trials=8,
        seed=7,
        validation_limit=4,
        holdout_limit=2,
    )

    assert len(seen) == 8
    assert result.results_by_sharpe
    assert result.results_by_return
    assert result.results_by_sharpe[0].metrics["search"]["sharpe"] >= result.results_by_sharpe[-1].metrics["search"]["sharpe"]
    assert result.results_by_return[0].metrics["search"]["annual_return"] >= result.results_by_return[-1].metrics["search"]["annual_return"]
