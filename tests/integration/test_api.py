from __future__ import annotations

import time
from pathlib import Path
from threading import Event

import numpy as np
import pandas as pd
from fastapi.testclient import TestClient

from selector_app.market_data.day_importer import LocalDayImporter
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.screening.jobs import TaskCancelledError
from selector_app.screening.models import ScanReport
from selector_app.tdx_protocol.errors import TdxConnectionError
from selector_app.web.app import create_app
from selector_app.web.schemas import FormulaScreenRequest
from tests.day_helpers import write_day_records


class EmptyEngine:
    def scan(self, config, progress_callback=None):
        return ScanReport(
            total_candidates=0,
            total_scanned=0,
            total_signals=0,
            errors=0,
            skipped=0,
            results=(),
            failure_reasons={},
            skip_reasons={},
        )


def wait_for_done(
    client: TestClient,
    job_id: str,
    base_path: str = "/api/v1/formula-screen/jobs",
) -> dict:
    for _ in range(50):
        response = client.get(f"{base_path}/{job_id}")
        assert response.status_code == 200
        payload = response.json()["data"]
        if payload["status"] in {"completed", "failed", "cancelled"}:
            return payload
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def valid_payload(tmp_path: Path) -> dict:
    vipdoc = tmp_path / "vipdoc"
    (vipdoc / "sh" / "lday").mkdir(parents=True, exist_ok=True)
    return {
        "selected_signals": ["indicator_three.accumulation_zone"],
        "combine_mode": "at_least",
        "minimum_matches": 1,
        "universe": "all",
        "vipdoc_path": str(vipdoc),
        "workers": 1,
        "period": "daily",
    }


def _precompute_bars(length: int = 120) -> pd.DataFrame:
    close = 10.0 + np.linspace(0.0, 8.0, length) + np.sin(np.arange(length) / 3.0)
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=length, freq="D"),
            "open": close - 0.2,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.full(length, 100_000.0),
            "amount": close * 100_000.0,
            "bar_status": "completed",
        }
    )


def test_metadata_returns_signals_modes_and_supported_markets() -> None:
    with TestClient(create_app(engine=EmptyEngine())) as client:
        response = client.get("/api/v1/formula-screen/metadata")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["indicators"]
    assert {mode["value"] for mode in data["combine_modes"]} == {"all", "any", "at_least"}
    assert data["supported_markets"] == ["SH", "SZ"]


def test_completed_screen_results_are_saved_to_history(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    with TestClient(create_app(engine=EmptyEngine(), market_data_store=store)) as client:
        response = client.post("/api/v1/formula-screen/jobs", json=valid_payload(tmp_path))
        assert response.status_code == 202
        job_id = response.json()["data"]["job_id"]
        state = wait_for_done(client, job_id)
        results = client.get(f"/api/v1/formula-screen/jobs/{job_id}/results")
        history = client.get("/api/v1/formula-screen/history")

    assert state["status"] == "completed"
    assert results.status_code == 200
    assert history.status_code == 200
    assert history.json()["data"][0]["history_id"] == job_id
    assert history.json()["data"][0]["result_count"] == 0


def test_health_endpoint_is_lightweight_and_does_not_require_vipdoc() -> None:
    with TestClient(create_app(engine=EmptyEngine())) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_app_records_database_maintenance_check_on_startup(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _precompute_bars())
    app = create_app(market_data_store=store, auto_sync_enabled=False)

    with TestClient(app):
        result = app.state.database_maintenance_result

    assert result["status"] == "baseline"
    assert result["record_path"].endswith("market.maintenance.json")


def test_app_schedules_online_sync_when_daily_refresh_is_due(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _precompute_bars())
    calls: list[object] = []

    class FakeMarketSync:
        def sync(self, config, progress_callback=None, *, detail_callback=None):
            del detail_callback
            calls.append(config)
            if progress_callback is not None:
                progress_callback(1, 1)
            store.set_meta(
                "last_online_sync_at",
                pd.Timestamp.now(tz="Asia/Shanghai").isoformat(),
            )
            return {
                "total_candidates": 1,
                "processed": 1,
                "updated_files": 1,
                "unchanged_files": 0,
                "written_bars": 1,
                "errors": 0,
                "failure_reasons": {},
            }

    app = create_app(
        market_data_store=store,
        market_sync=FakeMarketSync(),
        auto_sync_enabled=True,
        auto_sync_interval_seconds=0.01,
    )
    with TestClient(app) as client:
        for _ in range(100):
            if calls and app.state.auto_sync_job_id:
                break
            time.sleep(0.01)
        auto_sync_job_id = app.state.auto_sync_job_id
        assert auto_sync_job_id
        for _ in range(100):
            job_status = client.get(f"/api/v1/market-data/jobs/{auto_sync_job_id}")
            assert job_status.status_code == 200
            if job_status.json()["data"]["status"] in {"completed", "failed"}:
                break
            time.sleep(0.01)
        status = client.get("/api/v1/market-data/store")

    assert len(calls) == 1
    config = calls[0]
    assert config.provider == "akshare"
    assert config.prefer_store_targets is False
    assert config.refresh_names is False
    assert status.status_code == 200
    assert status.json()["data"]["last_online_sync_at"] is not None


def test_formula_screen_request_defaults_to_eight_workers() -> None:
    payload = FormulaScreenRequest(
        selected_signals=["indicator_three.accumulation_zone"],
        combine_mode="any",
        minimum_matches=None,
        universe="all",
        period="daily",
    )

    assert payload.workers == 8


def test_indicator_cache_status_and_manual_precompute_job(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _precompute_bars())

    with TestClient(create_app(market_data_store=store)) as client:
        initial = client.get("/api/v1/market-data/indicator-cache")
        response = client.post(
            "/api/v1/market-data/indicator-cache/precompute",
            json={"universe": "sh"},
        )
        assert response.status_code == 202
        state = wait_for_done(
            client,
            response.json()["data"]["job_id"],
            "/api/v1/market-data/jobs",
        )
        warmed = client.get("/api/v1/market-data/indicator-cache")

    assert initial.status_code == 200
    assert initial.json()["data"]["persistent"]["total_entries"] == 0
    assert state["status"] == "completed"
    assert state["description"] == "指标预计算"
    assert state["result"]["computed"] == 4
    assert warmed.status_code == 200
    assert warmed.json()["data"]["lookups"] == 4
    assert warmed.json()["data"]["p95_ms"] >= warmed.json()["data"]["p50_ms"] >= 0
    assert warmed.json()["data"]["persistent"]["total_entries"] == 4


def test_invalid_signal_is_rejected_without_stack_trace(tmp_path: Path) -> None:
    payload = valid_payload(tmp_path)
    payload["selected_signals"] = ["unknown.signal"]

    with TestClient(create_app(engine=EmptyEngine())) as client:
        response = client.post("/api/v1/formula-screen/jobs", json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert "Traceback" not in response.text


def test_custom_formula_parse_returns_parameters_and_outputs() -> None:
    with TestClient(create_app(engine=EmptyEngine())) as client:
        response = client.post(
            "/api/v1/formula-screen/parse",
            json={"formula_text": "N:=5; BREAKOUT:CROSS(C,REF(C,N));"},
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["parameters"][0]["name"] == "N"
    assert data["parameters"][0]["default"] == 5
    assert data["signals"][0]["id"] == "custom.breakout"
    assert data["values"][0]["id"] == "custom.n"


def test_custom_formula_unsafe_input_is_rejected(tmp_path: Path) -> None:
    payload = valid_payload(tmp_path)
    payload.update(
        {
            "formula_text": "X:__import__('os').system('id');",
            "selected_signals": ["custom.x"],
            "combine_mode": "any",
            "minimum_matches": None,
        }
    )

    with TestClient(create_app(engine=EmptyEngine())) as client:
        response = client.post("/api/v1/formula-screen/jobs", json=payload)

    assert response.status_code == 422
    assert "Traceback" not in response.text


def test_invalid_combine_mode_is_rejected_and_legacy_vipdoc_is_ignored(tmp_path: Path) -> None:
    payload = valid_payload(tmp_path)
    payload["combine_mode"] = "invalid"
    with TestClient(create_app(engine=EmptyEngine())) as client:
        invalid_mode = client.post("/api/v1/formula-screen/jobs", json=payload)
    assert invalid_mode.status_code == 422

    payload = valid_payload(tmp_path)
    payload["vipdoc_path"] = str(tmp_path / "does-not-exist")
    with TestClient(create_app(engine=EmptyEngine())) as client:
        missing_path = client.post("/api/v1/formula-screen/jobs", json=payload)
    assert missing_path.status_code == 202


def test_job_creation_status_and_empty_results() -> None:
    with TestClient(create_app(engine=EmptyEngine())) as client:
        payload = {
            "selected_signals": ["indicator_three.accumulation_zone"],
            "combine_mode": "any",
            "universe": "all",
            "vipdoc_path": "/tmp",
            "workers": 1,
            "period": "daily",
        }
        create_response = client.post("/api/v1/formula-screen/jobs", json=payload)
        assert create_response.status_code == 202
        job_id = create_response.json()["data"]["job_id"]
        state = wait_for_done(client, job_id)
        assert state["status"] == "completed"
        result_response = client.get(f"/api/v1/formula-screen/jobs/{job_id}/results")

    assert result_response.status_code == 200
    assert result_response.json()["data"] == []


def test_unknown_job_uses_the_standard_error_envelope() -> None:
    with TestClient(create_app(engine=EmptyEngine())) as client:
        response = client.get("/api/v1/formula-screen/jobs/not-found")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert "detail" not in response.json()


def test_market_sync_job_can_be_created_and_polled() -> None:
    received = {}

    class FakeMarketSync:
        def sync(self, config, progress_callback=None, *, detail_callback=None):
            del detail_callback
            received["provider"] = config.provider
            received["instrument_types"] = config.instrument_types
            received["boards"] = config.boards
            received["prefer_store_targets"] = config.prefer_store_targets
            if progress_callback:
                progress_callback(1, 1)
            return {
                "total_candidates": 1,
                "processed": 1,
                "updated_files": 1,
                "written_bars": 3,
                "errors": 0,
                "failure_reasons": {},
            }

    with TestClient(create_app(engine=EmptyEngine(), market_sync=FakeMarketSync())) as client:
        response = client.post(
            "/api/v1/market-data/sync-online",
            json={"instrument_types": ["stock"], "boards": ["main"]},
        )
        assert response.status_code == 202
        job_id = response.json()["data"]["job_id"]
        for _ in range(50):
            status_response = client.get(f"/api/v1/market-data/sync/jobs/{job_id}")
            assert status_response.status_code == 200
            state = status_response.json()["data"]
            if state["status"] in {"completed", "failed"}:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("market sync job did not finish")

    assert state["status"] == "completed"
    assert state["result"]["written_bars"] == 3
    assert received == {
        "provider": "akshare",
        "instrument_types": ("stock",),
        "boards": ("main",),
        "prefer_store_targets": False,
    }


def test_market_data_capabilities_exposes_default_provider_and_tdx_compatibility() -> None:
    with TestClient(create_app(engine=EmptyEngine())) as client:
        response = client.get("/api/v1/market-data/capabilities")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["default_provider"] == "akshare"
    assert data["instrument_count"] == 0
    assert data["providers"]["tdx"]["available"] is True
    assert data["providers"]["akshare"]["available"] is False


def test_market_data_active_jobs_can_be_listed_and_cancelled() -> None:
    app = create_app(engine=EmptyEngine(), auto_sync_enabled=False)
    runner = app.state.screen_job_runner
    started = Event()
    job_id_holder = [""]

    def task(_progress):
        started.set()
        while not runner.is_cancel_requested(job_id_holder[0]):
            time.sleep(0.001)
        raise TaskCancelledError("行情同步已取消")

    job_id_holder[0] = runner.submit_callable(task, description="行情同步")
    try:
        assert started.wait(timeout=1)
        with TestClient(app) as client:
            active = client.get("/api/v1/market-data/jobs/active")
            cancel = client.post(f"/api/v1/market-data/jobs/{job_id_holder[0]}/cancel")
            state = wait_for_done(
                client,
                job_id_holder[0],
                "/api/v1/market-data/jobs",
            )

        assert active.status_code == 200
        assert active.json()["data"][0]["job_id"] == job_id_holder[0]
        assert active.json()["data"][0]["description"] == "行情同步"
        assert cancel.status_code == 200
        assert cancel.json()["data"] == {
            "job_id": job_id_holder[0],
            "status": "cancelling",
            "cancel_requested": True,
        }
        assert state["status"] == "cancelled"
    finally:
        runner.shutdown()


def test_market_data_rejects_duplicate_active_sync_jobs() -> None:
    app = create_app(engine=EmptyEngine(), auto_sync_enabled=False)
    runner = app.state.screen_job_runner
    release = Event()

    def task(_progress):
        release.wait(timeout=1)
        return {}

    existing_job = runner.submit_callable(task, description="行情同步")
    try:
        with TestClient(app) as client:
            response = client.post("/api/v1/market-data/sync-online", json={})

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "conflict"
        assert "已有行情同步任务" in response.json()["error"]["message"]
    finally:
        release.set()
        for _ in range(50):
            state = runner.get(existing_job)
            if state is not None and state.status == "completed":
                break
            time.sleep(0.01)
        runner.shutdown()


def test_akshare_backfill_forces_stock_full_history_without_local_import() -> None:
    received: dict[str, object] = {}

    class FakeMarketSync:
        def sync(self, config, progress_callback=None, *, detail_callback=None):
            del detail_callback
            received["provider"] = config.provider
            received["full_history"] = config.full_history
            received["instrument_types"] = config.instrument_types
            received["prefer_store_targets"] = config.prefer_store_targets
            if progress_callback:
                progress_callback(1, 1)
            return {
                "source": "online",
                "provider": "akshare",
                "total_candidates": 1,
                "processed": 1,
                "updated_files": 1,
                "written_bars": 10,
                "errors": 0,
                "failure_reasons": {},
            }

    with TestClient(create_app(engine=EmptyEngine(), market_sync=FakeMarketSync())) as client:
        response = client.post("/api/v1/market-data/backfill", json={})
        assert response.status_code == 202
        state = wait_for_done(client, response.json()["data"]["job_id"], "/api/v1/market-data/jobs")

    assert state["status"] == "completed"
    assert received == {
        "provider": "akshare",
        "full_history": True,
        "instrument_types": ("stock",),
        "prefer_store_targets": False,
    }


def test_one_click_sync_imports_local_vipdoc_before_online_sync(tmp_path: Path) -> None:
    vipdoc = tmp_path / "vipdoc"
    write_day_records(
        vipdoc / "sh" / "lday" / "sh600000.day",
        [(20240102, 10.0, 11.0, 9.0, 10.5, 1000.0, 10000.0)],
    )
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    events: list[str] = []
    real_importer = LocalDayImporter(store)

    class FakeMarketSync:
        def discover_instruments(self, config, *, detail_callback=None):
            del config, detail_callback
            events.append("discover")
            return {
                "source": "online_discovery",
                "total_candidates": 1,
                "listed_instruments": 1,
                "filtered_instruments": 0,
                "new_instruments": 1,
                "existing_instruments": 0,
                "target_source": "tdx_security_list",
            }

        def sync(self, config, progress_callback=None, *, detail_callback=None):
            del detail_callback
            events.append("sync")
            assert config.prefer_store_targets is True
            if progress_callback:
                progress_callback(1, 1)
            return {
                "source": "online",
                "total_candidates": 1,
                "processed": 1,
                "updated_files": 0,
                "unchanged_files": 1,
                "written_bars": 0,
                "errors": 0,
                "failure_reasons": {},
            }

    class TrackingImporter:
        def import_vipdoc(self, *args, **kwargs):
            events.append("local")
            return real_importer.import_vipdoc(*args, **kwargs)

    with TestClient(
        create_app(
            engine=EmptyEngine(),
            market_sync=FakeMarketSync(),
            market_data_store=store,
            local_day_importer=TrackingImporter(),
        )
    ) as client:
        response = client.post(
            "/api/v1/market-data/sync",
            json={"vipdoc_path": str(vipdoc), "universe": "sh"},
        )
        assert response.status_code == 202
        state = wait_for_done(
            client,
            response.json()["data"]["job_id"],
            base_path="/api/v1/market-data/jobs",
        )

    assert state["status"] == "completed"
    assert state["result"]["local_import"]["imported_files"] == 1
    assert state["result"]["online_sync"]["written_bars"] == 0
    assert state["result"]["online_sync"]["listed_instruments"] == 1
    assert events == ["discover", "local", "sync"]
    assert store.status().bar_count == 1


def test_formula_screen_scope_filters_are_forwarded_to_the_engine(tmp_path: Path) -> None:
    received = {}

    class CapturingEngine:
        def scan(self, config, progress_callback=None):
            received["instrument_types"] = config.instrument_types
            received["boards"] = config.boards
            return ScanReport(
                total_candidates=0,
                total_scanned=0,
                total_signals=0,
                errors=0,
                skipped=0,
                results=(),
                failure_reasons={},
                skip_reasons={},
            )

    with TestClient(create_app(engine=CapturingEngine())) as client:
        response = client.post(
            "/api/v1/formula-screen/jobs",
            json={
                **valid_payload(tmp_path),
                "instrument_types": ["stock"],
                "boards": ["main", "chinext"],
            },
        )
        assert response.status_code == 202
        state = wait_for_done(client, response.json()["data"]["job_id"])

    assert state["status"] == "completed"
    assert received == {
        "instrument_types": ("stock",),
        "boards": ("main", "chinext"),
    }


def test_market_sync_rejects_an_invalid_request() -> None:
    with TestClient(create_app(engine=EmptyEngine())) as client:
        response = client.post(
            "/api/v1/market-data/sync-online",
            json={"bars": 0},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_market_sync_reports_incomplete_security_list_without_running_bars() -> None:
    class FailingMarketSync:
        def sync(self, config, progress_callback=None, *, detail_callback=None):
            del config, progress_callback, detail_callback
            raise TdxConnectionError("证券列表分页不完整")

    with TestClient(create_app(engine=EmptyEngine(), market_sync=FailingMarketSync())) as client:
        response = client.post("/api/v1/market-data/sync-online", json={})
        assert response.status_code == 202
        job_id = response.json()["data"]["job_id"]
        for _ in range(50):
            state_response = client.get(f"/api/v1/market-data/jobs/{job_id}")
            assert state_response.status_code == 200
            state = state_response.json()["data"]
            if state["status"] == "failed":
                break
            time.sleep(0.01)
        else:
            raise AssertionError("market sync job did not fail")

    assert state["error"] == "完整证券列表获取失败，本次同步已停止，请检查 TDX 连接后重试。"


def test_backtest_job_can_be_created_and_results_are_available(tmp_path: Path) -> None:
    class FakeBacktestService:
        def run(self, config, progress_callback=None):
            if progress_callback:
                progress_callback(1, 1)
            return {
                "market": "SH",
                "code": "600000",
                "bars": 5,
                "start_date": "2024-01-02",
                "end_date": "2024-01-06",
                "buy_signal": config.buy_signal,
                "sell_signal": config.sell_signal,
                "performance": {"total_return": 0.04, "end_value": 10400.0},
                "equity_curve": [],
                "trades": [],
                "positions": [],
                "diagnostic": None,
            }

    with TestClient(
        create_app(engine=EmptyEngine(), backtest_service=FakeBacktestService())
    ) as client:
        response = client.post(
            "/api/v1/backtests",
            json={
                "market": "SH",
                "code": "600000",
                "vipdoc_path": str(tmp_path),
                "buy_signal": "custom.buy",
                "sell_signal": "custom.sell",
                "formula_text": "BUY:C>10; SELL:C<10;",
                "start_date": "2024-01-02",
                "end_date": "2024-01-06",
            },
        )
        assert response.status_code == 202
        job_id = response.json()["data"]["job_id"]
        state = None
        for _ in range(50):
            status_response = client.get(f"/api/v1/backtests/{job_id}")
            assert status_response.status_code == 200
            state = status_response.json()["data"]
            if state["status"] in {"completed", "failed"}:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("backtest job did not finish")
        results_response = client.get(f"/api/v1/backtests/{job_id}/results")

    assert state is not None
    assert state["status"] == "completed"
    assert state["result"] is None
    assert results_response.status_code == 200
    assert results_response.json()["data"]["code"] == "600000"


def test_backtest_default_service_reads_day_file_and_serializes_result(tmp_path: Path) -> None:
    vipdoc = tmp_path / "vipdoc"
    filepath = vipdoc / "sh/lday/sh600000.day"
    filepath.parent.mkdir(parents=True)
    write_day_records(
        filepath,
        [
            (
                20240100 + index,
                float(close),
                float(close) + 0.5,
                float(close) - 0.5,
                float(close),
                100_000,
                float(close) * 100_000,
            )
            for index, close in enumerate((10, 11, 12, 13, 14, 13, 12, 14, 15, 14), start=1)
        ],
    )
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    LocalDayImporter(store).import_vipdoc(vipdoc)

    with TestClient(create_app(market_data_store=store)) as client:
        response = client.post(
            "/api/v1/backtests",
            json={
                "market": "SH",
                "code": "600000",
                "vipdoc_path": str(vipdoc),
                "buy_signal": "custom.buy",
                "sell_signal": "custom.sell",
                "formula_text": "BUY:C=11; SELL:C=14;",
                "start_date": "2024-01-02",
                "end_date": "2024-01-06",
                "initial_cash": 10_000,
                "commission": 0,
                "min_commission": 0,
                "stamp_tax": 0,
            },
        )
        assert response.status_code == 202
        job_id = response.json()["data"]["job_id"]
        state = wait_for_done(client, job_id, "/api/v1/backtests")
        results_response = client.get(f"/api/v1/backtests/{job_id}/results")
        history_response = client.get("/api/v1/backtests/history")
        history_detail = client.get(f"/api/v1/backtests/history/{job_id}")

    assert state["status"] == "completed"
    payload = results_response.json()["data"]
    assert payload["code"] == "600000"
    assert payload["trades"][0]["date"] == "2024-01-03"
    assert payload["equity_curve"][-1]["date"] == "2024-01-06"
    assert history_response.status_code == 200
    assert history_response.json()["data"][0]["history_id"] == job_id
    assert history_response.json()["data"][0]["started_at"]
    assert history_response.json()["data"][0]["finished_at"]
    assert history_response.json()["data"][0]["summary"]["start_date"] == "2024-01-02"
    assert history_response.json()["data"][0]["summary"]["end_date"] == "2024-01-06"
    assert history_detail.status_code == 200
    assert history_detail.json()["data"]["request"]["code"] == "600000"


def test_backtest_rejects_same_buy_and_sell_signal(tmp_path: Path) -> None:
    with TestClient(create_app(engine=EmptyEngine())) as client:
        response = client.post(
            "/api/v1/backtests",
            json={
                "market": "SH",
                "code": "600000",
                "vipdoc_path": str(tmp_path),
                "buy_signal": "indicator_three.begin_zone",
                "sell_signal": "indicator_three.begin_zone",
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_backtest_task_returns_a_safe_domain_error(tmp_path: Path) -> None:
    class FailingBacktestService:
        def run(self, config, progress_callback=None):
            raise ValueError("指定日期范围内没有可用日线数据")

    with TestClient(
        create_app(engine=EmptyEngine(), backtest_service=FailingBacktestService())
    ) as client:
        response = client.post(
            "/api/v1/backtests",
            json={
                "market": "SH",
                "code": "600000",
                "vipdoc_path": str(tmp_path),
                "buy_signal": "indicator_three.begin_zone",
                "sell_signal": "indicator_three.end_zone",
            },
        )
        assert response.status_code == 202
        job_id = response.json()["data"]["job_id"]
        state = None
        for _ in range(50):
            state_response = client.get(f"/api/v1/backtests/{job_id}")
            state = state_response.json()["data"]
            if state["status"] == "failed":
                break
            time.sleep(0.01)
        else:
            raise AssertionError("backtest job did not fail")

    assert state is not None
    assert state["error"] == "指定日期范围内没有可用日线数据"


def test_portfolio_backtest_job_can_be_created_and_polled(tmp_path: Path) -> None:
    class FakePortfolioService:
        def run(self, config, progress_callback=None):
            if progress_callback:
                progress_callback(2, 2)
            return {
                "universe": "all",
                "total_candidates": 2,
                "processed": 2,
                "skipped": 0,
                "errors": 0,
                "bars": 5,
                "start_date": "2024-01-02",
                "end_date": "2024-01-06",
                "max_positions": config.max_positions,
                "ranking_value": config.ranking_value,
                "rank_order": config.rank_order,
                "performance": {"total_return": 0.12},
                "equity_curve": [],
                "trades": [],
                "states": [],
                "ranking_events": [],
                "failure_reasons": {},
                "diagnostic": None,
            }

    with TestClient(
        create_app(
            engine=EmptyEngine(),
            portfolio_backtest_service=FakePortfolioService(),
            market_data_store=DuckDbMarketDataStore(tmp_path / "market.duckdb"),
        )
    ) as client:
        response = client.post(
            "/api/v1/portfolio-backtests",
            json={
                "vipdoc_path": str(tmp_path),
                "universe": "all",
                "selected_signals": ["custom.buy"],
                "combine_mode": "any",
                "ranking_value": "custom.rank",
                "max_positions": 2,
                "formula_text": "BUY:C>0; RANK:C;",
                "stop_loss_pct": 0.05,
                "fitness_filter_enabled": True,
                "fitness_min_score": 60,
                "fitness_min_trades": 3,
                "fitness_max_drawdown": 0.4,
            },
        )
        assert response.status_code == 202
        job_id = response.json()["data"]["job_id"]
        state = wait_for_done(client, job_id, "/api/v1/portfolio-backtests")
        results_response = client.get(f"/api/v1/portfolio-backtests/{job_id}/results")
        history_response = client.get("/api/v1/portfolio-backtests/history")
        history_detail = client.get(f"/api/v1/portfolio-backtests/history/{job_id}")

    assert state["status"] == "completed"
    assert state["result"] is None
    assert results_response.status_code == 200
    assert results_response.json()["data"]["max_positions"] == 2
    assert history_response.status_code == 200
    assert history_response.json()["data"][0]["history_id"] == job_id
    assert history_response.json()["data"][0]["backtest_type"] == "portfolio"
    assert history_response.json()["data"][0]["summary"]["start_date"] == "2024-01-02"
    assert history_response.json()["data"][0]["summary"]["end_date"] == "2024-01-06"
    assert history_detail.status_code == 200
    assert history_detail.json()["data"]["result"]["max_positions"] == 2


def test_portfolio_backtest_requires_a_sell_rule(tmp_path: Path) -> None:
    with TestClient(create_app(engine=EmptyEngine())) as client:
        response = client.post(
            "/api/v1/portfolio-backtests",
            json={
                "vipdoc_path": str(tmp_path),
                "universe": "all",
                "selected_signals": ["indicator_three.begin_zone"],
                "combine_mode": "any",
                "ranking_value": "indicator_three.varo7",
                "max_positions": 2,
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_unexpected_job_failure_does_not_expose_internal_stack_trace() -> None:
    class FailingEngine:
        def scan(self, config, progress_callback=None):
            raise RuntimeError("secret implementation detail")

    with TestClient(create_app(engine=FailingEngine())) as client:
        payload = {
            "selected_signals": ["indicator_three.accumulation_zone"],
            "combine_mode": "any",
            "universe": "all",
            "vipdoc_path": "/tmp",
            "workers": 1,
            "period": "daily",
        }
        response = client.post("/api/v1/formula-screen/jobs", json=payload)
        job_id = response.json()["data"]["job_id"]
        state = wait_for_done(client, job_id)

    assert state["status"] == "failed"
    assert "secret implementation detail" not in state["error"]
    assert "Traceback" not in str(state)
