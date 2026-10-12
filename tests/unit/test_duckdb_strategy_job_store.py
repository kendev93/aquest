from __future__ import annotations

import time

import pytest

from aquest.market_data.store import DuckDbMarketDataStore
from aquest.strategy_discovery.duckdb_job_store import DuckDbStrategyJobStore
from aquest.strategy_discovery.job_store import StrategyJobStore


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


def test_polling_job_metadata_uses_read_only_connections(tmp_path, monkeypatch) -> None:
    market_store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store = DuckDbStrategyJobStore(market_store)
    store.create(
        "job-read", operation="market_strategy_pool", payload={"mode": "full"}, description="策略池"
    )
    calls: list[bool | None] = []
    open_connection = market_store._connect

    def track_connection(*, read_only=None):
        calls.append(read_only)
        return open_connection(read_only=read_only)

    monkeypatch.setattr(market_store, "_connect", track_connection)

    assert store.read_request("job-read") is not None
    assert store.read_state("job-read") is not None
    assert store.recover_stale_running(stale_after_seconds=120) == []

    assert calls == [True, True, True]


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


def test_cancel_requested_before_atomic_completion_wins_and_discards_result(tmp_path) -> None:
    store = _store(tmp_path)
    store.create("job-final-race", operation="discovery", payload={}, description="实验")
    assert store.claim_next("worker-a") == "job-final-race"
    assert store.request_cancel("job-final-race") is True

    state = store.write_result_from_worker(
        "job-final-race", "worker-a", {"should_not_publish": True}
    )

    assert state["status"] == "cancelled"
    assert state["cancel_requested"] is True
    assert store.read_result("job-final-race") is None


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


def test_market_pool_history_keeps_running_research_above_tracking_noise(tmp_path) -> None:
    """跟踪轮询每十分钟写一条记录，不能把跑了几小时的研究挤出列表第一页。

    前端靠这份列表恢复运行中任务的进度显示；列表按创建时间倒序截一页，
    tracking 记录很快填满窗口，长任务就从列表里"消失"了——页面不再显示
    进度，用户会以为研究停了。第一页必须先带上全部非终态任务，再补终态历史。
    """

    store = _store(tmp_path)
    store.create(
        "research-running",
        operation="market_strategy_pool",
        payload={"mode": "full"},
        description="全市场策略池研究",
    )
    store.claim_next("pool-worker")
    for index in range(25):
        job_id = f"tracking-{index:02d}"
        store.create(
            job_id,
            operation="market_strategy_pool_tracking",
            payload={"mode": "signals"},
            description="跟踪轮询",
        )
        store.claim_next("tracking-worker")
        store.write_result_from_worker(job_id, "tracking-worker", {})

    page = store.list_market_strategy_pool_history(limit=20)

    statuses = {item["job_id"]: item["status"] for item in page["items"]}
    assert statuses.get("research-running") == "running"
    assert len(page["items"]) == 20
    # 终态历史照常按时间倒序补位，不再被活跃任务挤占名额。
    terminal = [item["job_id"] for item in page["items"] if item["status"] != "running"]
    assert terminal == [f"tracking-{index:02d}" for index in range(24, 5, -1)]
