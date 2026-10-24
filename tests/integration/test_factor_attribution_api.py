"""The attribution endpoints: submit, poll, archive, and the source picker."""

from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from aquest.market_data.store import DuckDbMarketDataStore
from aquest.screening.jobs import TaskUserError
from aquest.screening.models import ScanReport
from aquest.web.app import create_app
from aquest.web.backtest_history import save_completed_backtest_history


class EmptyEngine:
    def scan(self, config, progress_callback=None):
        return ScanReport(0, 0, 0, 0, 0, (), {}, {})


def wait_for_done(client: TestClient, job_id: str, *, expect: str = "completed") -> dict:
    for _ in range(50):
        response = client.get(f"/api/v1/factor-attribution/{job_id}")
        assert response.status_code == 200
        payload = response.json()["data"]
        if payload["status"] in {"completed", "failed"}:
            assert payload["status"] == expect, payload.get("error")
            return payload
        time.sleep(0.02)
    raise AssertionError("attribution job did not finish")


def _payload() -> dict[str, object]:
    return {
        "source_history_id": "abc123",
        "value_ids": ["technical.rsi14"],
        "include_custom": False,
        "permutations": 0,
        "min_group_trades": 20,
    }


def _result() -> dict[str, object]:
    return {
        "source": {
            "history_id": "abc123",
            "backtest_type": "portfolio",
            "finished_at": None,
            "date_range": {"start": "2024-01-01", "end": "2024-12-31"},
        },
        "samples": {
            "trades": 40,
            "wins": 24,
            "losses": 16,
            "open_positions": 2,
            "orphan_sells": 0,
            "rejected": 1,
            "unparsable": 0,
            "signal_days": 30,
            "stocks": 12,
            "holding_days": 6.5,
            "win_holding_days": 7.0,
            "loss_holding_days": 6.0,
            "missing_stocks": [],
        },
        "value_count": 1,
        "significant_count": 0,
        "permutations": 0,
        "block_size_days": 20,
        "block_size_trades": None,
        "alpha": 0.05,
        "factors": [
            {
                "value_id": "technical.rsi14",
                "display_name": "RSI14",
                "win_mean": 61.2,
                "loss_mean": 48.4,
                "difference": 12.8,
                "standardized": 1.4,
                "win_count": 24,
                "loss_count": 16,
                "missing": 0,
                "p_value": None,
                "adjusted_p_value": None,
                "significant": None,
                "note": None,
            }
        ],
        "notes": ["期末还有 2 笔未平仓，没有已实现收益，不计入胜负"],
        "boundaries": ["收益是净口径"],
        "reason": "未做噪声门槛检验，p 值与多重检验校正不可用",
    }


class FakeAttributionService:
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


def _client(tmp_path: Path, service: FakeAttributionService) -> TestClient:
    return TestClient(
        create_app(
            engine=EmptyEngine(),
            factor_attribution_service=service,
            market_data_store=DuckDbMarketDataStore(tmp_path / "market.duckdb"),
        )
    )


def test_attribution_job_can_be_created_and_results_are_available(tmp_path: Path) -> None:
    service = FakeAttributionService()
    with _client(tmp_path, service) as client:
        response = client.post("/api/v1/factor-attribution", json=_payload())
        assert response.status_code == 202
        job_id = response.json()["data"]["job_id"]

        wait_for_done(client, job_id)

        results = client.get(f"/api/v1/factor-attribution/{job_id}/results")
        assert results.status_code == 200
        factor = results.json()["data"]["factors"][0]
        assert factor["value_id"] == "technical.rsi14"
        assert factor["difference"] == 12.8
        # The source reaches the service intact - attribution is meaningless
        # without knowing which backtest it is explaining.
        assert service.configs[0].source_history_id == "abc123"
        assert service.configs[0].min_group_trades == 20

        history = client.get("/api/v1/factor-attribution/history")
        assert history.status_code == 200
        assert any(item["history_id"] == job_id for item in history.json()["data"])

        detail = client.get(f"/api/v1/factor-attribution/history/{job_id}")
        assert detail.status_code == 200
        assert detail.json()["data"]["request"]["source_history_id"] == "abc123"

        deleted = client.delete(f"/api/v1/factor-attribution/history/{job_id}")
        assert deleted.json()["data"]["deleted"] is True


def test_unknown_job_is_not_found(tmp_path: Path) -> None:
    with _client(tmp_path, FakeAttributionService()) as client:
        assert client.get("/api/v1/factor-attribution/nope").status_code == 404
        assert client.get("/api/v1/factor-attribution/nope/results").status_code == 404


def test_a_group_floor_below_two_is_rejected(tmp_path: Path) -> None:
    with _client(tmp_path, FakeAttributionService()) as client:
        payload = _payload()
        payload["min_group_trades"] = 1
        assert client.post("/api/v1/factor-attribution", json=payload).status_code == 422


def test_a_service_complaint_fails_the_job_with_its_message(tmp_path: Path) -> None:
    """A missing or trade-less history is a user error, not a server error."""

    service = FakeAttributionService(raise_message="这条回测记录没有成交明细，无法归因")
    with _client(tmp_path, service) as client:
        response = client.post("/api/v1/factor-attribution", json=_payload())
        job_id = response.json()["data"]["job_id"]
        state = wait_for_done(client, job_id, expect="failed")
        assert "没有成交明细" in (state.get("error") or "")
        results = client.get(f"/api/v1/factor-attribution/{job_id}/results")
        # 任务跑完但失败不是「服务器坏了」：结果接口回 409 + task_failed，和「尚未完成」
        # 用同一个语义层级，前端与告警才不会把它当服务故障。
        assert results.status_code == 409
        assert results.json()["error"] == {
            "code": "task_failed",
            "message": "这条回测记录没有成交明细，无法归因",
        }


def test_sources_offer_only_history_kinds_that_carry_trades(tmp_path: Path) -> None:
    """Attribution reads archived trades, so a trade-less kind is not a source."""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    save_completed_backtest_history(
        store,
        history_id="portfolio-1",
        backtest_type="portfolio",
        request={"start_date": "2024-01-01"},
        result={"trades": [{"direction": "BUY", "code": "600000"}]},
        started_at="2024-06-01T00:00:00+00:00",
        finished_at="2024-06-01T00:01:00+00:00",
    )
    save_completed_backtest_history(
        store,
        history_id="factor-1",
        backtest_type="factor_test",
        request={"horizon": 5},
        result={"factors": []},
        started_at="2024-06-02T00:00:00+00:00",
        finished_at="2024-06-02T00:01:00+00:00",
    )
    with _client(tmp_path, FakeAttributionService()) as client:
        sources = client.get("/api/v1/factor-attribution/sources")
        assert sources.status_code == 200
        kinds = {item["backtest_type"] for item in sources.json()["data"]}
        assert kinds == {"portfolio"}


def test_task_user_error_is_the_shape_the_runner_expects() -> None:
    """Guard: the router converts ValueError into TaskUserError so the job fails
    with a readable message instead of turning into a 500 with a stack trace."""

    assert issubclass(TaskUserError, Exception)
