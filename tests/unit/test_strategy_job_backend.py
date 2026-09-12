from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path

from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.screening.jobs import ScreenJobRunner
from selector_app.strategy_discovery.job_backend import (
    FileStrategyJobBackend,
    InProcessStrategyJobBackend,
)
from selector_app.strategy_discovery.job_store import StrategyJobStore
from selector_app.strategy_discovery.models import DiscoveryRequest


class FakeDiscoveryService:
    def run(self, request: DiscoveryRequest, *, progress_callback=None, abort_check=None):
        if progress_callback is not None:
            progress_callback(1, 1, "completed", {"overall_progress": 1.0})
        return {
            "request": request.to_dict(),
            "results_by_sharpe": [],
            "results_by_return": [],
            "search_metadata": {"evaluated_trials": 1},
            "validation_metadata": {},
            "algorithm_metadata": {},
            "data_fingerprint": "",
        }

    def refine(self, experiment: Mapping[str, object], parent_candidate_hash: str, **kwargs):
        return {
            "request": experiment.get("request", {}),
            "results_by_sharpe": [],
            "results_by_return": [],
            "search_metadata": {"evaluated_trials": 1},
            "validation_metadata": {},
            "algorithm_metadata": {},
            "data_fingerprint": "",
        }

    def replay(self, experiment: Mapping[str, object], leaderboard: str, **kwargs):
        return {
            "experiment_id": str(experiment.get("history_id") or "experiment-1"),
            "leaderboard": leaderboard,
            "data_start": "2024-01-01",
            "data_end": "2024-01-03",
            "data_fingerprint": "",
            "candidates": [],
        }


def _request() -> DiscoveryRequest:
    return DiscoveryRequest(
        base_config={"universe": "all"},
        splits={"search": {"start_date": "2024-01-01", "end_date": "2024-01-03"}},
        max_trials=1,
        validation_candidates=1,
        holdout_candidates=1,
        min_trades=0,
        max_drawdown=None,
        multifidelity_enabled=False,
        seed=1,
    )


def test_file_backend_submits_a_persistent_job_without_running_it(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    backend = FileStrategyJobBackend(
        store,
        StrategyJobStore(tmp_path / "jobs"),
        worker_count=2,
    )

    job_id = backend.submit(
        "discovery",
        {"request": _request().to_dict()},
        description="策略实验室",
    )

    state = backend.get(job_id)
    assert state is not None
    assert state["status"] == "queued"
    assert state["worker_count"] == 2
    assert backend.result(job_id) is None


def test_in_process_backend_preserves_existing_fake_service_contract(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    runner = ScreenJobRunner(max_workers=1)
    backend = InProcessStrategyJobBackend(store, runner, FakeDiscoveryService())
    try:
        job_id = backend.submit(
            "discovery",
            {"request": _request().to_dict()},
            description="策略实验室",
        )
        for _ in range(100):
            state = backend.get(job_id)
            if state is not None and state["status"] in {"completed", "failed"}:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("job did not finish")

        assert state["status"] == "completed"
        assert backend.result(job_id)["history_id"] == job_id
        assert store.read_strategy_discovery_history(job_id) is not None
    finally:
        runner.shutdown()
