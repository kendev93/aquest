from __future__ import annotations

from collections.abc import Mapping

from selector_app.strategy_discovery.job_store import StrategyJobStore
from selector_app.strategy_discovery.worker import StrategyDiscoveryWorker


class FakeDiscoveryService:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(self, request, *, progress_callback=None, abort_check=None):
        self.calls.append("discovery")
        if progress_callback is not None:
            progress_callback(1, 2, "searching", {"overall_progress": 0.5})
        return {"request": request.to_dict(), "results_by_sharpe": [], "results_by_return": []}

    def refine(
        self,
        experiment: Mapping[str, object],
        parent_candidate_hash: str,
        *,
        max_trials=None,
        seed=None,
        radius="medium",
        progress_callback=None,
        abort_check=None,
    ):
        self.calls.append("refinement")
        return {"history_id": "refined", "results_by_sharpe": [], "results_by_return": []}

    def replay(
        self,
        experiment: Mapping[str, object],
        leaderboard: str,
        *,
        progress_callback=None,
        abort_check=None,
    ):
        self.calls.append(f"replay:{leaderboard}")
        return {"experiment_id": "experiment-1", "leaderboard": leaderboard, "candidates": []}


def _worker(
    tmp_path,
    service: FakeDiscoveryService,
) -> tuple[StrategyJobStore, StrategyDiscoveryWorker]:
    store = StrategyJobStore(tmp_path / "jobs")
    return store, StrategyDiscoveryWorker(store, service, poll_interval_seconds=0.001)


def test_worker_claims_discovery_and_persists_progress_and_result(tmp_path) -> None:
    service = FakeDiscoveryService()
    store, worker = _worker(tmp_path, service)
    store.create(
        "job-1",
        operation="discovery",
        payload={"request": {"base_config": {}, "splits": {}, "max_trials": 1}},
        description="策略实验室",
    )

    assert worker.run_once() == "job-1"
    assert service.calls == ["discovery"]
    state = store.read_state("job-1")
    assert state is not None
    assert state["status"] == "completed"
    assert state["progress"] == 1.0
    assert state["metadata"]["overall_progress"] == 0.5
    assert store.read_result("job-1")["results_by_sharpe"] == []


def test_worker_dispatches_refinement_and_replay_operations(tmp_path) -> None:
    service = FakeDiscoveryService()
    store, worker = _worker(tmp_path, service)
    store.create(
        "job-1",
        operation="refinement",
        payload={
            "experiment": {"history_id": "parent-1"},
            "parent_candidate_hash": "candidate-1",
            "max_trials": 4,
            "radius": "medium",
        },
        description="策略二次优化",
    )
    store.create(
        "job-2",
        operation="replay",
        payload={"experiment": {"history_id": "experiment-1"}, "leaderboard": "return"},
        description="策略前十全量复核",
    )

    assert worker.run_once() == "job-1"
    assert worker.run_once() == "job-2"
    assert service.calls == ["refinement", "replay:return"]
    assert store.read_state("job-1")["status"] == "completed"
    assert store.read_state("job-2")["status"] == "completed"


def test_worker_returns_none_when_no_job_is_queued(tmp_path) -> None:
    store, worker = _worker(tmp_path, FakeDiscoveryService())

    assert worker.run_once() is None
