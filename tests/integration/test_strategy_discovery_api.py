from __future__ import annotations

import time
from threading import Event

import pandas as pd
from fastapi.testclient import TestClient

from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.screening.jobs import ScreenJobRunner
from selector_app.strategy_discovery.job_backend import (
    FileStrategyJobBackend,
    InProcessStrategyJobBackend,
)
from selector_app.web.app import create_app


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
                "selector_app.market_data.store", fromlist=["DuckDbMarketDataStore"]
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


def test_create_app_uses_file_backend_for_production_service(tmp_path) -> None:
    app = create_app(
        market_data_store=DuckDbMarketDataStore(tmp_path / "market.duckdb"),
        auto_sync_enabled=False,
    )

    assert isinstance(app.state.strategy_job_backend, FileStrategyJobBackend)


def test_create_app_keeps_in_process_backend_for_injected_fake_service(tmp_path) -> None:
    app = create_app(
        strategy_discovery_service=FakeDiscoveryService(),
        market_data_store=DuckDbMarketDataStore(tmp_path / "market.duckdb"),
        auto_sync_enabled=False,
    )

    assert isinstance(app.state.strategy_job_backend, InProcessStrategyJobBackend)


def test_strategy_refinement_api_creates_independent_parent_linked_history(tmp_path) -> None:
    from selector_app.market_data.store import DuckDbMarketDataStore

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
    from selector_app.market_data.store import DuckDbMarketDataStore

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
    from selector_app.market_data.store import DuckDbMarketDataStore

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    with TestClient(create_app(market_data_store=store)) as client:
        response = client.post(
            "/api/v1/strategy-discovery/experiment-1/replays",
            json={"leaderboard": "unknown"},
        )

    assert response.status_code == 422


def test_strategy_discovery_api_uses_all_completed_local_dates_when_omitted(tmp_path) -> None:
    store = __import__(
        "selector_app.market_data.store", fromlist=["DuckDbMarketDataStore"]
    ).DuckDbMarketDataStore(tmp_path / "market.duckdb")
    dates = pd.date_range("2024-01-01", periods=3, freq="D")
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
    assert service.requests[0].base_config["start_date"] == "2024-01-01"
    assert service.requests[0].base_config["end_date"] == "2024-01-03"
    assert service.requests[0].required_buy_conditions[0].right_value == "technical.ma20"


def test_strategy_discovery_api_reports_actionable_error_for_empty_store(tmp_path) -> None:
    store = __import__(
        "selector_app.market_data.store", fromlist=["DuckDbMarketDataStore"]
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
    assert "请先在“本地行情”页面导入日线数据" in response.json()["error"]["message"]


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
                    "selector_app.market_data.store",
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
