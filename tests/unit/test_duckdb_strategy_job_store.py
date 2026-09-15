from __future__ import annotations

import time

import pytest

from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.strategy_discovery.duckdb_job_store import DuckDbStrategyJobStore
from selector_app.strategy_discovery.job_store import StrategyJobStore


def _store(tmp_path) -> DuckDbStrategyJobStore:
    return DuckDbStrategyJobStore(DuckDbMarketDataStore(tmp_path / "market.duckdb"))


def test_duckdb_job_store_persists_lifecycle_across_instances(tmp_path) -> None:
    first = _store(tmp_path)
    first.create(
        "job-1",
        operation="discovery",
        payload={"request": {"max_trials": 4}},
        description="策略实验室",
        worker_count=2,
    )

    assert first.claim_next("worker-a") == "job-1"
    first.update_from_worker(
        "job-1",
        "worker-a",
        phase="searching",
        progress=0.25,
        total_candidates=4,
        total_scanned=1,
        metadata={"stage": "coarse"},
    )
    first.write_result_from_worker("job-1", "worker-a", {"history_id": "job-1"})

    restarted = _store(tmp_path)
    state = restarted.read_state("job-1")
    assert state is not None
    assert state["status"] == "completed"
    assert state["progress"] == 1.0
    assert state["worker_count"] == 2
    assert state["metadata"]["stage"] == "coarse"
    assert restarted.read_request("job-1") == {
        "job_id": "job-1",
        "operation": "discovery",
        "payload": {"request": {"max_trials": 4}},
        "description": "策略实验室",
    }
    assert restarted.read_result("job-1") == {"history_id": "job-1"}


def test_duckdb_job_store_claim_is_exclusive_and_worker_owned(tmp_path) -> None:
    first = _store(tmp_path)
    second = _store(tmp_path)
    first.create("job-1", operation="replay", payload={}, description="复核")

    assert first.claim_next("worker-a") == "job-1"
    assert second.claim_next("worker-b") is None
    with pytest.raises(ValueError, match="worker"):
        second.update_from_worker("job-1", "worker-b", phase="replaying")

    state = second.update_from_worker("job-1", "worker-a", phase="replaying", progress=0.5)
    assert state["status"] == "running"
    assert state["progress"] == 0.5


def test_duckdb_job_store_cancel_heartbeat_and_stale_recovery(tmp_path) -> None:
    store = _store(tmp_path)
    store.create("job-1", operation="discovery", payload={}, description="实验")
    assert store.claim_next("worker-a") == "job-1"

    assert store.request_cancel("job-1") is True
    assert store.heartbeat("job-1", "worker-a")["cancel_requested"] is True
    assert store.cancel_from_worker("job-1", "worker-a")["status"] == "cancelled"
    assert store.request_cancel("job-1") is False

    store.create("job-2", operation="discovery", payload={}, description="实验")
    assert store.claim_next("worker-a") == "job-2"
    store.update("job-2", heartbeat_at=time.time() - 100)
    assert store.recover_stale_running(stale_after_seconds=10) == ["job-2"]
    assert store.read_state("job-2")["error_code"] == "worker_stale"

    store.create("job-3", operation="discovery", payload={}, description="实验")
    assert store.claim_next("worker-a") == "job-3"
    store.request_cancel("job-3")
    failed = store.fail_from_worker("job-3", "worker-a", "interrupted", "worker_failed")
    assert failed["status"] == "cancelled"
    assert failed["error_code"] == "cancelled"


def test_duckdb_job_store_imports_all_legacy_states_idempotently(tmp_path) -> None:
    legacy = StrategyJobStore(tmp_path / "legacy-jobs")
    legacy.create("queued", operation="discovery", payload={"a": 1}, description="排队")
    legacy.create("running", operation="refinement", payload={"b": 2}, description="运行")
    legacy.claim_next()
    legacy.create("completed", operation="replay", payload={"c": 3}, description="完成")
    legacy.write_result("completed", {"candidate_count": 2})
    legacy.create("failed", operation="discovery", payload={}, description="失败")
    legacy.update("failed", status="failed", error="legacy error", error_code="worker_failed")
    legacy.create("cancelled", operation="discovery", payload={}, description="取消")
    legacy.request_cancel("cancelled")
    legacy.update("cancelled", status="cancelled", error_code="cancelled")

    target = _store(tmp_path)
    assert target.import_legacy_jobs(tmp_path / "legacy-jobs") == 5
    assert target.import_legacy_jobs(tmp_path / "legacy-jobs") == 0

    assert {item["status"] for item in target.list_snapshots()} == {
        "queued",
        "running",
        "completed",
        "failed",
        "cancelled",
    }
    assert target.read_request("queued")["payload"] == {"a": 1}
    assert target.read_result("completed") == {"candidate_count": 2}
    assert target.read_state("failed")["error"] == "legacy error"
    assert target.read_state("cancelled")["cancel_requested"] is True


def test_duckdb_job_store_rejects_invalid_job_ids_and_legacy_json(tmp_path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="任务 ID"):
        store.create("../outside", operation="discovery", payload={}, description="实验")

    legacy_root = tmp_path / "broken"
    bad_job = legacy_root / "job-1"
    bad_job.mkdir(parents=True)
    (bad_job / "request.json").write_text("not-json", encoding="utf-8")
    (bad_job / "state.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="文件无效"):
        store.import_legacy_jobs(legacy_root)
