from __future__ import annotations

from fastapi.testclient import TestClient

from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.web.app import create_app

TOKEN = "unit-test-worker-token"


def _app(tmp_path, **kwargs):
    return create_app(
        market_data_store=DuckDbMarketDataStore(tmp_path / "market.duckdb"),
        strategy_worker_token=TOKEN,
        auto_sync_enabled=False,
        **kwargs,
    )


def _headers(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_worker_api_authenticates_and_claims_jobs_atomically(tmp_path) -> None:
    app = _app(tmp_path)
    job_id = app.state.strategy_job_backend.submit(
        "discovery",
        {"request": {"max_trials": 3}},
        description="策略实验室",
        worker_count=2,
    )

    with TestClient(app) as client:
        assert (
            client.post(
                "/internal/strategy-worker/jobs/claim", json={"worker_id": "worker-a"}
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/internal/strategy-worker/jobs/claim",
                headers=_headers("wrong-token"),
                json={"worker_id": "worker-a"},
            ).status_code
            == 401
        )

        response = client.post(
            "/internal/strategy-worker/jobs/claim",
            headers=_headers(),
            json={"worker_id": "worker-a"},
        )
        empty = client.post(
            "/internal/strategy-worker/jobs/claim",
            headers=_headers(),
            json={"worker_id": "worker-b"},
        )

    assert response.status_code == 200
    assert response.json()["job"]["job_id"] == job_id
    assert response.json()["job"]["request"]["payload"] == {"request": {"max_trials": 3}}
    assert response.json()["job"]["state"]["worker_count"] == 2
    assert empty.status_code == 200
    assert empty.json() == {"job": None}


def test_worker_api_progress_heartbeat_cancel_and_completion_persist_history(tmp_path) -> None:
    app = _app(tmp_path)
    job_id = app.state.strategy_job_backend.submit(
        "discovery",
        {"request": {"base_config": {}, "splits": {}, "max_trials": 1}},
        description="策略实验室",
    )

    with TestClient(app) as client:
        claimed = client.post(
            "/internal/strategy-worker/jobs/claim",
            headers=_headers(),
            json={"worker_id": "worker-a"},
        )
        assert claimed.json()["job"]["job_id"] == job_id
        wrong_worker = client.post(
            f"/internal/strategy-worker/jobs/{job_id}/progress",
            headers=_headers(),
            json={"worker_id": "worker-b", "progress": 0.5},
        )
        progress = client.post(
            f"/internal/strategy-worker/jobs/{job_id}/progress",
            headers=_headers(),
            json={
                "worker_id": "worker-a",
                "phase": "searching",
                "progress": 0.5,
                "total_candidates": 2,
                "total_scanned": 1,
                "metadata": {"stage": "coarse"},
            },
        )
        heartbeat = client.post(
            f"/internal/strategy-worker/jobs/{job_id}/heartbeat",
            headers=_headers(),
            json={"worker_id": "worker-a"},
        )
        completed = client.post(
            f"/internal/strategy-worker/jobs/{job_id}/complete",
            headers=_headers(),
            json={
                "worker_id": "worker-a",
                "result": {
                    "request": {"base_config": {}, "splits": {}, "max_trials": 1},
                    "results_by_sharpe": [],
                    "results_by_return": [],
                    "search_metadata": {"evaluated_trials": 1},
                    "started_at": "2026-01-01T00:00:00+00:00",
                    "finished_at": "2026-01-01T00:00:01+00:00",
                    "duration_seconds": 1.0,
                },
            },
        )
        state = client.get(f"/api/v1/strategy-discovery/{job_id}")
        history = client.get("/api/v1/strategy-discovery/history")

    assert wrong_worker.status_code == 409
    assert progress.status_code == 200
    assert progress.json()["data"]["state"]["metadata"]["stage"] == "coarse"
    assert heartbeat.status_code == 200
    assert heartbeat.json()["data"]["cancel_requested"] is False
    assert completed.status_code == 200
    assert state.json()["data"]["status"] == "completed"
    assert history.status_code == 200
    assert history.json()["data"][0]["history_id"] == job_id


def test_worker_api_cancellation_is_returned_by_heartbeat(tmp_path) -> None:
    app = _app(tmp_path)
    job_id = app.state.strategy_job_backend.submit(
        "discovery", {"request": {}}, description="策略实验室"
    )

    with TestClient(app) as client:
        client.post(
            "/internal/strategy-worker/jobs/claim",
            headers=_headers(),
            json={"worker_id": "worker-a"},
        )
        client.post(f"/api/v1/strategy-discovery/{job_id}/cancel")
        heartbeat = client.post(
            f"/internal/strategy-worker/jobs/{job_id}/heartbeat",
            headers=_headers(),
            json={"worker_id": "worker-a"},
        )
        cancelled = client.post(
            f"/internal/strategy-worker/jobs/{job_id}/cancelled",
            headers=_headers(),
            json={"worker_id": "worker-a"},
        )

    assert heartbeat.status_code == 200
    assert heartbeat.json()["data"]["cancel_requested"] is True
    assert cancelled.json()["data"]["state"]["status"] == "cancelled"
    assert app.state.strategy_job_backend.get(job_id)["status"] == "cancelled"


def test_worker_api_still_requires_a_worker_token(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/internal/strategy-worker/jobs/claim",
            headers=_headers(),
            json={"worker_id": "worker-a"},
        )

    assert response.status_code == 200
    assert response.json() == {"job": None}


def test_worker_api_is_unavailable_when_no_token_is_configured(tmp_path) -> None:
    app = create_app(
        market_data_store=DuckDbMarketDataStore(tmp_path / "market.duckdb"),
        auto_sync_enabled=False,
    )
    with TestClient(app) as client:
        response = client.post(
            "/internal/strategy-worker/jobs/claim",
            json={"worker_id": "worker-a"},
        )

    assert response.status_code == 503
