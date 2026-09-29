from __future__ import annotations

import multiprocessing
import os
import random
import time
from concurrent.futures import ProcessPoolExecutor
from itertools import repeat

import pytest

from selector_app.screening.models import ScreenCondition
from selector_app.screening.rules import RuleGroup
from selector_app.strategy_discovery import candidates as candidates_module
from selector_app.strategy_discovery import search as search_module
from selector_app.strategy_discovery.candidates import (
    build_random_candidate,
    candidate_hash,
    normalize_candidate_config,
)
from selector_app.strategy_discovery.catalog import default_indicator_catalog
from selector_app.strategy_discovery.models import DiscoveryRequest, StrategyCandidate
from selector_app.strategy_discovery.search import HybridSearchEngine
from selector_app.web.schemas import PortfolioBacktestRequest


def _parallel_pid_evaluator(_candidate, _stage: str):
    time.sleep(0.05)
    return {
        "annual_return": 0.1,
        "sharpe": 1.0,
        "total_trades": 10,
        "max_drawdown": 0.1,
        "worker_pid": os.getpid(),
    }


def test_default_catalog_contains_existing_formula_and_chart_outputs() -> None:
    catalog = default_indicator_catalog()

    assert "indicator_three.var1" in catalog.numeric_ids
    assert "indicator_two.short_line" in catalog.numeric_ids
    assert "ma5" in catalog.numeric_ids
    assert "rsi14" in catalog.numeric_ids
    assert "macd_signal" in catalog.numeric_ids
    assert "indicator_one.z" not in catalog.rankable_ids
    assert "indicator_two.warning_line" not in catalog.rankable_ids
    # Prices are usable condition operands but never cross-stock ranking features.
    assert "price.close" in catalog.numeric_ids
    assert "price.close" not in catalog.rankable_ids
    assert "price.open" not in catalog.rankable_ids
    # Currency-denominated indicators stay usable operands but must not be
    # ranking features: sorting on them is sorting by share price, which breaks
    # outright once ETFs (0.5-5 CNY) and stocks (3-300 CNY) share a universe.
    for price_scaled in ("technical.ma5", "technical.ma10", "technical.ma20", "technical.ma60"):
        assert price_scaled in catalog.numeric_ids
        assert price_scaled not in catalog.rankable_ids
    for macd_scaled in ("technical.macd", "technical.macd_signal", "technical.macd_histogram"):
        assert macd_scaled in catalog.numeric_ids
        assert macd_scaled not in catalog.rankable_ids
    # RSI is dimensionless, so it must survive as a ranking feature.
    assert "technical.rsi14" in catalog.rankable_ids
    assert len(catalog.signal_ids) == 15


def test_candidate_generator_can_create_indicator_and_constant_conditions() -> None:
    request = DiscoveryRequest(
        base_config={"max_positions": 3},
        indicator_ids=("ma5", "ma20"),
        signal_ids=(),
        condition_constants={"ma5": (10.0, 20.0)},
        max_conditions_per_side=1,
    )

    candidates = [build_random_candidate(request, random.Random(seed)) for seed in range(20)]
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


def test_candidate_config_excludes_strategy_only_metadata() -> None:
    request = DiscoveryRequest(
        base_config={"date_mode": "recent5", "max_positions": 3},
        indicator_ids=("technical.ma5",),
        signal_ids=(),
        max_conditions_per_side=1,
    )
    candidate = build_random_candidate(request, random.Random(7))

    payload = candidate.to_config()

    assert "date_mode" not in payload
    PortfolioBacktestRequest.model_validate(payload)


def test_risk_only_candidates_preserve_every_nonrisk_portfolio_setting() -> None:
    base_config: dict[str, object] = {
        "date_mode": "custom",
        "start_date": "2024-01-01",
        "end_date": "2024-12-31",
        "vipdoc_path": "/tmp/vipdoc",
        "universe": "all",
        "selected_signals": [],
        "combine_mode": "all",
        "minimum_matches": None,
        "buy_rule": {
            "signals": [],
            "conditions": [{"left_value": "technical.ma5", "operator": "gt", "right_constant": 10}],
            "required_conditions": [],
            "combine_mode": "all",
            "minimum_matches": None,
        },
        "sell_rule": None,
        "ranking_value": "technical.rsi14",
        "rank_order": "asc",
        "max_positions": 3,
        "rebalance_frequency": "weekly",
        "initial_cash": 200_000.0,
        "commission": 0.0002,
        "execution": "next_close",
        "stop_loss_pct": 0.08,
        "take_profit_pct": 0.2,
        "moving_stop_loss_pct": None,
        "trailing_stop_pct": 0.1,
    }
    risk_keys = {
        "stop_loss_pct",
        "stop_loss_mode",
        "stop_loss_atr_multiplier",
        "stop_loss_cooldown_days",
        "take_profit_pct",
        "take_profit_mode",
        "take_profit_atr_multiplier",
        "take_profit_cooldown_days",
        "moving_stop_loss_pct",
        "moving_stop_loss_mode",
        "moving_stop_loss_atr_multiplier",
        "moving_stop_loss_cooldown_days",
        "trailing_stop_pct",
        "trailing_stop_mode",
        "trailing_stop_atr_multiplier",
        "trailing_stop_cooldown_days",
        "atr_period",
    }
    request = DiscoveryRequest(
        base_config=base_config,
        splits={
            "search": {"start_date": "2024-01-01", "end_date": "2024-07-01"},
            "validation": {"start_date": "2024-07-02", "end_date": "2024-10-01"},
            "holdout": {"start_date": "2024-10-02", "end_date": "2024-12-31"},
        },
        candidate_mode="risk_only",
        max_trials=20,
    )

    for seed in range(30):
        candidate = build_random_candidate(request, random.Random(seed))
        candidate_config = candidate.to_config()
        for key, value in base_config.items():
            if key not in risk_keys and key != "date_mode":
                assert candidate_config[key] == value
        for prefix in ("stop_loss", "take_profit", "moving_stop_loss", "trailing_stop"):
            mode = candidate_config[f"{prefix}_mode"]
            percentage = candidate_config.get(f"{prefix}_pct")
            multiplier = candidate_config.get(f"{prefix}_atr_multiplier")
            cooldown = candidate_config[f"{prefix}_cooldown_days"]
            assert mode in {"pct", "atr"}
            assert cooldown in {0, 3, 5, 10}
            if mode == "pct":
                assert percentage in {None, 0.05, 0.08, 0.12, 0.2}
                assert multiplier is None
            else:
                assert percentage is None
                assert multiplier in {1.0, 2.0, 3.0}


def test_candidate_generator_includes_required_conditions_and_limits_auto_items() -> None:
    required_buy = ScreenCondition(
        left_value="technical.ma5",
        operator="cross_up",
        right_value="technical.ma20",
    )
    required_sell = ScreenCondition(
        left_value="technical.rsi14",
        operator="gte",
        right_constant=70.0,
    )
    request = DiscoveryRequest(
        base_config={"max_positions": 3},
        indicator_ids=("technical.ma5", "technical.ma20", "technical.rsi14"),
        signal_ids=("indicator_three.begin_zone",),
        max_conditions_per_side=2,
        required_buy_conditions=(required_buy,),
        required_sell_conditions=(required_sell,),
    )

    candidates = [build_random_candidate(request, random.Random(seed)) for seed in range(20)]

    for candidate in candidates:
        assert required_buy in candidate.buy_rule.required_conditions
        assert required_sell in candidate.sell_rule.required_conditions
        assert (
            len(candidate.buy_rule.conditions)
            + len(candidate.buy_rule.required_conditions)
            + len(candidate.buy_rule.signals)
            <= 2
        )
        assert (
            len(candidate.sell_rule.conditions)
            + len(candidate.sell_rule.required_conditions)
            + len(candidate.sell_rule.signals)
            <= 2
        )


def test_required_conditions_allow_or_for_generated_conditions() -> None:
    required = ScreenCondition(
        left_value="technical.ma5",
        operator="gt",
        right_constant=10.0,
    )

    class MaxChoice:
        def __init__(self):
            self.condition_count = 0

        def choice(self, values):
            options = list(values)
            if "any" in options:
                return "any"
            if options == ["technical.ma5", "technical.ma20"]:
                choice = options[self.condition_count % len(options)]
                self.condition_count += 1
                return choice
            return options[-1]

    rule = candidates_module._rule(
        DiscoveryRequest(
            base_config={"max_positions": 3},
            indicator_ids=("technical.ma5", "technical.ma20"),
            signal_ids=(),
            max_conditions_per_side=3,
        ),
        MaxChoice(),
        ("technical.ma5", "technical.ma20"),
        (),
        (required,),
    )

    assert rule.combine_mode == "any"
    assert rule.required_conditions == (required,)
    assert required not in rule.conditions
    assert len(rule.conditions) == 2
    PortfolioBacktestRequest.model_validate(
        StrategyCandidate(
            buy_rule=rule,
            sell_rule=rule,
            ranking_value="technical.ma5",
            rank_order="desc",
            max_positions=3,
            base_config={"universe": "all", "max_positions": 3},
        ).to_config()
    )


def test_candidate_hash_is_stable_for_mapping_order() -> None:
    first = {"b": 2, "a": {"d": 4, "c": 3}}
    second = {"a": {"c": 3, "d": 4}, "b": 2}

    assert candidate_hash(first) == candidate_hash(second)


def test_candidate_normalization_deduplicates_commutative_condition_order() -> None:
    first = normalize_candidate_config(
        {
            "buy_rule": {
                "signals": ["b", "a"],
                "conditions": [
                    {"left_value": "z", "operator": "gt", "right_constant": 1},
                    {"left_value": "a", "operator": "lt", "right_constant": 2},
                ],
            }
        }
    )
    second = normalize_candidate_config(
        {
            "buy_rule": {
                "signals": ["a", "b"],
                "conditions": [
                    {"left_value": "a", "operator": "lt", "right_constant": 2},
                    {"left_value": "z", "operator": "gt", "right_constant": 1},
                ],
            }
        }
    )

    assert candidate_hash(first) == candidate_hash(second)


def test_lightgbm_surrogate_reuses_model_until_enough_new_samples(monkeypatch) -> None:
    pytest.importorskip("lightgbm")
    request = DiscoveryRequest(
        base_config={"max_positions": 1},
        indicator_ids=("technical.ma5", "technical.ma20"),
        signal_ids=(),
        max_conditions_per_side=1,
        required_buy_conditions=(
            ScreenCondition(left_value="technical.ma5", operator="gt", right_constant=10.0),
        ),
        required_sell_conditions=(
            ScreenCondition(left_value="technical.ma20", operator="gt", right_constant=10.0),
        ),
    )
    candidates = [
        build_random_candidate(request, random.Random(seed)).with_metrics(
            "search",
            {"sharpe": float(seed), "annual_return": float(seed) / 10},
        )
        for seed in range(40)
    ]
    surrogate = search_module._LightGBMSurrogate()

    assert surrogate.fit(candidates[:10], "sharpe") is True
    assert surrogate.fit_count == 1
    assert surrogate.fit(candidates[:20], "sharpe") is True
    assert surrogate.fit_count == 1
    assert surrogate.fit(candidates[:25], "sharpe") is True
    assert surrogate.fit_count == 1
    assert surrogate.fit(candidates[:35], "sharpe") is True
    assert surrogate.fit_count == 2
    assert surrogate.fit(candidates[:20], "annual_return") is True
    assert surrogate.fit_count == 3


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
    assert (
        result.results_by_sharpe[0].metrics["search"]["sharpe"]
        >= result.results_by_sharpe[-1].metrics["search"]["sharpe"]
    )
    assert (
        result.results_by_return[0].metrics["search"]["annual_return"]
        >= result.results_by_return[-1].metrics["search"]["annual_return"]
    )


def test_hybrid_search_can_stop_after_a_long_plateau() -> None:
    request = DiscoveryRequest(
        base_config={"max_positions": 1},
        indicator_ids=("technical.ma5",),
        signal_ids=(),
        max_conditions_per_side=0,
        max_trials=100,
        min_trades=0,
        max_drawdown=None,
    )

    result = HybridSearchEngine().search(
        request,
        evaluator=lambda _candidate, _stage: {
            "annual_return": 0.1,
            "sharpe": 1.0,
            "total_trades": 10,
            "max_drawdown": 0.1,
        },
        max_trials=100,
        batch_size=10,
        convergence_min_trials=20,
        convergence_patience=20,
        convergence_enabled=True,
        seed=23,
    )

    assert result.search_metadata["stop_reason"] == "converged"
    assert result.search_metadata["evaluated_trials"] < 100


def test_hybrid_search_runs_the_requested_budget_by_default_and_reports_timings() -> None:
    request = DiscoveryRequest(
        base_config={"max_positions": 1},
        indicator_ids=("technical.ma5",),
        signal_ids=(),
        max_conditions_per_side=1,
        max_trials=400,
        min_trades=0,
        max_drawdown=None,
        multifidelity_enabled=False,
    )
    counter = iter(range(10_000))

    def builder(current_request, _generator):
        index = next(counter)
        return StrategyCandidate(
            buy_rule=RuleGroup(
                conditions=(
                    ScreenCondition(
                        left_value="technical.ma5",
                        operator="gt",
                        right_constant=float(index),
                    ),
                ),
            ),
            sell_rule=RuleGroup(
                conditions=(
                    ScreenCondition(
                        left_value="technical.ma5",
                        operator="lt",
                        right_constant=float(index + 1),
                    ),
                ),
            ),
            ranking_value="technical.ma5",
            rank_order="desc",
            max_positions=1,
            base_config=current_request.base_config,
        )

    result = HybridSearchEngine().search(
        request,
        candidate_builder=builder,
        evaluator=lambda _candidate, _stage: {
            "annual_return": 0.1,
            "sharpe": 1.0,
            "total_trades": 10,
            "max_drawdown": 0.1,
        },
        max_trials=400,
        seed=31,
    )

    assert result.search_metadata["evaluated_trials"] == 400
    assert result.search_metadata["stop_reason"] == "completed"
    assert result.search_metadata["convergence_enabled"] is False
    durations = result.search_metadata["stage_durations_seconds"]
    assert isinstance(durations, dict)
    assert durations["search"] >= 0
    assert result.search_metadata["candidate_generation_seconds"] >= 0
    assert result.search_metadata["proposal_attempts"] >= 400
    assert 0 < result.search_metadata["unique_proposal_rate"] <= 1
    assert result.search_metadata["surrogate_refit_interval"] == 25
    assert result.search_metadata["preflight_skipped"] == 0
    throughput = result.search_metadata["stage_throughput_per_second"]
    assert isinstance(throughput, dict)
    assert "search" in throughput


def test_missing_ml_dependencies_fail_without_explicit_local_fallback(monkeypatch) -> None:
    request = DiscoveryRequest(
        base_config={"max_positions": 1},
        indicator_ids=("technical.ma5",),
        signal_ids=(),
    )

    monkeypatch.delenv("INDICATOR_LAB_DISCOVERY_ALLOW_FALLBACK", raising=False)
    monkeypatch.setattr(
        search_module,
        "_assert_ml_dependencies",
        lambda: (_ for _ in ()).throw(RuntimeError("Docker dependencies missing")),
    )
    with pytest.raises(RuntimeError, match="Docker dependencies missing"):
        HybridSearchEngine().search(
            request,
            evaluator=lambda _candidate, _stage: {
                "annual_return": 0.1,
                "sharpe": 1.0,
                "total_trades": 5,
                "max_drawdown": 0.1,
            },
            max_trials=1,
        )


def test_explicit_local_fallback_is_seeded_and_deterministic(monkeypatch) -> None:
    request = DiscoveryRequest(
        base_config={"max_positions": 1},
        indicator_ids=("technical.ma5", "technical.ma20"),
        signal_ids=(),
        max_trials=4,
        min_trades=1,
        max_drawdown=None,
    )
    monkeypatch.setenv("INDICATOR_LAB_DISCOVERY_ALLOW_FALLBACK", "1")

    def evaluate(candidate, _stage):
        score = 1.0 if candidate.rank_order == "desc" else 0.5
        return {"annual_return": score, "sharpe": score, "total_trades": 5, "max_drawdown": 0.1}

    first = HybridSearchEngine().search(request, evaluator=evaluate, seed=31, max_trials=4)
    second = HybridSearchEngine().search(request, evaluator=evaluate, seed=31, max_trials=4)

    assert [item.candidate_hash for item in first.results_by_sharpe] == [
        item.candidate_hash for item in second.results_by_sharpe
    ]
    assert first.search_metadata["algorithm"] == "seeded_random_fallback"


def test_multifidelity_promotes_only_bounded_prefixes() -> None:
    request = DiscoveryRequest(
        base_config={"max_positions": 1},
        indicator_ids=("technical.ma5",),
        signal_ids=(),
        max_trials=12,
        min_trades=5,
        max_drawdown=None,
    )
    stages: list[str] = []

    def evaluate_fidelity(_candidate, stage: str):
        stages.append(stage)
        return {"annual_return": 0.1, "sharpe": 1.0, "total_trades": 10, "max_drawdown": 0.1}

    result = HybridSearchEngine().search(
        request,
        evaluator=lambda _candidate, _stage: {},
        fidelity_evaluator=evaluate_fidelity,
        max_trials=12,
        seed=9,
    )

    assert stages.count("coarse") == 12
    assert stages.count("medium") == 5
    assert stages.count("full") == 2
    assert result.search_metadata["fidelity_evaluations"] == {"coarse": 12, "medium": 5, "full": 2}


def test_batch_evaluation_uses_eight_process_slots_and_preserves_result_order() -> None:
    request = DiscoveryRequest(
        base_config={"max_positions": 1},
        indicator_ids=("technical.ma5",),
        signal_ids=(),
        max_trials=8,
        min_trades=1,
        max_drawdown=None,
        multifidelity_enabled=False,
        seed=17,
    )
    batches: list[int] = []
    process_context = multiprocessing.get_context("spawn")

    with ProcessPoolExecutor(max_workers=8, mp_context=process_context) as executor:

        def evaluate_batch(candidates, stage: str):
            batches.append(len(candidates))
            return list(executor.map(_parallel_pid_evaluator, candidates, repeat(stage)))

        result = HybridSearchEngine().search(
            request,
            evaluator=lambda _candidate, _stage: {},
            batch_evaluator=evaluate_batch,
            batch_size=8,
            evaluation_workers=8,
            max_trials=8,
            seed=17,
        )

    assert batches == [8]
    assert result.search_metadata["evaluation_workers"] == 8
    assert (
        2 <= len({item.metrics["search"]["worker_pid"] for item in result.results_by_sharpe}) <= 8
    )
    assert [item.candidate_hash for item in result.results_by_sharpe] == [
        item.candidate_hash for item in result.results_by_return
    ]
