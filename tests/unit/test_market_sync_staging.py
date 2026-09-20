from __future__ import annotations

import multiprocessing
import os
from concurrent.futures import Future, ProcessPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from selector_app.adapters import market_data_sync as market_data_sync_module
from selector_app.adapters.market_data_sync import MarketDataSync
from selector_app.adapters.market_sync import MarketSyncConfig
from selector_app.market_data.staging import (
    _DEFAULT_REQUEST_INTERVAL_SECONDS,
    ProcessRateGate,
    ProviderStagePlan,
    StageTarget,
    StageWorkerRequest,
    StageWorkerResult,
    StageWriter,
    cleanup_stale_staging,
    create_rate_state,
    rate_state_snapshot,
    run_stage_worker,
    staging_directory,
)
from selector_app.market_data.store import DuckDbMarketDataStore


def _target(code: str = "600000") -> StageTarget:
    return StageTarget(
        provider="eastmoney",
        market="SH",
        code=code,
        instrument_type="stock",
        board="main",
        name="浦发银行",
        requested_ranges=((date(2024, 1, 1), date(2024, 1, 31)),),
        existing_first_date=None,
        existing_last_date=None,
        existing_bar_count=0,
        existing_latest_rows=(),
    )


def _staged_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
            "open": [10.0, 11.0],
            "high": [11.0, 12.0],
            "low": [9.0, 10.0],
            "close": [10.5, 11.5],
            "volume": [1000.0, 1200.0],
            "amount": [10000.0, 13800.0],
            "bar_status": ["completed", "completed"],
        }
    )


def test_stage_writer_uses_an_independent_duckdb_file(tmp_path: Path) -> None:
    main = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    stage_path = tmp_path / "worker-0.duckdb"

    with StageWriter(stage_path) as writer:
        written = writer.write_target(
            _target(),
            _staged_frame(),
            status="updated",
            range_start="2024-01-02",
            range_end="2024-01-03",
            retries=0,
            history_source="eastmoney",
        )

    assert written == 2
    assert main.status().bar_count == 0
    assert stage_path.is_file()
    assert StageWriter.read_manifest(stage_path)[0]["code"] == "600000"


def test_stage_merge_preserves_local_rows_and_is_idempotent(tmp_path: Path) -> None:
    main = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    local = _staged_frame()
    main.replace_local_bars("SH", "600000", "stock", local.iloc[[0]].copy())
    stage_path = tmp_path / "worker-0.duckdb"

    with StageWriter(stage_path) as writer:
        writer.write_target(
            _target(),
            _staged_frame(),
            status="updated",
            range_start="2024-01-02",
            range_end="2024-01-03",
            retries=0,
            history_source="eastmoney",
        )

    first = main.merge_online_stage(stage_path)
    second = main.merge_online_stage(stage_path)
    bars = main.read_bars("SH", "600000")

    assert first.merged_rows == 1
    assert second.merged_rows == 1
    assert len(bars) == 2
    assert list(bars["source"]) == ["local", "online"]
    assert list(bars["close"]) == [10.5, 11.5]


def test_cleanup_stale_staging_removes_only_old_job_directories(tmp_path: Path) -> None:
    staging_root = tmp_path / ".sync-staging"
    old_job = staging_root / "old-job"
    fresh_job = staging_root / "fresh-job"
    old_job.mkdir(parents=True)
    fresh_job.mkdir(parents=True)
    old_timestamp = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()
    os.utime(old_job, (old_timestamp, old_timestamp))

    removed = cleanup_stale_staging(staging_root, max_age_seconds=60)

    assert removed == (old_job,)
    assert not old_job.exists()
    assert fresh_job.exists()


def test_stage_worker_entrypoint_runs_in_a_spawned_process(tmp_path: Path) -> None:
    manager = multiprocessing.Manager()
    rate_state, rate_lock = create_rate_state(manager)
    request = StageWorkerRequest(
        worker_id=0,
        stage_path=str(tmp_path / "worker-0.duckdb"),
        provider="eastmoney",
        targets=(),
        config=MarketSyncConfig(),
        as_of=datetime(2026, 9, 12, 16, 0, tzinfo=timezone.utc),
        cancel_event=manager.Event(),
        rate_state=rate_state,
        rate_lock=rate_lock,
        event_queue=manager.Queue(),
    )
    try:
        with ProcessPoolExecutor(
            max_workers=1,
            mp_context=multiprocessing.get_context("spawn"),
        ) as executor:
            result = executor.submit(run_stage_worker, request).result(timeout=10)
    finally:
        manager.shutdown()

    assert result.processed == 0
    assert Path(result.stage_path).is_file()


def test_process_rate_gate_backoffs_and_recovers_per_provider() -> None:
    manager = multiprocessing.Manager()
    try:
        rate_state, rate_lock = create_rate_state(manager)
        from selector_app.market_data.staging import ProcessRateGate

        gate = ProcessRateGate(rate_state, rate_lock)
        gate.report(provider="eastmoney", success=False, throttled=True)
        backed_off = rate_state_snapshot(rate_state)
        for _ in range(32):
            gate.report(provider="eastmoney", success=True)
        recovered = rate_state_snapshot(rate_state)
    finally:
        manager.shutdown()

    assert backed_off["rate_limit_events"] == 1
    assert backed_off["backoffs"] == 1
    assert (
        recovered["by_provider"]["eastmoney"]["request_interval_seconds"]
        < backed_off["by_provider"]["eastmoney"]["request_interval_seconds"]
    )


def test_process_rate_gate_tracks_the_tencent_source() -> None:
    """腾讯是日线主源，限流状态里必须有它那一份。

    只给东财建状态的话，worker 第一次请求就会 ``KeyError: next_allowed_tencent``，
    整个进程池同步一起失败——而且崩溃前还会把请求误记到东财的桶里。
    """

    manager = multiprocessing.Manager()
    try:
        rate_state, rate_lock = create_rate_state(manager)
        gate = ProcessRateGate(rate_state, rate_lock)
        gate.wait("tencent")
        gate.report(provider="tencent", success=False, throttled=True)
        gate.wait("tencent")
        snapshot = rate_state_snapshot(rate_state)
    finally:
        manager.shutdown()

    assert snapshot["rate_limit_events"] == 1
    assert snapshot["backoffs"] == 1
    tencent = snapshot["by_provider"]["tencent"]
    # 退避必须只作用在被限流的那个源上，东财的间隔不受影响。
    assert tencent["request_interval_seconds"] > _DEFAULT_REQUEST_INTERVAL_SECONDS
    assert (
        snapshot["by_provider"]["eastmoney"]["request_interval_seconds"]
        == _DEFAULT_REQUEST_INTERVAL_SECONDS
    )


def test_process_rate_gate_backfills_a_provider_missing_from_the_state() -> None:
    """旧进程传来的状态可能缺某个 provider，不能让它直接 KeyError。"""

    manager = multiprocessing.Manager()
    try:
        rate_state = manager.dict({"next_allowed_eastmoney": 0.0})
        gate = ProcessRateGate(rate_state, manager.Lock())
        gate.wait("tencent")
        gate.report(provider="tencent", success=True)
        interval = float(rate_state["interval_tencent"])
        successes = int(rate_state["successes_tencent"])
    finally:
        manager.shutdown()

    assert interval > 0
    assert successes == 1


def test_market_data_sync_merges_staged_worker_results(monkeypatch, tmp_path: Path) -> None:
    class StagedProvider:
        def supports_process_staging(self):
            return True

        def prepare_stage_plan(self, config):
            del config
            return ProviderStagePlan(
                provider="eastmoney",
                targets=(_target(),),
                listed_instruments=1,
                filtered_instruments=0,
                new_instruments=0,
                existing_instruments=1,
                target_source="test",
                provider_version="test",
                instrument_list_fingerprint="fingerprint",
                as_of=datetime(2026, 9, 12, 16, 0, tzinfo=timezone.utc),
            )

        def sync(self, *args, **kwargs):
            raise AssertionError("staged provider should not use direct sync")

    class ImmediateExecutor:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        def submit(self, function, request):
            future = Future()
            try:
                future.set_result(function(request))
            except Exception as exc:
                future.set_exception(exc)
            return future

        def shutdown(self, **kwargs):
            del kwargs

    def fake_worker(request: StageWorkerRequest) -> StageWorkerResult:
        with StageWriter(request.stage_path) as writer:
            rows = writer.write_target(
                request.targets[0],
                _staged_frame(),
                status="updated",
                range_start="2024-01-02",
                range_end="2024-01-03",
                retries=0,
                history_source="eastmoney",
            )
        return StageWorkerResult(
            worker_id=request.worker_id,
            stage_path=request.stage_path,
            processed=1,
            updated_files=1,
            unchanged_files=0,
            staged_rows=rows,
            errors=0,
            failure_reasons={},
            history_sources={"eastmoney": 1},
        )

    monkeypatch.setattr(market_data_sync_module, "ProcessPoolExecutor", ImmediateExecutor)
    monkeypatch.setattr(market_data_sync_module, "run_stage_worker", fake_worker)
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    report = MarketDataSync(
        store=store,
        eastmoney_sync=StagedProvider(),
    ).sync(MarketSyncConfig(universe="sh", sync_workers=8))

    assert report.execution_mode == "process_pool_staging"
    assert report.worker_count == 1
    assert report.staged_rows == 2
    assert report.merged_rows == 2
    assert report.written_bars == 2
    assert store.status().bar_count == 2
    assert not any(staging_directory(store.database_path).glob("*/worker-*.duckdb"))
