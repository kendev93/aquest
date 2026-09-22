from __future__ import annotations

import time
from datetime import date
from pathlib import Path
from threading import Event

import numpy as np
import pandas as pd
from fastapi.testclient import TestClient

from selector_app.adapters.online_market_sync import OnlineProviderUnavailable
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.portfolio_backtest.models import PortfolioBacktestConfig
from selector_app.screening.jobs import TaskCancelledError
from selector_app.screening.models import ScanReport
from selector_app.web.app import create_app
from selector_app.web.schemas import FormulaScreenRequest


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
    assert result["record_path"].endswith("market.duckdb#data_meta.maintenance_state")


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
    assert state["result"]["computed"] == 5
    assert warmed.status_code == 200
    # 预计算按年分段直接计算后落库，不走查询路径，因此 lookups 保持为 0。
    assert warmed.json()["data"]["lookups"] == 0
    assert warmed.json()["data"]["p95_ms"] >= warmed.json()["data"]["p50_ms"] >= 0
    assert warmed.json()["data"]["persistent"]["total_entries"] == 5


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
        "provider": "eastmoney",
        "instrument_types": ("stock",),
        "boards": ("main",),
        "prefer_store_targets": False,
    }


def test_market_data_capabilities_expose_the_only_remaining_provider(
    monkeypatch,
) -> None:
    def unavailable_eastmoney():
        raise OnlineProviderUnavailable("测试环境在线客户端不可用")

    monkeypatch.setattr(
        "selector_app.adapters.online_market_sync._default_module",
        unavailable_eastmoney,
    )

    with TestClient(create_app(engine=EmptyEngine())) as client:
        response = client.get("/api/v1/market-data/capabilities")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["default_provider"] == "eastmoney"
    assert data["instrument_count"] == 0
    # TDX 通道已移除，能力清单里只剩 East Money。
    assert list(data["providers"]) == ["eastmoney"]
    assert data["providers"]["eastmoney"]["available"] is False


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
        # 只是等任务起来，不是断言目标；跑满整套测试时 1 秒不一定够。
        assert started.wait(timeout=5)
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


def test_active_jobs_endpoint_finds_a_running_backtest_after_page_refresh() -> None:
    app = create_app(engine=EmptyEngine(), auto_sync_enabled=False)
    runner = app.state.screen_job_runner
    started = Event()
    release = Event()

    def task(_progress):
        started.set()
        # 这个测试要验的就是「刷新页面后还能找到运行中的任务」，所以任务在整个断言期间
        # 必须还活着——超时写 1 秒时，跑满整套测试的机器上 TestClient 起来就要 1 秒以上，
        # 任务先结束、active 列表里就没有它，测试偶发假红。30 秒只是防止 finally 没跑到
        # 时永久挂起，正常路径拿到响应就 set。
        release.wait(timeout=30)
        return {}

    job_id = runner.submit_callable(task, description="历史回测")
    try:
        assert started.wait(timeout=5)
        with TestClient(app) as client:
            response = client.get("/api/v1/jobs/active")
            # 必须在 with 退出前放人：lifespan 关闭会 join 执行器，
            # 任务还在等 release 的话整个测试会卡死在 shutdown 上。
            release.set()

        assert response.status_code == 200
        active = response.json()["data"]
        assert any(
            item["job_id"] == job_id and item["description"] == "历史回测" for item in active
        )
    finally:
        release.set()
        runner.shutdown()


def test_market_data_rejects_duplicate_active_sync_jobs() -> None:
    app = create_app(engine=EmptyEngine(), auto_sync_enabled=False)
    runner = app.state.screen_job_runner
    release = Event()

    def task(_progress):
        # 同上：409 的前提是「已有任务还在跑」，任务自己结束就验不到冲突了。
        release.wait(timeout=30)
        return {}

    existing_job = runner.submit_callable(task, description="行情同步")
    try:
        with TestClient(app) as client:
            response = client.post("/api/v1/market-data/sync-online", json={})
            release.set()  # 同上：别让 lifespan 的 shutdown 等这个任务

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


def test_eastmoney_backfill_forces_stock_full_history_without_local_import() -> None:
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
                "provider": "eastmoney",
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
        "provider": "eastmoney",
        "full_history": True,
        "instrument_types": ("stock",),
        "prefer_store_targets": False,
    }


def test_one_click_sync_does_not_import_local_vipdoc(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    events: list[str] = []

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
                "target_source": "eastmoney_stock_list",
            }

        def sync(self, config, progress_callback=None, *, detail_callback=None):
            del detail_callback
            events.append("sync")
            assert config.prefer_store_targets is False
            if progress_callback:
                progress_callback(1, 1)
            return {
                "source": "online",
                "total_candidates": 1,
                "processed": 1,
                "updated_files": 1,
                "unchanged_files": 0,
                "written_bars": 1,
                "errors": 0,
                "failure_reasons": {},
            }

    with TestClient(
        create_app(
            engine=EmptyEngine(),
            market_sync=FakeMarketSync(),
            market_data_store=store,
        )
    ) as client:
        response = client.post(
            "/api/v1/market-data/sync",
            json={"vipdoc_path": str(tmp_path / "unused-vipdoc"), "universe": "sh"},
        )
        assert response.status_code == 202
        state = wait_for_done(
            client,
            response.json()["data"]["job_id"],
            base_path="/api/v1/market-data/jobs",
        )

    assert state["status"] == "completed"
    assert state["result"]["source"] == "online"
    assert state["result"]["written_bars"] == 1
    assert events == ["sync"]
    assert store.status().bar_count == 0


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
                "fitness_rank_weight": 0.25,
                "fitness_min_trades": 3,
                "fitness_max_drawdown": 0.4,
                "start_date": "2024-01-02",
                "end_date": "2024-01-06",
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


def test_portfolio_backtest_without_dates_uses_recent_five_years(tmp_path: Path) -> None:
    dates = pd.date_range("2020-01-01", periods=2_200, freq="D")
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars(
        "SH",
        "600000",
        "stock",
        _precompute_bars(len(dates)).assign(date=dates),
    )
    received: dict[str, object] = {}

    class CapturingPortfolioService:
        def run(self, config, progress_callback=None):
            received["start_date"] = config.start_date
            received["end_date"] = config.end_date
            if progress_callback:
                progress_callback(1, 1)
            return {
                "universe": config.universe,
                "total_candidates": 1,
                "processed": 1,
                "skipped": 0,
                "errors": 0,
                "bars": 1,
                "start_date": str(config.start_date),
                "end_date": str(config.end_date),
                "max_positions": config.max_positions,
                "ranking_value": config.ranking_value,
                "rank_order": config.rank_order,
                "performance": {},
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
            portfolio_backtest_service=CapturingPortfolioService(),
            market_data_store=store,
            auto_sync_enabled=False,
        )
    ) as client:
        response = client.post(
            "/api/v1/portfolio-backtests",
            json={
                "universe": "all",
                "selected_signals": ["custom.buy"],
                "combine_mode": "any",
                "ranking_value": "custom.rank",
                "formula_text": "BUY:C>0; RANK:C;",
                "stop_loss_pct": 0.05,
            },
        )
        assert response.status_code == 202
        state = wait_for_done(
            client,
            response.json()["data"]["job_id"],
            "/api/v1/portfolio-backtests",
        )

    assert state["status"] == "completed"
    assert received["start_date"] == date(2021, 1, 8)
    assert received["end_date"] == date(2026, 1, 8)


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


def test_portfolio_backtest_accepts_an_explicit_code_list(tmp_path: Path) -> None:
    class CapturingPortfolioService:
        seen: PortfolioBacktestConfig | None = None

        def run(self, config, progress_callback=None):
            if progress_callback:
                progress_callback(1, 1)
            CapturingPortfolioService.seen = config
            return {
                "universe": "codes",
                "total_candidates": 1,
                "processed": 1,
                "skipped": 0,
                "errors": 0,
                "bars": 5,
                "start_date": "2024-01-02",
                "end_date": "2024-01-06",
                "max_positions": 1,
                "ranking_value": config.ranking_value,
                "rank_order": "desc",
                "performance": {},
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
            portfolio_backtest_service=CapturingPortfolioService(),
            market_data_store=DuckDbMarketDataStore(tmp_path / "market.duckdb"),
        )
    ) as client:
        response = client.post(
            "/api/v1/portfolio-backtests",
            json={
                "vipdoc_path": str(tmp_path),
                "universe": "codes",
                "codes": ["600519", "SH:510300"],
                "selected_signals": ["custom.buy"],
                "combine_mode": "any",
                "ranking_value": "custom.rank",
                "max_positions": 1,
                "position_mode": "fixed",
                "fixed_size": 500,
                "formula_text": "BUY:C>0; RANK:C;",
                "stop_loss_pct": 0.05,
                "start_date": "2024-01-02",
                "end_date": "2024-01-06",
            },
        )
        assert response.status_code == 202

    config = CapturingPortfolioService.seen
    assert config is not None
    assert config.universe == "codes"
    assert config.codes == ("600519", "SH:510300")
    assert config.position_mode == "fixed"
    assert config.fixed_size == 500


def test_portfolio_backtest_rejects_codes_without_the_matching_universe(tmp_path: Path) -> None:
    with TestClient(create_app(engine=EmptyEngine())) as client:
        response = client.post(
            "/api/v1/portfolio-backtests",
            json={
                "vipdoc_path": str(tmp_path),
                "universe": "all",
                "codes": ["600519"],
                "selected_signals": ["indicator_three.begin_zone"],
                "combine_mode": "any",
                "ranking_value": "indicator_three.varo7",
                "max_positions": 2,
                "stop_loss_pct": 0.05,
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_portfolio_backtest_rejects_fixed_mode_without_a_size(tmp_path: Path) -> None:
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
                "position_mode": "fixed",
                "stop_loss_pct": 0.05,
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
