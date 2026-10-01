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


def test_validation_pipeline_deduplicates_each_top_ten_by_its_own_metric() -> None:
    request = replace(_request(), validation_candidates=12, holdout_candidates=10)
    candidates = [
        build_random_candidate(request, __import__("random").Random(seed)) for seed in range(12)
    ]
    behavior_by_hash = {
        candidate.candidate_hash: "behavior-a" if index in {0, 3} else f"behavior-{index}"
        for index, candidate in enumerate(candidates)
    }
    scores = {
        candidates[0].candidate_hash: {"sharpe": 9.0, "annual_return": 0.90},
        candidates[1].candidate_hash: {"sharpe": 8.0, "annual_return": 0.80},
        candidates[2].candidate_hash: {"sharpe": 7.0, "annual_return": 0.70},
        candidates[3].candidate_hash: {"sharpe": 6.0, "annual_return": 0.95},
        candidates[4].candidate_hash: {"sharpe": 5.0, "annual_return": 0.05},
        candidates[5].candidate_hash: {"sharpe": 4.0, "annual_return": 0.50},
        candidates[6].candidate_hash: {"sharpe": 3.0, "annual_return": 0.40},
        candidates[7].candidate_hash: {"sharpe": 2.0, "annual_return": 0.30},
        candidates[8].candidate_hash: {"sharpe": 1.0, "annual_return": 0.20},
        candidates[9].candidate_hash: {"sharpe": 0.5, "annual_return": 0.10},
        candidates[10].candidate_hash: {"sharpe": 0.4, "annual_return": 0.04},
        candidates[11].candidate_hash: {"sharpe": 10.0, "annual_return": -0.20},
    }
    search = SearchResult(
        results_by_sharpe=tuple(candidates),
        results_by_return=tuple(candidates),
        search_metadata={"evaluated_trials": 4},
    )

    def evaluate(candidate, _split: str):
        return {
            **scores[candidate.candidate_hash],
            "total_trades": 10,
            "max_drawdown": 0.1,
            "trade_fingerprint": behavior_by_hash[candidate.candidate_hash],
        }

    result = run_validation_pipeline(search, request, evaluate)

    sharpe_hashes = [candidate.candidate_hash for candidate in result.results_by_sharpe]
    return_hashes = [candidate.candidate_hash for candidate in result.results_by_return]

    assert sharpe_hashes == [
        candidates[index].candidate_hash for index in (11, 0, 1, 2, 4, 5, 6, 7, 8, 9)
    ]
    assert return_hashes == [
        candidates[index].candidate_hash for index in (3, 1, 2, 5, 6, 7, 8, 9, 4, 10)
    ]
    assert sharpe_hashes != return_hashes
    assert [
        candidate.metrics["validation"]["sharpe"] for candidate in result.results_by_sharpe
    ] == sorted(
        (candidate.metrics["validation"]["sharpe"] for candidate in result.results_by_sharpe),
        reverse=True,
    )
    assert [
        candidate.metrics["validation"]["annual_return"] for candidate in result.results_by_return
    ] == sorted(
        (
            candidate.metrics["validation"]["annual_return"]
            for candidate in result.results_by_return
        ),
        reverse=True,
    )


def test_validation_pipeline_can_defer_holdout_until_global_shortlist_is_known() -> None:
    request = replace(_request(), holdout_candidates=0)
    candidates = [build_random_candidate(request, __import__("random").Random(5))]
    search = SearchResult(
        results_by_sharpe=tuple(candidates),
        results_by_return=tuple(candidates),
        search_metadata={"evaluated_trials": 1},
    )
    evaluated_splits: list[str] = []

    def evaluate(_candidate, split: str):
        evaluated_splits.append(split)
        return {"annual_return": 0.1, "sharpe": 0.5, "total_trades": 10, "max_drawdown": 0.1}

    result = run_validation_pipeline(search, request, evaluate)

    assert "validation" in evaluated_splits
    assert "holdout" not in evaluated_splits
    assert result.validation_metadata["holdout_candidate_count"] == 0
