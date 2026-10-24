"""The factor test endpoints: submit, poll, archive, restore."""

from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from aquest.market_data.store import DuckDbMarketDataStore
from aquest.screening.models import ScanReport
from aquest.web.app import create_app


class EmptyEngine:
    def scan(self, config, progress_callback=None):
        return ScanReport(0, 0, 0, 0, 0, (), {}, {})


def wait_for_done(client: TestClient, job_id: str) -> dict:
    for _ in range(50):
        response = client.get(f"/api/v1/factor-test/{job_id}")
        assert response.status_code == 200
        payload = response.json()["data"]
        if payload["status"] in {"completed", "failed"}:
            return payload
        time.sleep(0.02)
    raise AssertionError("factor test job did not finish")


def _payload() -> dict[str, object]:
    return {
        "universe": "sh",
        "start_date": "2024-01-01",
        "end_date": "2024-12-31",
        "horizon": 5,
        "value_ids": ["technical.rsi14"],
        "include_custom": False,
        "permutations": 0,
        "min_cross_section": 5,
    }


def _result() -> dict[str, object]:
    return {
        "universe": "sh",
        "stock_count": 3,
        "scanned_stocks": 3,
        "value_count": 1,
        "horizon": 5,
        "dates": ["2024-01-02"],
        "factors": [
            {
                "value_id": "technical.rsi14",
                "display_name": "RSI14",
                "order": "high",
                "ic_mean": 0.031,
                "icir": 0.42,
                "ic_days": 1,
                "ic_positive_rate": 1.0,
                "ic_series": [0.031],
                "ic_cumulative": [0.031],
                "ic_first_half": None,
                "ic_second_half": None,
                "layers": [],
                "layer_inversions": 0,
                "long_short": {
                    "mean_spread": 0.001,
                    "std": 0.0,
                    "t_stat": None,
                    "win_rate": 1.0,
                    "final": 0.001,
                    "max_drawdown": 0.0,
                    "days": 1,
                    "cumulative": [0.001],
                },
                "ic_threshold": None,
                "pvalue": None,
                "qvalue": None,
                "significant": False,
                "note": "未做噪声门槛检验",
            }
        ],
        "significant_count": 0,
        "reason": "未做噪声门槛检验，p 值与多重检验校正不可用",
        "boundaries": ["标签是收盘价到收盘价的前瞻收益"],
    }


class FakeFactorTestService:
    def __init__(self, *, raise_message: str | None = None) -> None:
        self.configs: list[object] = []
        self._raise_message = raise_message

    def run(self, config, progress_callback=None):
        self.configs.append(config)
        if self._raise_message is not None:
            raise ValueError(self._raise_message)
        if progress_callback:
            progress_callback(1, 1)
        return _result()


def test_a_failed_job_result_is_a_conflict_not_a_server_error(tmp_path: Path) -> None:
    """任务失败的 details 已在任务状态里给出，结果接口只负责说"没有结果"。"""

    (tmp_path / "vipdoc").mkdir()
    with TestClient(
        create_app(
            engine=EmptyEngine(),
            factor_test_service=FakeFactorTestService(raise_message="因子检验窗口不足 20 个交易日"),
            market_data_store=DuckDbMarketDataStore(tmp_path / "market.duckdb"),
        )
    ) as client:
        job_id = client.post("/api/v1/factor-test", json=_payload()).json()["data"]["job_id"]
        assert wait_for_done(client, job_id)["status"] == "failed"

        results = client.get(f"/api/v1/factor-test/{job_id}/results")
        assert results.status_code == 409
        assert results.json()["error"] == {
            "code": "task_failed",
            "message": "因子检验窗口不足 20 个交易日",
        }


def test_factor_test_job_can_be_created_and_results_are_available(tmp_path: Path) -> None:
    service = FakeFactorTestService()
    (tmp_path / "vipdoc").mkdir()
    with TestClient(
        create_app(
            engine=EmptyEngine(),
            factor_test_service=service,
            market_data_store=DuckDbMarketDataStore(tmp_path / "market.duckdb"),
        )
    ) as client:
        response = client.post("/api/v1/factor-test", json=_payload())
        assert response.status_code == 202
        job_id = response.json()["data"]["job_id"]
        state = wait_for_done(client, job_id)
        assert state["status"] == "completed"

        results = client.get(f"/api/v1/factor-test/{job_id}/results")
        assert results.status_code == 200
        assert results.json()["data"]["factors"][0]["value_id"] == "technical.rsi14"
        # The horizon is the whole label; it has to reach the service intact.
        assert service.configs[0].horizon == 5

        history = client.get("/api/v1/factor-test/history")
        assert history.status_code == 200
        assert any(item["history_id"] == job_id for item in history.json()["data"])

        detail = client.get(f"/api/v1/factor-test/history/{job_id}")
        assert detail.status_code == 200
        assert detail.json()["data"]["request"]["horizon"] == 5

        deleted = client.delete(f"/api/v1/factor-test/history/{job_id}")
        assert deleted.json()["data"]["deleted"] is True


def test_unknown_job_is_not_found(tmp_path: Path) -> None:
    with TestClient(
        create_app(
            engine=EmptyEngine(),
            factor_test_service=FakeFactorTestService(),
            market_data_store=DuckDbMarketDataStore(tmp_path / "market.duckdb"),
        )
    ) as client:
        assert client.get("/api/v1/factor-test/nope").status_code == 404
        assert client.get("/api/v1/factor-test/nope/results").status_code == 404


def test_out_of_range_horizon_is_rejected(tmp_path: Path) -> None:
    with TestClient(
        create_app(
            engine=EmptyEngine(),
            factor_test_service=FakeFactorTestService(),
            market_data_store=DuckDbMarketDataStore(tmp_path / "market.duckdb"),
        )
    ) as client:
        payload = _payload()
        payload["horizon"] = 0
        assert client.post("/api/v1/factor-test", json=payload).status_code == 422
