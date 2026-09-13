from __future__ import annotations

from dataclasses import replace

from selector_app.strategy_discovery.candidates import build_random_candidate
from selector_app.strategy_discovery.evaluation import run_validation_pipeline
from selector_app.strategy_discovery.models import DiscoveryRequest
from selector_app.strategy_discovery.search import SearchResult


def _request() -> DiscoveryRequest:
    return DiscoveryRequest(
        base_config={
            "max_positions": 2,
            "start_date": "2024-01-01",
            "end_date": "2024-12-31",
        },
        indicator_ids=("technical.ma5",),
        signal_ids=(),
        max_conditions_per_side=1,
        max_trials=4,
        validation_candidates=2,
        holdout_candidates=2,
        min_trades=1,
        max_drawdown=None,
        seed=3,
    )


def test_validation_pipeline_ranks_by_validation_and_attaches_holdout() -> None:
    request = _request()
    candidates = [
        build_random_candidate(request, __import__("random").Random(seed)) for seed in range(4)
    ]
    search = SearchResult(
        results_by_sharpe=tuple(candidates[:2]),
        results_by_return=tuple(candidates[2:]),
        search_metadata={"evaluated_trials": 4},
    )

    def evaluate(candidate, split: str):
        value = float(int(candidate.candidate_hash[:4], 16) % 100) / 100.0
        if split == "holdout":
            value = 1.0 - value
        return {
            "annual_return": value,
            "sharpe": value * 2,
            "total_trades": 10,
            "max_drawdown": 0.1,
        }

    result = run_validation_pipeline(search, request, evaluate)

    assert result.results_by_sharpe
    assert result.results_by_return
    assert all("validation" in item.metrics for item in result.results_by_sharpe)
    assert all("holdout" in item.metrics for item in result.results_by_sharpe)
    assert result.results_by_sharpe[0].metrics["holdout"]["constraints_passed"] is True
    assert result.to_dict()["results_by_sharpe"][0]["config"]["start_date"] == "2024-01-01"
    assert result.validation_metadata["leaderboard_period"] == "validation"
    durations = result.validation_metadata["stage_durations_seconds"]
    assert isinstance(durations, dict)
    assert durations["validation"] >= 0
    assert durations["holdout"] >= 0


def test_validation_pipeline_keeps_independent_sharpe_and_return_leaderboards() -> None:
    request = _request()
    candidates = [
        build_random_candidate(request, __import__("random").Random(seed)) for seed in range(4)
    ]
    sharpe_order = candidates
    return_order = [candidates[2], candidates[3], candidates[0], candidates[1]]
    scores = {
        candidates[0].candidate_hash: {"annual_return": 0.1, "sharpe": 2.0},
        candidates[1].candidate_hash: {"annual_return": 0.2, "sharpe": 1.5},
        candidates[2].candidate_hash: {"annual_return": 0.5, "sharpe": 0.5},
        candidates[3].candidate_hash: {"annual_return": 0.4, "sharpe": 0.4},
    }
    search = SearchResult(
        results_by_sharpe=tuple(sharpe_order),
        results_by_return=tuple(return_order),
        search_metadata={"evaluated_trials": 4},
    )

    def evaluate(candidate, _split: str):
        return {
            **scores[candidate.candidate_hash],
            "total_trades": 10,
            "max_drawdown": 0.1,
        }

    result = run_validation_pipeline(search, request, evaluate)

    assert [item.candidate_hash for item in result.results_by_sharpe[:2]] == [
        candidates[0].candidate_hash,
        candidates[1].candidate_hash,
    ]
    assert [item.candidate_hash for item in result.results_by_return[:2]] == [
        candidates[2].candidate_hash,
        candidates[3].candidate_hash,
    ]
    assert result.validation_metadata["search_candidate_count"] == 4


def test_validation_pipeline_removes_duplicate_trade_behaviors_from_each_leaderboard() -> None:
    request = replace(_request(), validation_candidates=12, holdout_candidates=10)
    candidates = [
        build_random_candidate(request, __import__("random").Random(seed)) for seed in range(12)
    ]
    behavior_by_hash = {
        candidate.candidate_hash: "behavior-a" if index < 6 else "behavior-b"
        for index, candidate in enumerate(candidates)
    }
    search = SearchResult(
        results_by_sharpe=tuple(candidates),
        results_by_return=tuple(candidates),
        search_metadata={"evaluated_trials": 4},
    )

    def evaluate(candidate, _split: str):
        return {
            "annual_return": 0.1,
            "sharpe": 1.0,
            "total_trades": 10,
            "max_drawdown": 0.1,
            "trade_fingerprint": behavior_by_hash[candidate.candidate_hash],
        }

    result = run_validation_pipeline(search, request, evaluate)

    assert len(result.results_by_sharpe) == 10
    assert len(result.results_by_return) == 10
    assert len({candidate.candidate_hash for candidate in result.results_by_sharpe}) == 10
    assert len({candidate.candidate_hash for candidate in result.results_by_return}) == 10
    sharpe_fingerprints = [
        candidate.metrics["validation"]["trade_fingerprint"]
        for candidate in result.results_by_sharpe
    ]
    return_fingerprints = [
        candidate.metrics["validation"]["trade_fingerprint"]
        for candidate in result.results_by_return
    ]
    assert set(sharpe_fingerprints[:2]) == {"behavior-a", "behavior-b"}
    assert set(return_fingerprints[:2]) == {"behavior-a", "behavior-b"}
    assert result.validation_metadata["behavioral_deduplication"] == {
        "validation_sharpe_removed": 10,
        "validation_return_removed": 10,
        "sharpe_backfilled": 8,
        "return_backfilled": 8,
    }
