from __future__ import annotations

import time

import pytest

from aquest.market_data.store import DuckDbMarketDataStore
from aquest.strategy_discovery.duckdb_job_store import (
    MAX_CLAIM_REQUEUES,
    DuckDbStrategyJobStore,
)
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
    """轮询任务的读必须走只读快路径：独占打开会与研究进程互相顶死。

    只读打开被打回时会退回读写（见 `test_job_store_reads_survive_a_refused_read_only_open`），
    所以这里盯的是 `_open` 的实参而不是 `_connect`：正常路径上一条读写连接都不该开。
    """

    market_store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store = DuckDbStrategyJobStore(market_store)
    store.create(
        "job-read", operation="market_strategy_pool", payload={"mode": "full"}, description="策略池"
    )
    calls: list[bool] = []
    open_connection = market_store._open

    def track_open(*, read_only: bool):
        calls.append(read_only)
        return open_connection(read_only=read_only)

    monkeypatch.setattr(market_store, "_open", track_open)

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
    # 先续一次租，表示这个任务真的跑起来了：只有"开始过"的任务断心跳才按判死处理。
    store.update_from_worker("job-2", "worker-a", phase="searching", progress=0.1)
    store.update("job-2", heartbeat_at=time.time() - 100)
    assert store.recover_stale_running(stale_after_seconds=10) == ["job-2"]
    assert store.read_state("job-2")["error_code"] == "worker_stale"

    store.create("job-3", operation="discovery", payload={}, description="实验")
    assert store.claim_next("worker-a") == "job-3"
    store.request_cancel("job-3")
    failed = store.fail_from_worker("job-3", "worker-a", "interrupted", "worker_failed")
    assert failed["status"] == "cancelled"
    assert failed["error_code"] == "cancelled"


def test_lost_claim_response_is_replayed_then_requeued(tmp_path) -> None:
    """认领响应丢在网络上时，任务不能变成永远没人跑的孤儿。

    后端在 claim 事务里就把任务标成 running 并绑上 worker_id，worker 却没拿到 job_id：
    之后它每 0.5 秒的 claim 只查 queued，这个任务再也没人执行，120 秒后被判「worker 已停止」，
    用户白等几分钟还要手动重提（2026-10-16 实测三次，全部卡在 121 秒）。
    """

    store = _store(tmp_path)
    store.create("job-lost", operation="discovery", payload={}, description="实验")
    assert store.claim_next("worker-a") == "job-lost"
    # 同一个 worker 再来 claim：必须把同一份任务重新交给它。
    assert store.claim_next("worker-a") == "job-lost"
    # 别的 worker 不能插进来抢这个任务。
    assert store.claim_next("worker-b") is None

    # worker 进程重启（换了 worker_id）：这个任务从没续过租，退回队列让别人领，而不是判死。
    store.update("job-lost", heartbeat_at=time.time() - 600)
    assert store.recover_stale_running(stale_after_seconds=10) == ["job-lost"]
    state = store.read_state("job-lost")
    assert state["status"] == "queued"
    assert state["claim_requeues"] == 1
    assert store.claim_next("worker-c") == "job-lost"


def test_renewed_jobs_are_never_replayed_to_the_same_worker(tmp_path) -> None:
    """已经续过租的任务不能再重放给同一个 worker_id。

    一个容器里可能跑着多个共用 hostname 的 worker 进程：正在被其中一个执行的任务如果又被
    发给另一个，同一份研究会跑两遍、两边还会互相抢租约。
    """

    store = _store(tmp_path)
    store.create("job-busy", operation="discovery", payload={}, description="实验")
    assert store.claim_next("worker-a") == "job-busy"
    store.update_from_worker("job-busy", "worker-a", phase="searching", progress=0.1)
    assert store.claim_next("worker-a") is None


def test_cancelled_orphan_does_not_starve_the_queue(tmp_path) -> None:
    """已被要求取消的孤儿不能占住 claim：否则队列里真正的任务永远等不到 worker。"""

    store = _store(tmp_path)
    store.create("job-cancel", operation="discovery", payload={}, description="实验")
    assert store.claim_next("worker-a") == "job-cancel"
    assert store.request_cancel("job-cancel") is True
    store.create("job-next", operation="discovery", payload={}, description="实验")
    assert store.claim_next("worker-a") == "job-next"


def test_never_renewed_jobs_stop_requeueing_after_the_cap(tmp_path) -> None:
    """worker 与后端之间的通道一直坏着时不能无限重排：超过上限仍按「worker 已停止」判死。"""

    store = _store(tmp_path)
    store.create("job-loop", operation="discovery", payload={}, description="实验")
    for round_index in range(MAX_CLAIM_REQUEUES):
        assert store.claim_next("worker-a") == "job-loop"
        store.update("job-loop", heartbeat_at=time.time() - 600)
        assert store.recover_stale_running(stale_after_seconds=10) == ["job-loop"]
        state = store.read_state("job-loop")
        assert state["status"] == "queued"
        assert state["claim_requeues"] == round_index + 1

    assert store.claim_next("worker-a") == "job-loop"
    store.update("job-loop", heartbeat_at=time.time() - 600)
    assert store.recover_stale_running(stale_after_seconds=10) == ["job-loop"]
    final = store.read_state("job-loop")
    assert final["status"] == "failed"
    assert final["error_code"] == "worker_stale"


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


def _finish_pool_job(
    store: DuckDbStrategyJobStore,
    job_id: str,
    *,
    mode: str,
    status: str,
    started_at: float,
    finished_at: float,
    completed: int,
    skipped: int,
    parallelism: int,
) -> None:
    """按显式时间戳落一条终态任务，模拟 worker 跑完（或中途取消）之后的样子。"""

    operation = "market_strategy_pool_tracking" if mode == "signals" else "market_strategy_pool"
    store.create(job_id, operation=operation, payload={"mode": mode}, description="策略池")
    store.update(
        job_id,
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        metadata={
            "completed_symbols": completed,
            "skipped_symbols": skipped,
            "total_symbols": max(completed, 1),
            "maximum_parallelism": parallelism,
        },
    )


def test_market_pool_history_exposes_duration_and_research_split(tmp_path) -> None:
    """记录只写「X/Y 只完成」，看不出这轮跑了多久、其中几只是真研究。

    completed_symbols 把复用标的也算进去了，页面必须有 duration_seconds 和
    newly_researched_symbols 才能回答「全市场重选到底多慢」。
    """

    store = _store(tmp_path)
    store.create(
        "full-1",
        operation="market_strategy_pool",
        payload={"mode": "full"},
        description="全市场策略池研究",
    )
    assert store.claim_next("pool-worker") == "full-1"
    claimed = store.read_state("full-1")
    assert claimed is not None and claimed["started_at"] is not None
    store.update_from_worker(
        "full-1",
        "pool-worker",
        metadata={
            "completed_symbols": 30,
            "skipped_symbols": 20,
            "failed_symbols": 2,
            "total_symbols": 100,
            "maximum_parallelism": 4,
            "phase_label": "规则 3/7 · 搜索止损、止盈和冷却参数",
        },
    )
    store.write_result_from_worker("full-1", "pool-worker", {})

    item = next(
        row
        for row in store.list_market_strategy_pool_history()["items"]
        if row["job_id"] == "full-1"
    )
    assert item["newly_researched_symbols"] == 10
    assert item["maximum_parallelism"] == 4
    assert item["phase_label"] == "规则 3/7 · 搜索止损、止盈和冷却参数"
    assert item["automatic"] is False
    assert item["duration_includes_queue"] is False
    duration = item["duration_seconds"]
    assert isinstance(duration, float) and duration >= 0.0

    # 认领过的任务才有 started_at；更早入库的行只能从创建时间起算，时长含排队，
    # 必须用 duration_includes_queue 把口径标出来，否则页面会把排队说成研究耗时。
    store.create(
        "legacy-1",
        operation="market_strategy_pool",
        payload={"mode": "full"},
        description="全市场策略池研究",
    )
    store.update(
        "legacy-1",
        status="cancelled",
        created_at=1_000.0,
        finished_at=4_000.0,
        metadata={"completed_symbols": 8, "skipped_symbols": 3},
    )
    legacy = next(
        row
        for row in store.list_market_strategy_pool_history()["items"]
        if row["job_id"] == "legacy-1"
    )
    assert legacy["duration_seconds"] == 3_000.0
    assert legacy["duration_includes_queue"] is True
    assert legacy["newly_researched_symbols"] == 5
    assert legacy["maximum_parallelism"] is None


def test_market_pool_history_stats_average_only_counts_finished_runs(tmp_path) -> None:
    """平均完成时长只取跑完的轮次；取消的半截任务进吞吐样本，失败任务两个都不进。"""

    store = _store(tmp_path)
    _finish_pool_job(
        store,
        "full-20m",
        mode="full",
        status="completed",
        started_at=1_000.0,
        finished_at=2_200.0,
        completed=120,
        skipped=100,
        parallelism=4,
    )
    _finish_pool_job(
        store,
        "full-40m",
        mode="full",
        status="completed",
        started_at=1_000.0,
        finished_at=3_400.0,
        completed=220,
        skipped=100,
        parallelism=4,
    )
    _finish_pool_job(
        store,
        "full-60m",
        mode="full",
        status="completed",
        started_at=1_000.0,
        finished_at=4_600.0,
        completed=400,
        skipped=280,
        parallelism=2,
    )
    # 15 分钟只研究了 10 只就被取消：总时长不算进平均，每只 90 秒算进吞吐。
    _finish_pool_job(
        store,
        "full-cancelled",
        mode="full",
        status="cancelled",
        started_at=1_000.0,
        finished_at=1_900.0,
        completed=18,
        skipped=8,
        parallelism=4,
    )
    _finish_pool_job(
        store,
        "full-failed",
        mode="full",
        status="failed",
        started_at=1_000.0,
        finished_at=6_000.0,
        completed=1,
        skipped=0,
        parallelism=8,
    )
    _finish_pool_job(
        store,
        "signals-1",
        mode="signals",
        status="completed",
        started_at=1_000.0,
        finished_at=1_030.0,
        completed=2,
        skipped=0,
        parallelism=1,
    )

    stats = store.market_pool_job_stats()

    assert stats["sample_limit"] == 60
    full = stats["modes"]["full"]
    assert full["samples"] == 3
    assert full["avg_duration_seconds"] == 2_400.0
    assert full["median_duration_seconds"] == 2_400.0
    assert full["started_at_samples"] == 3
    # 每只新研究标的：60、20、30、90 秒
    assert full["throughput_samples"] == 4
    assert full["avg_seconds_per_symbol"] == 50.0
    assert full["median_seconds_per_symbol"] == 45.0
    assert full["avg_parallelism"] == 3.5
    assert stats["modes"]["signals"]["samples"] == 1
    assert stats["modes"]["signals"]["avg_duration_seconds"] == 30.0
    assert stats["modes"]["single"]["samples"] == 0
    assert stats["modes"]["single"]["avg_duration_seconds"] is None
    assert stats["modes"]["single"]["throughput_samples"] == 0


def test_signal_results_keep_only_the_last_scan_per_trading_day(tmp_path) -> None:
    """每十分钟一次的跟踪扫描，只留每个交易日最后一份，窗口最近 10 个交易日。

    2026-10-19 实测：库里 3,063 条已完成的跟踪结果共 2.58 GB，占整库（4.1 GB）的 62%，
    而回看买卖信号只需要每天那一份。任务行必须原样留着——历史列表和耗时平均值都靠它。
    """

    store = _store(tmp_path)

    def scan(job_id: str, as_of: str) -> None:
        store.create(
            job_id,
            operation="market_strategy_pool_tracking",
            payload={"mode": "signals", "as_of_date": as_of},
            description="策略池双目标跟踪",
        )
        store.write_result(
            job_id,
            {
                "mode": "signals",
                "as_of_date": as_of,
                "buy_signals": [{"symbol": "SH600000"}],
            },
        )

    days = [f"2026-10-{day:02d}" for day in range(1, 13)]
    for index, day in enumerate(days):
        scan(f"s-{index:02d}-1", day)
        scan(f"s-{index:02d}-2", day)
    # 最新那个交易日又扫了一次：这一天的「最后一次」应该换成它
    scan("s-11-3", days[-1])

    # 全市场研究那条不是扫描结果，不该被这条规则碰
    store.create(
        "research-1",
        operation="market_strategy_pool",
        payload={"mode": "full"},
        description="全市场策略池研究",
    )
    store.write_result("research-1", {"mode": "full", "results_by_objective": {}})

    assert store.read_result("s-00-2") is None
    assert store.read_result("s-01-2") is None
    assert store.read_state("s-00-2") is not None
    assert store.read_state("s-00-2")["status"] == "completed"
    assert store.read_result("research-1") is not None

    days_back = store.pool_signal_days()
    assert [item["as_of_date"] for item in days_back] == list(reversed(days[2:]))
    assert days_back[0]["job_id"] == "s-11-3"
    assert days_back[1]["job_id"] == "s-10-2"
    # 每天那份留着：第二新的交易日与最旧那条可回看的日期都在
    assert store.read_result("s-10-2") is not None
    assert store.read_result("s-02-2") is not None
    # 同一天被顶掉的那次全市场扫描，结果已经摘掉，任务行还在
    assert store.read_result("s-11-2") is None
    assert store.read_state("s-11-2") is not None


def test_incremental_signal_results_are_never_pruned(tmp_path) -> None:
    """个股级信号刷新的结果不参与裁剪：它们才几 KB，而且当天面板要合并着用。

    面板显示的最新一天 = 当天最后一次全市场扫描 + 之后逐只重跑的增量结果。把增量也裁掉，
    「当天」那一栏就会少标的，所以规则只针对没有 market/code 的全市场扫描。
    """

    store = _store(tmp_path)

    def create(job_id: str, payload: dict[str, object], as_of: str) -> None:
        store.create(
            job_id,
            operation="market_strategy_pool_tracking",
            payload=payload,
            description="策略池双目标跟踪",
        )
        store.write_result(job_id, {"mode": "signals", "as_of_date": as_of, "buy_signals": []})

    create("baseline-1", {"mode": "signals", "as_of_date": "2026-10-19"}, "2026-10-19")
    create("baseline-2", {"mode": "signals", "as_of_date": "2026-10-19"}, "2026-10-19")
    create(
        "single-1",
        {"mode": "signals", "as_of_date": "2026-10-19", "market": "SH", "code": "600000"},
        "2026-10-19",
    )
    # 提交时没有目标日的老记录（理论上不存在）：不进日期列表，结果也不裁，避免误伤
    create("no-date", {"mode": "signals"}, "2026-10-19")

    assert store.read_result("baseline-1") is None
    assert store.read_result("baseline-2") is not None
    assert store.read_result("single-1") is not None
    assert store.read_result("no-date") is not None
    # 交易日列表只由全市场扫描构成：个股级那份不该变成一个可选日期
    assert [item["job_id"] for item in store.pool_signal_days()] == ["baseline-2"]
