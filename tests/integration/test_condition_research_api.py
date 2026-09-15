from __future__ import annotations

import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi.testclient import TestClient

from selector_app.condition_research.resources import ResearchResourceEstimate
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.web.app import create_app


class FakeConditionResearchService:
    def __init__(self, *, within_budget: bool = True) -> None:
        self.config = None
        self._within_budget = within_budget

    def estimate_resources(self, config):
        peak = 4 * 1024**3 if self._within_budget else 99 * 1024**3
        return ResearchResourceEstimate(
            stock_count=1,
            candidates_per_stock=2,
            bars_per_stock=120,
            requested_workers=4,
            selected_workers=4 if self._within_budget else 1,
            worker_bytes=224 * 1024**2,
            parent_bytes=160 * 1024**2,
            estimated_peak_bytes=peak,
            memory_budget_bytes=4 * 1024**3,
            within_budget=self._within_budget,
            reason="fixture",
        )

    def run(self, config, progress_callback=None, cancel_check=None):
        self.config = config
        if progress_callback is not None:
            progress_callback(1, 1)
        return {
            "processed": 1,
            "total_candidates": 1,
            "candidates": [],
            "marker": "condition-research-complete",
        }


def bars() -> pd.DataFrame:
    close = 10.0 + np.linspace(0.0, 5.0, 120) + np.sin(np.arange(120) / 3.0)
    return pd.DataFrame(
        {
            "date": pd.date_range("2020-01-01", periods=120, freq="D"),
            "open": close - 0.1,
            "high": close + 0.4,
            "low": close - 0.4,
            "close": close,
            "volume": np.full(120, 50_000.0),
            "amount": close * 50_000.0,
            "bar_status": "completed",
        }
    )


def payload(tmp_path: Path) -> dict[str, object]:
    return {
        "universe": "all",
        "vipdoc_path": str(tmp_path),
        "buy_rule": {
            "conditions": [
                {
                    "left_value": "indicator_three.var1",
                    "operator": "gt",
                    "right_constant": 0,
                }
            ]
        },
        "sell_rule": {
            "conditions": [
                {
                    "left_value": "indicator_three.var1",
                    "operator": "lte",
                    "right_constant": 0,
                }
            ]
        },
        "date_mode": "custom",
        "start_date": "2020-01-01",
        "end_date": "2020-04-29",
        "min_trades": 1,
        "min_instruments": 1,
    }


def wait_for_job(client: TestClient, job_id: str) -> dict[str, object]:
    for _ in range(100):
        response = client.get(f"/api/v1/condition-research/{job_id}")
        assert response.status_code == 200
        state = response.json()["data"]
        if state["status"] in {"completed", "failed", "cancelled"}:
            return state
        time.sleep(0.01)
    raise AssertionError("condition research job did not finish")


def test_condition_research_job_returns_results_and_persists_history(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", bars())
    service = FakeConditionResearchService()
    app = create_app(
        market_data_store=store,
        condition_research_service=service,
        auto_sync_enabled=False,
    )

    with TestClient(app) as client:
        created = client.post("/api/v1/condition-research", json=payload(tmp_path))
        assert created.status_code == 202
        job_id = created.json()["data"]["job_id"]
        state = wait_for_job(client, job_id)
        result = client.get(f"/api/v1/condition-research/{job_id}/results")
        history = client.get("/api/v1/condition-research/history")
        history_detail = client.get(f"/api/v1/condition-research/history/{job_id}")

    assert state["status"] == "completed"
    assert result.status_code == 200
    assert result.json()["data"]["marker"] == "condition-research-complete"
    assert history.status_code == 200
    assert history.json()["data"][0]["history_id"] == job_id
    assert history_detail.status_code == 200
    assert history_detail.json()["data"]["backtest_type"] == "condition_research"
    assert history_detail.json()["data"]["result"]["marker"] == "condition-research-complete"
    assert service.config.start_date == date(2020, 1, 1)
    assert service.config.end_date == date(2020, 4, 29)
    assert not hasattr(service.config, "max_positions")


def test_condition_research_api_rejects_missing_sell_rule(tmp_path: Path) -> None:
    invalid = payload(tmp_path)
    invalid.pop("sell_rule")
    app = create_app(auto_sync_enabled=False)

    with TestClient(app) as client:
        response = client.post("/api/v1/condition-research", json=invalid)

    assert response.status_code == 422


def test_condition_research_api_rejects_a_run_over_the_memory_budget(tmp_path: Path) -> None:
    """预算不足时必须在创建任务前返回 422，并带上可执行的估算信息。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", bars())
    app = create_app(
        market_data_store=store,
        condition_research_service=FakeConditionResearchService(within_budget=False),
        auto_sync_enabled=False,
    )

    with TestClient(app) as client:
        response = client.post("/api/v1/condition-research", json=payload(tmp_path))

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "resource_budget_exceeded"
    assert "内存" in error["message"]
    assert error["estimate"]["estimated_peak_bytes"] == 99 * 1024**3
    assert error["estimate"]["memory_budget_bytes"] == 4 * 1024**3
