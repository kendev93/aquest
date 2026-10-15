from __future__ import annotations

import time
from threading import Event

import pandas as pd
from fastapi.testclient import TestClient

from aquest.market_data.store import DuckDbMarketDataStore
from aquest.screening.jobs import ScreenJobRunner
from aquest.strategy_discovery.job_backend import (
    DuckDbStrategyJobBackend,
    FileStrategyJobBackend,
    InProcessStrategyJobBackend,
)
from aquest.strategy_discovery.job_store import StrategyJobStore
from aquest.web.app import create_app


class FakeDiscoveryService:
    def capabilities(self):
        return {"algorithm": "seeded_random_fallback", "indicator_count": 1}

    def run(self, request, progress_callback=None, abort_check=None):
        if progress_callback is not None:
            progress_callback(1, 1, "searching", {"best_sharpe": 1.2, "best_return": 0.3})
        return {
            "request": request,
            "results_by_sharpe": [
                {"candidate_hash": "s1", "metrics": {"validation": {"sharpe": 1.2}}}
            ],
            "results_by_return": [
                {"candidate_hash": "r1", "metrics": {"validation": {"annual_return": 0.3}}}
            ],
            "search_metadata": {
                "requested_trials": 3,
                "evaluated_trials": 1,
                "unique_evaluated_trials": 1,
                "duplicate_trials": 2,
            },
            "algorithm_metadata": {"algorithm": "seeded_random_fallback"},
            "data_fingerprint": "abc123",
            "duration_seconds": 0.01,
        }

    def replay(self, experiment, leaderboard, progress_callback=None, abort_check=None):
        if progress_callback is not None:
            progress_callback(1, 1, "replaying", {"overall_progress": 1.0})
        return {
            "experiment_id": experiment["history_id"],
            "leaderboard": leaderboard,
            "data_start": "2024-01-01",
            "data_end": "2024-12-31",
            "data_fingerprint": "replay-fingerprint",
            "data_changed": False,
            "candidate_count": 1,
            "candidates": [{"candidate_hash": "candidate-1"}],
        }


class CapturingDiscoveryService(FakeDiscoveryService):
    def __init__(self) -> None:
        self.requests = []

    def run(self, request, progress_callback=None, abort_check=None):
        self.requests.append(request)
        return super().run(request, progress_callback, abort_check)


class RefinableDiscoveryService(FakeDiscoveryService):
    def refine(self, experiment, parent_candidate_hash, **kwargs):
        if kwargs.get("progress_callback") is not None:
            kwargs["progress_callback"](1, 1, "searching", {"overall_progress": 1.0})
        return {
            "request": {
                "mode": "refinement",
                "refinement": {
                    "parent_history_id": experiment["history_id"],
                    "parent_candidate_hash": parent_candidate_hash,
                    "refinement_radius": kwargs.get("radius"),
                },
            },
            "results_by_sharpe": [{"candidate_hash": "refined-1"}],
            "results_by_return": [{"candidate_hash": "refined-1"}],
            "search_metadata": {"evaluated_trials": 1},
            "data_fingerprint": "abc123",
        }


def _wait(client: TestClient, job_id: str) -> dict:
    for _ in range(50):
        payload = client.get(f"/api/v1/strategy-discovery/{job_id}").json()["data"]
        if payload["status"] in {"completed", "failed", "cancelled"}:
            return payload
        time.sleep(0.01)
    raise AssertionError("strategy discovery job did not finish")


def test_strategy_discovery_api_starts_reports_and_reads_history(tmp_path) -> None:
    with TestClient(
        create_app(
            strategy_discovery_service=FakeDiscoveryService(),
            market_data_store=__import__(
                "aquest.market_data.store", fromlist=["DuckDbMarketDataStore"]
            ).DuckDbMarketDataStore(tmp_path / "market.duckdb"),
        )
    ) as client:
        capabilities = client.get("/api/v1/strategy-discovery/capabilities")
        response = client.post(
            "/api/v1/strategy-discovery",
            json={
                "universe": "all",
                "max_positions": 3,
                "start_date": "2024-01-01",
                "end_date": "2024-12-31",
            },
        )
        assert response.status_code == 202
        state = _wait(client, response.json()["data"]["job_id"])
        results = client.get(
            f"/api/v1/strategy-discovery/{response.json()['data']['job_id']}/results"
        )
        history = client.get("/api/v1/strategy-discovery/history")

    assert capabilities.status_code == 200
    assert capabilities.json()["data"]["indicator_count"] == 1
    assert state["status"] == "completed"
    assert state["metadata"]["overall_progress"] == 1.0
    assert state["metadata"]["requested_trials"] == 3
    assert state["metadata"]["duplicate_trials"] == 2
    assert results.status_code == 200
    assert history.status_code == 200
    assert history.json()["data"][0]["history_id"]
    assert history.json()["data"][0]["duration_seconds"] == 0.01
    assert history.json()["data"][0]["started_at"]
    assert history.json()["data"][0]["finished_at"]


def test_portfolio_risk_optimization_uses_async_worker_and_fixed_three_way_split(tmp_path) -> None:
    service = CapturingDiscoveryService()
    with TestClient(
        create_app(
            strategy_discovery_service=service,
            market_data_store=DuckDbMarketDataStore(tmp_path / "market.duckdb"),
        )
    ) as client:
        response = client.post(
            "/api/v1/strategy-discovery/risk-optimizations",
            json={
                "universe": "all",
                "start_date": "2024-01-01",
                "end_date": "2024-06-30",
                "buy_rule": {
                    "conditions": [
                        {"left_value": "technical.ma5", "operator": "gt", "right_constant": 10}
                    ]
                },
                "sell_rule": {
                    "conditions": [
                        {"left_value": "technical.rsi14", "operator": "gte", "right_constant": 70}
                    ]
                },
                "ranking_value": "technical.ma5",
                "rank_order": "asc",
                "max_positions": 3,
                "max_trials": 1000,
            },
        )
        assert response.status_code == 202
        job_id = response.json()["data"]["job_id"]
        state = _wait(client, job_id)
        results = client.get(f"/api/v1/strategy-discovery/{job_id}/results")

    assert state["status"] == "completed"
    assert state["description"] == "组合风险寻优"
    assert results.status_code == 200
    assert results.json()["data"]["mode"] == "risk_optimization"
    assert results.json()["data"]["risk_optimization_id"] == job_id
    assert len(service.requests) == 1
    request = service.requests[0]
    assert request.candidate_mode == "risk_only"
    assert request.base_config["ranking_value"] == "technical.ma5"
    assert request.base_config["rank_order"] == "asc"
    assert request.splits["search"]["end_date"] < request.splits["validation"]["start_date"]
    assert request.splits["validation"]["end_date"] < request.splits["holdout"]["start_date"]


def test_create_app_uses_duckdb_backend_for_production_service(tmp_path) -> None:
    app = create_app(
        market_data_store=DuckDbMarketDataStore(tmp_path / "market.duckdb"),
        auto_sync_enabled=False,
    )

    assert isinstance(app.state.strategy_job_backend, DuckDbStrategyJobBackend)


def test_app_imports_legacy_job_files_into_duckdb_on_startup(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("AQUEST_DISCOVERY_JOB_DIR", raising=False)
    legacy = StrategyJobStore(tmp_path / ".strategy-jobs")
    legacy.create(
        "legacy-completed",
        operation="discovery",
        payload={"request": {"max_trials": 2}},
        description="策略实验室",
    )
    legacy.write_result("legacy-completed", {"history_id": "legacy-completed"})
    app = create_app(
        market_data_store=DuckDbMarketDataStore(tmp_path / "market.duckdb"),
        auto_sync_enabled=False,
    )

    with TestClient(app):
        state = app.state.strategy_job_store.read_state("legacy-completed")
        result = app.state.strategy_job_store.read_result("legacy-completed")

    assert state is not None
    assert state["status"] == "completed"
    assert result == {"history_id": "legacy-completed"}
    assert (tmp_path / ".strategy-jobs" / "legacy-completed" / "state.json").is_file()


def test_active_jobs_endpoint_reads_a_queued_strategy_job_from_shared_storage(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    job_store = StrategyJobStore(tmp_path / "jobs")
    job_store.create(
        "discovery-active",
        operation="discovery",
        payload={"request": {}},
        description="策略实验室",
    )
    app = create_app(
        market_data_store=store,
        strategy_job_backend=FileStrategyJobBackend(store, job_store),
        auto_sync_enabled=False,
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/jobs/active")

    assert response.status_code == 200
    assert response.json()["data"] == [
        {
            "job_id": "discovery-active",
            "status": "queued",
            "progress": 0.0,
            "total_candidates": 0,
            "total_scanned": 0,
            "total_signals": 0,
            "errors": 0,
            "skipped": 0,
            "error": None,
            "error_code": None,
            "result": None,
            "description": "策略实验室",
            "worker_count": None,
            "created_at": response.json()["data"][0]["created_at"],
            "started_at": None,
            "finished_at": None,
            "phase": "queued",
            "metadata": {},
            "cancel_requested": False,
            "heartbeat_at": response.json()["data"][0]["heartbeat_at"],
            "operation": "discovery",
        }
    ]


def test_create_app_keeps_in_process_backend_for_injected_fake_service(tmp_path) -> None:
    app = create_app(
        strategy_discovery_service=FakeDiscoveryService(),
        market_data_store=DuckDbMarketDataStore(tmp_path / "market.duckdb"),
        auto_sync_enabled=False,
    )

    assert isinstance(app.state.strategy_job_backend, InProcessStrategyJobBackend)


def test_strategy_discovery_rejects_over_budget_before_creating_a_job(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AQUEST_DISCOVERY_MEMORY_BUDGET_MB", "1")
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars(
        "SH",
        "600000",
        "stock",
        pd.DataFrame(
            {
                "date": pd.date_range("2024-01-01", periods=5, freq="D"),
                "open": [10, 11, 12, 13, 14],
                "high": [10.2, 11.2, 12.2, 13.2, 14.2],
                "low": [9.8, 10.8, 11.8, 12.8, 13.8],
                "close": [10, 11, 12, 13, 14],
                "volume": [1000] * 5,
                "amount": [10000] * 5,
                "bar_status": ["completed"] * 5,
            }
        ),
    )
    app = create_app(market_data_store=store, auto_sync_enabled=False)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/strategy-discovery",
            json={
                "start_date": "2024-01-01",
                "end_date": "2024-01-05",
                "max_positions": 2,
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "resource_budget_exceeded"
    assert app.state.strategy_job_store.list_snapshots() == []


def test_strategy_refinement_api_creates_independent_parent_linked_history(tmp_path) -> None:
    from aquest.market_data.store import DuckDbMarketDataStore

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.save_strategy_discovery_history(
        "parent-1",
        {
            "request": {
                "base_config": {"universe": "all", "max_positions": 3},
                "splits": {},
                "indicator_ids": ["technical.ma5"],
                "signal_ids": [],
                "max_conditions_per_side": 3,
                "max_trials": 4,
                "validation_candidates": 1,
                "holdout_candidates": 1,
                "min_trades": 0,
                "max_drawdown": None,
                "max_complexity": 12,
                "multifidelity_enabled": False,
                "seed": 1,
            },
            "results_by_sharpe": [{"candidate_hash": "parent-candidate"}],
            "results_by_return": [],
            "search_metadata": {"evaluated_trials": 1},
            "data_fingerprint": "abc123",
        },
    )

    with TestClient(
        create_app(
            strategy_discovery_service=RefinableDiscoveryService(),
            market_data_store=store,
        )
    ) as client:
        response = client.post(
            "/api/v1/strategy-discovery/refinements",
            json={
                "parent_history_id": "parent-1",
                "parent_candidate_hash": "parent-candidate",
                "refinement_radius": "medium",
                "max_trials": 4,
            },
        )
        assert response.status_code == 202
        job_id = response.json()["data"]["job_id"]
        state = _wait(client, job_id)
        history = client.get("/api/v1/strategy-discovery/history")

    assert state["status"] == "completed"
    assert history.status_code == 200
    refinement_history = next(
        item for item in history.json()["data"] if item["history_id"] == job_id
    )
    assert refinement_history["mode"] == "refinement"
    assert refinement_history["parent_history_id"] == "parent-1"
    assert refinement_history["parent_candidate_hash"] == "parent-candidate"


def test_strategy_replay_api_runs_persists_and_reads_the_selected_leaderboard(tmp_path) -> None:
    from aquest.market_data.store import DuckDbMarketDataStore

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.save_strategy_discovery_history(
        "experiment-1",
        {
            "request": {"base_config": {"start_date": "2024-01-01", "end_date": "2024-12-31"}},
            "search_metadata": {},
            "validation_metadata": {},
            "algorithm_metadata": {},
            "data_fingerprint": "source-fingerprint",
            "results_by_sharpe": [],
            "results_by_return": [],
            "duration_seconds": 1.0,
        },
    )
    service = FakeDiscoveryService()
    with TestClient(
        create_app(
            strategy_discovery_service=service,
            market_data_store=store,
        )
    ) as client:
        created = client.post(
            "/api/v1/strategy-discovery/experiment-1/replays",
            json={"leaderboard": "sharpe"},
        )
        assert created.status_code == 202
        replay_job_id = created.json()["data"]["job_id"]
        state = None
        for _ in range(50):
            state_response = client.get(f"/api/v1/strategy-discovery/replay-jobs/{replay_job_id}")
            assert state_response.status_code == 200
            state = state_response.json()["data"]
            if state["status"] in {"completed", "failed"}:
                break
            time.sleep(0.01)
        result = client.get(f"/api/v1/strategy-discovery/replay-jobs/{replay_job_id}/results")
        saved = client.get(
            "/api/v1/strategy-discovery/history/experiment-1/replays?leaderboard=sharpe"
        )

    assert state is not None
    assert state["status"] == "completed"
    assert state["total_candidates"] == 1
    assert state["total_scanned"] == 1
    assert result.status_code == 200
    assert result.json()["data"]["candidate_count"] == 1
    assert saved.status_code == 200
    assert saved.json()["data"]["replay_id"] == replay_job_id


def test_strategy_replay_api_rejects_unknown_leaderboard(tmp_path) -> None:
    from aquest.market_data.store import DuckDbMarketDataStore

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    with TestClient(create_app(market_data_store=store)) as client:
        response = client.post(
            "/api/v1/strategy-discovery/experiment-1/replays",
            json={"leaderboard": "unknown"},
        )

    assert response.status_code == 422


def test_strategy_discovery_api_defaults_to_the_recent_five_years(tmp_path) -> None:
    store = __import__(
        "aquest.market_data.store", fromlist=["DuckDbMarketDataStore"]
    ).DuckDbMarketDataStore(tmp_path / "market.duckdb")
    dates = pd.to_datetime(["2018-01-01", "2021-09-11", "2026-09-11"])
    close = [10.0, 10.5, 11.0]
    store.replace_local_bars(
        "SH",
        "600000",
        "stock",
        pd.DataFrame(
            {
                "date": dates,
                "open": close,
                "high": [value + 0.2 for value in close],
                "low": [value - 0.2 for value in close],
                "close": close,
                "volume": [1000.0] * 3,
                "amount": [value * 1000 for value in close],
                "bar_status": ["completed"] * 3,
            }
        ),
    )
    service = CapturingDiscoveryService()

    with TestClient(
        create_app(strategy_discovery_service=service, market_data_store=store)
    ) as client:
        response = client.post(
            "/api/v1/strategy-discovery",
            json={
                "max_positions": 2,
                "required_buy_conditions": [
                    {
                        "left_value": "technical.ma5",
                        "operator": "gt",
                        "right_value": "technical.ma20",
                    }
                ],
            },
        )
        state = _wait(client, response.json()["data"]["job_id"])

    assert response.status_code == 202
    assert state["status"] == "completed"
    assert len(service.requests) == 1
    assert service.requests[0].base_config["start_date"] == "2021-09-11"
    assert service.requests[0].base_config["end_date"] == "2026-09-11"
    assert service.requests[0].required_buy_conditions[0].right_value == "technical.ma20"


def test_strategy_discovery_api_allows_explicit_full_history_mode(tmp_path) -> None:
    store = __import__(
        "aquest.market_data.store", fromlist=["DuckDbMarketDataStore"]
    ).DuckDbMarketDataStore(tmp_path / "market.duckdb")
    dates = pd.to_datetime(["2018-01-01", "2021-09-11", "2026-09-11"])
    close = [10.0, 10.5, 11.0]
    store.replace_local_bars(
        "SH",
        "600000",
        "stock",
        pd.DataFrame(
            {
                "date": dates,
                "open": close,
                "high": [value + 0.2 for value in close],
                "low": [value - 0.2 for value in close],
                "close": close,
                "volume": [1000.0] * 3,
                "amount": [value * 1000 for value in close],
                "bar_status": ["completed"] * 3,
            }
        ),
    )
    service = CapturingDiscoveryService()

    with TestClient(
        create_app(strategy_discovery_service=service, market_data_store=store)
    ) as client:
        response = client.post(
            "/api/v1/strategy-discovery",
            json={"date_mode": "full", "max_positions": 2},
        )
        state = _wait(client, response.json()["data"]["job_id"])

    assert response.status_code == 202
    assert state["status"] == "completed"
    assert service.requests[0].base_config["start_date"] == "2018-01-01"
    assert service.requests[0].base_config["end_date"] == "2026-09-11"


def test_strategy_discovery_api_reports_actionable_error_for_empty_store(tmp_path) -> None:
    store = __import__(
        "aquest.market_data.store", fromlist=["DuckDbMarketDataStore"]
    ).DuckDbMarketDataStore(tmp_path / "empty.duckdb")

    with TestClient(
        create_app(
            strategy_discovery_service=FakeDiscoveryService(),
            market_data_store=store,
        )
    ) as client:
        response = client.post(
            "/api/v1/strategy-discovery",
            json={"max_positions": 2},
        )

    assert response.status_code == 422
    assert "请先同步行情" in response.json()["error"]["message"]


class BlockingDiscoveryService:
    started = Event()

    def capabilities(self):
        return {"enabled": True}

    def run(self, request, progress_callback=None, abort_check=None):
        self.started.set()
        while abort_check is None or not abort_check():
            time.sleep(0.005)
        raise RuntimeError("策略实验已取消")


def test_strategy_discovery_api_can_cancel_a_running_job(tmp_path) -> None:
    service = BlockingDiscoveryService()
    runner = ScreenJobRunner(max_workers=1)
    try:
        with TestClient(
            create_app(
                runner=runner,
                strategy_discovery_service=service,
                market_data_store=__import__(
                    "aquest.market_data.store",
                    fromlist=["DuckDbMarketDataStore"],
                ).DuckDbMarketDataStore(tmp_path / "market.duckdb"),
            )
        ) as client:
            response = client.post(
                "/api/v1/strategy-discovery",
                json={
                    "start_date": "2024-01-01",
                    "end_date": "2024-12-31",
                    "max_positions": 2,
                },
            )
            job_id = response.json()["data"]["job_id"]
            assert service.started.wait(timeout=1)
            cancelled = client.post(f"/api/v1/strategy-discovery/{job_id}/cancel")
            state = _wait(client, job_id)
    finally:
        runner.shutdown()

    assert cancelled.status_code == 200
    assert cancelled.json()["data"]["cancel_requested"] is True
    assert state["status"] == "cancelled"


def test_portfolio_risk_optimization_api_can_cancel_a_running_job(tmp_path) -> None:
    service = BlockingDiscoveryService()
    service.started.clear()
    runner = ScreenJobRunner(max_workers=1)
    try:
        with TestClient(
            create_app(
                runner=runner,
                strategy_discovery_service=service,
                market_data_store=DuckDbMarketDataStore(tmp_path / "risk-market.duckdb"),
            )
        ) as client:
            response = client.post(
                "/api/v1/strategy-discovery/risk-optimizations",
                json={
                    "start_date": "2024-01-01",
                    "end_date": "2024-12-31",
                    "buy_rule": {
                        "conditions": [
                            {
                                "left_value": "technical.ma5",
                                "operator": "gt",
                                "right_constant": 10,
                            }
                        ]
                    },
                    "sell_rule": {
                        "conditions": [
                            {
                                "left_value": "technical.rsi14",
                                "operator": "gte",
                                "right_constant": 70,
                            }
                        ]
                    },
                    "ranking_value": "technical.ma5",
                    "max_trials": 1000,
                },
            )
            job_id = response.json()["data"]["job_id"]
            assert service.started.wait(timeout=1)
            cancelled = client.post(f"/api/v1/strategy-discovery/{job_id}/cancel")
            state = _wait(client, job_id)
    finally:
        runner.shutdown()

    assert response.status_code == 202
    assert cancelled.status_code == 200
    assert cancelled.json()["data"]["cancel_requested"] is True
    assert state["description"] == "组合风险寻优"
    assert state["status"] == "cancelled"
