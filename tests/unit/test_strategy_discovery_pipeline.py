from __future__ import annotations

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
