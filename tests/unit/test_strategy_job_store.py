from __future__ import annotations

import time

import pytest

from aquest.strategy_discovery.job_store import StrategyJobStore


def test_job_store_creates_atomic_request_and_state_files(tmp_path) -> None:
    store = StrategyJobStore(tmp_path / "jobs")

    store.create(
        "job-1",
        operation="discovery",
        payload={"max_trials": 4},
        description="策略实验室",
    )

    assert store.read_request("job-1") == {
        "job_id": "job-1",
        "operation": "discovery",
        "payload": {"max_trials": 4},
        "description": "策略实验室",
    }
    state = store.read_state("job-1")
    assert state is not None
    assert state["status"] == "queued"
    assert state["result"] is None
    assert state["cancel_requested"] is False


def test_job_store_claims_one_queued_job_and_preserves_progress(tmp_path) -> None:
    first = StrategyJobStore(tmp_path / "jobs")
    second = StrategyJobStore(tmp_path / "jobs")
    first.create("job-1", operation="discovery", payload={}, description="策略实验室")

    assert first.claim_next() == "job-1"
    assert second.claim_next() is None
    first.update(
        "job-1",
        phase="searching",
        progress=0.25,
        total_candidates=10,
        total_scanned=3,
        metadata={"estimated_seconds_remaining": 12.0},
    )

    state = second.read_state("job-1")
    assert state is not None
    assert state["status"] == "running"
    assert state["progress"] == 0.25
    assert state["total_candidates"] == 10
    assert state["total_scanned"] == 3
    assert state["metadata"] == {"estimated_seconds_remaining": 12.0}


def test_job_store_lists_active_jobs_for_a_new_api_reader(tmp_path) -> None:
    store = StrategyJobStore(tmp_path / "jobs")
    store.create("a-queued", operation="discovery", payload={}, description="策略实验室")
    store.create("b-running", operation="refinement", payload={}, description="策略二次优化")
    store.create("c-finished", operation="replay", payload={}, description="策略前十全量复核")
    store.update("c-finished", status="completed")
    assert store.claim_next() == "a-queued"

    active = store.active_snapshots()

    assert [item["job_id"] for item in active] == ["a-queued", "b-running"]
    assert active[0]["operation"] == "discovery"
    assert [item["job_id"] for item in store.active_snapshots({"策略二次优化"})] == ["b-running"]


def test_job_store_cancel_and_complete_are_visible_across_instances(tmp_path) -> None:
    store = StrategyJobStore(tmp_path / "jobs")
    observer = StrategyJobStore(tmp_path / "jobs")
    store.create("job-1", operation="discovery", payload={}, description="策略实验室")
    assert store.claim_next() == "job-1"

    store.request_cancel("job-1")
    assert observer.is_cancel_requested("job-1") is True

    store.write_result("job-1", {"results_by_sharpe": [], "results_by_return": []})
    state = observer.read_state("job-1")
    assert state is not None
    assert state["status"] == "completed"
    assert state["cancel_requested"] is True
    assert observer.read_result("job-1") == {
        "results_by_sharpe": [],
        "results_by_return": [],
    }


def test_job_store_marks_stale_running_job_failed(tmp_path) -> None:
    store = StrategyJobStore(tmp_path / "jobs")
    store.create("job-1", operation="discovery", payload={}, description="策略实验室")
    assert store.claim_next() == "job-1"
    store.update("job-1", heartbeat_at=time.time() - 100)

    assert store.recover_stale_running(stale_after_seconds=10) == ["job-1"]
    state = store.read_state("job-1")
    assert state is not None
    assert state["status"] == "failed"
    assert state["error_code"] == "worker_stale"


def test_job_store_rejects_path_traversal_job_ids(tmp_path) -> None:
    store = StrategyJobStore(tmp_path / "jobs")

    with pytest.raises(ValueError, match="任务 ID"):
        store.create("../escape", operation="discovery", payload={}, description="策略实验室")
