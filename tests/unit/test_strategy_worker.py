from __future__ import annotations

from collections.abc import Mapping

from selector_app.strategy_discovery.job_store import StrategyJobStore
from selector_app.strategy_discovery.worker import StrategyDiscoveryWorker
from selector_app.strategy_discovery.worker_api_client import TransientWorkerApiError


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
    assert state["metadata"]["overall_progress"] == 1.0
    result = store.read_result("job-1")
    assert result is not None
    assert result["history_id"] == "job-1"
    assert result["results_by_sharpe"] == []


def test_worker_dispatches_and_completes_risk_optimization_operation(tmp_path) -> None:
    service = FakeDiscoveryService()
    store, worker = _worker(tmp_path, service)
    store.create(
        "risk-job",
        operation="risk_optimization",
        payload={
            "request": {
                "base_config": {"ranking_value": "technical.ma5"},
                "splits": {},
                "max_trials": 1,
                "candidate_mode": "risk_only",
            }
        },
        description="组合风险寻优",
    )

    assert worker.run_once() == "risk-job"
    assert service.calls == ["discovery"]
    assert store.read_state("risk-job")["status"] == "completed"
    assert store.read_result("risk-job")["mode"] == "risk_optimization"


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


def test_worker_survives_a_transient_claim_failure(tmp_path) -> None:
    """A failed poll must not end the worker process.

    The backend restarts holding the DuckDB write lock for a moment; claim
    raises then. Letting that escape killed the worker, so every queued
    backtest sat unclaimed until Docker restarted the container.
    """

    service = FakeDiscoveryService()
    store = StrategyJobStore(tmp_path / "jobs")
    worker = StrategyDiscoveryWorker(store, service, poll_interval_seconds=0.001)
    store.create(
        "job-1",
        operation="discovery",
        payload={"request": {"base_config": {}, "splits": {}, "max_trials": 1}},
        description="策略实验室",
    )

    attempts = {"count": 0}
    original_claim = store.claim_next

    def flaky_claim() -> str | None:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("策略 worker API 请求失败")
        return original_claim()

    store.claim_next = flaky_claim  # type: ignore[method-assign]

    worker.run_forever(stop_event=_StopAfter(4))

    assert attempts["count"] >= 2
    assert service.calls == ["discovery"]
    assert store.read_state("job-1")["status"] == "completed"


def test_worker_continues_when_a_transient_progress_update_fails(tmp_path) -> None:
    class ProgressFlakyStore(StrategyJobStore):
        failed_once = False

        def update(self, job_id, fields=None, **updates):
            if "progress" in updates and not self.failed_once:
                self.failed_once = True
                raise TransientWorkerApiError("temporary progress API timeout")
            super().update(job_id, fields, **updates)

    service = FakeDiscoveryService()
    store = ProgressFlakyStore(tmp_path / "jobs")
    worker = StrategyDiscoveryWorker(store, service)
    store.create(
        "job-1",
        operation="discovery",
        payload={"request": {"base_config": {}, "splits": {}, "max_trials": 1}},
        description="策略实验室",
    )

    assert worker.run_once() == "job-1"
    assert store.failed_once is True
    assert store.read_state("job-1")["status"] == "completed"


def test_worker_throttles_bursty_progress_updates(tmp_path) -> None:
    """逐 trial 的进度回调必须被时间节流：单股重跑没有池协调器，不节流会退
    化成每 trial 一次 HTTP + 后端 DuckDB 写，实测把寻优拖慢约 4 倍。"""

    class BurstProgressService:
        def __init__(self, bursts: int) -> None:
            self.bursts = bursts

        def run(self, request, *, progress_callback=None, abort_check=None):
            for index in range(self.bursts):
                if progress_callback is not None:
                    progress_callback(
                        index + 1,
                        self.bursts,
                        "searching",
                        {"overall_progress": (index + 1) / self.bursts},
                    )
            return {"request": request.to_dict(), "results_by_sharpe": [], "results_by_return": []}

    class CountingStore(StrategyJobStore):
        def __init__(self, path) -> None:  # type: ignore[no-untyped-def]
            super().__init__(path)
            self.progress_updates = 0

        def update(self, job_id, fields=None, **updates):
            merged = {**dict(fields or {}), **updates}
            if merged.get("phase") == "searching":
                self.progress_updates += 1
            return super().update(job_id, fields, **updates)

    bursts = 20
    service = BurstProgressService(bursts)
    store = CountingStore(tmp_path / "jobs")
    worker = StrategyDiscoveryWorker(store, service)
    store.create(
        "job-1",
        operation="discovery",
        payload={"request": {"base_config": {}, "splits": {}, "max_trials": 1}},
        description="策略实验室",
    )

    assert worker.run_once() == "job-1"
    # 一秒节流窗内的 20 连发只放行第一次;留 1 次余量防慢机器偶发跨窗。
    assert 1 <= store.progress_updates <= 2
    assert store.read_state("job-1")["status"] == "completed"


class _StopAfter:
    """Stop the polling loop after ``limit`` iterations, whatever happened."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._seen = 0

    def is_set(self) -> bool:
        self._seen += 1
        return self._seen > self._limit


def test_worker_factory_receives_the_persisted_worker_count(tmp_path) -> None:
    service = FakeDiscoveryService()
    store = StrategyJobStore(tmp_path / "jobs")
    selected: list[int | None] = []
    worker = StrategyDiscoveryWorker(
        store,
        service,
        service_factory=lambda worker_count: selected.append(worker_count) or service,
        poll_interval_seconds=0.001,
    )
    store.create(
        "job-1",
        operation="discovery",
        payload={"request": {"base_config": {}, "splits": {}, "max_trials": 1}},
        description="策略实验室",
        worker_count=1,
    )

    assert worker.run_once() == "job-1"
    assert selected == [1]
