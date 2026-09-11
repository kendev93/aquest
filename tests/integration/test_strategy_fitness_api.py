from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.screening.models import ScanReport
from selector_app.web.app import create_app


class EmptyEngine:
    def scan(self, config, progress_callback=None):
        return ScanReport(0, 0, 0, 0, 0, (), {}, {})


def wait_for_done(client: TestClient, job_id: str) -> dict:
    for _ in range(50):
        response = client.get(f"/api/v1/strategy-fitness/{job_id}")
        assert response.status_code == 200
        payload = response.json()["data"]
        if payload["status"] in {"completed", "failed"}:
            return payload
        time.sleep(0.01)
    raise AssertionError("strategy fitness job did not finish")


def test_strategy_fitness_job_can_be_created_and_results_are_available(tmp_path: Path) -> None:
    class FakeFitnessService:
        def run(self, config, progress_callback=None):
            if progress_callback:
                progress_callback(1, 1)
            return {
                "total_candidates": 1,
                "processed": 1,
                "skipped": 0,
                "errors": 0,
                "start_date": "2024-01-01",
                "end_date": "2024-01-06",
                "train_end_date": "2024-01-03",
                "validation_end_date": "2024-01-04",
                "ranking_value": config.strategy.ranking_value,
                "results": [],
                "failure_reasons": {},
                "diagnostic": None,
            }

    (tmp_path / "vipdoc").mkdir()
    with TestClient(
        create_app(
            engine=EmptyEngine(),
            strategy_fitness_service=FakeFitnessService(),
            market_data_store=DuckDbMarketDataStore(tmp_path / "market.duckdb"),
        )
    ) as client:
        response = client.post(
            "/api/v1/strategy-fitness",
            json={
                "vipdoc_path": str(tmp_path / "vipdoc"),
                "selected_signals": ["custom.buy"],
                "combine_mode": "any",
                "ranking_value": "custom.rank",
                "formula_text": "BUY:C>0; RANK:C;",
                "stop_loss_pct": 0.05,
                "start_date": "2024-01-01",
                "end_date": "2024-01-06",
                "min_trades": 1,
            },
        )
        assert response.status_code == 202
        job_id = response.json()["data"]["job_id"]
        state = wait_for_done(client, job_id)
        result_response = client.get(f"/api/v1/strategy-fitness/{job_id}/results")
        history_response = client.get("/api/v1/strategy-fitness/history")
        history_detail = client.get(f"/api/v1/strategy-fitness/history/{job_id}")

    assert state["status"] == "completed"
    assert state["result"] is None
    assert result_response.status_code == 200
    assert result_response.json()["data"]["train_end_date"] == "2024-01-03"
    assert history_response.status_code == 200
    assert history_response.json()["data"][0]["history_id"] == job_id
    assert history_response.json()["data"][0]["backtest_type"] == "strategy_fitness"
    assert history_response.json()["data"][0]["summary"]["start_date"] == "2024-01-01"
    assert history_response.json()["data"][0]["summary"]["end_date"] == "2024-01-06"
    assert history_detail.status_code == 200
    assert history_detail.json()["data"]["result"]["train_end_date"] == "2024-01-03"
