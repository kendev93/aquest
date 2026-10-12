from __future__ import annotations

import time
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi.testclient import TestClient

from aquest.condition_research.resources import ResearchResourceEstimate
from aquest.market_data.models import DataStoreStatus
from aquest.market_data.store import DuckDbMarketDataStore
from aquest.web.app import create_app


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


def test_condition_research_history_can_be_deleted_and_spares_other_types(
    tmp_path: Path,
) -> None:
    """研究历史和回测共用一张表，删除必须先认类型。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.save_backtest_history(
        "portfolio-1",
        backtest_type="portfolio",
        request={"universe": "all"},
        result={"performance": {"total_return": 0.1}},
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        finished_at=datetime(2026, 1, 1, 0, 2, tzinfo=timezone.utc),
    )
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
        assert wait_for_job(client, job_id)["status"] == "completed"

        assert client.delete("/api/v1/condition-research/history/portfolio-1").json() == {
            "data": {"deleted": False}
        }
        assert client.delete("/api/v1/condition-research/history/missing").json() == {
            "data": {"deleted": False}
        }
        # 别人的回测历史还好好的，不能被顺手删掉。
        assert [item["history_id"] for item in store.list_backtest_history("portfolio")] == [
            "portfolio-1"
        ]

        assert client.delete(f"/api/v1/condition-research/history/{job_id}").json() == {
            "data": {"deleted": True}
        }
        assert client.get("/api/v1/condition-research/history").json()["data"] == []


def test_condition_research_api_accepts_missing_sell_rule_as_hold_to_end(tmp_path: Path) -> None:
    """不传卖出规则表示「持有到期末」，应该被接受而不是当作缺字段。"""

    body = payload(tmp_path)
    body.pop("sell_rule")
    service = FakeConditionResearchService()
    app = create_app(condition_research_service=service, auto_sync_enabled=False)

    with TestClient(app) as client:
        created = client.post("/api/v1/condition-research", json=body)
        assert created.status_code == 202
        job_id = created.json()["data"]["job_id"]
        assert wait_for_job(client, job_id)["status"] == "completed"

    assert service.config is not None
    assert service.config.sell_rule is None


def test_condition_research_api_accepts_missing_buy_rule(tmp_path: Path) -> None:
    """不传买入规则表示「持续满仓」，应该被接受而不是当作缺字段。"""

    body = payload(tmp_path)
    body.pop("buy_rule")
    service = FakeConditionResearchService()
    app = create_app(condition_research_service=service, auto_sync_enabled=False)

    with TestClient(app) as client:
        created = client.post("/api/v1/condition-research", json=body)
        assert created.status_code == 202
        job_id = created.json()["data"]["job_id"]
        assert wait_for_job(client, job_id)["status"] == "completed"

    assert service.config is not None
    assert service.config.buy_rule is None


def test_condition_research_api_rejects_both_rules_missing(tmp_path: Path) -> None:
    invalid = payload(tmp_path)
    invalid.pop("buy_rule")
    invalid.pop("sell_rule")
    app = create_app(auto_sync_enabled=False)

    with TestClient(app) as client:
        response = client.post("/api/v1/condition-research", json=invalid)

    assert response.status_code == 422
    assert "不能同时留空" in response.json()["error"]["message"]


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


class _SharedSourceAdapter:
    """Stand-in adapter that reports its own data bounds."""

    def __init__(self, root: Path, start: date, end: date) -> None:
        self.database_path = root / "shared_market_data.db"
        self._start = start
        self._end = end

    def status(self):
        return DataStoreStatus(
            database_path=str(self.database_path),
            schema_version=0,
            instrument_count=1,
            bar_count=100,
            data_start=self._start,
            data_end=self._end,
        )

    def list_stock_refs(self, *_args, **_kwargs):
        return []


def test_recent5_window_follows_the_shared_source_not_the_local_store(tmp_path: Path) -> None:
    """共享行情模式下，默认区间必须按实际读取的库计算。

    本地 sidecar 在开启共享模式后不再推进，若拿它的 data_end 去算“最近五年”，
    研究会静默跑在一个过期的窗口上。
    """

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", bars())
    service = FakeConditionResearchService()
    adapter = _SharedSourceAdapter(tmp_path, date(1990, 12, 19), date(2026, 9, 11))
    app = create_app(
        market_data_store=store,
        market_data_adapter=adapter,
        condition_research_service=service,
        auto_sync_enabled=False,
    )

    request = payload(tmp_path)
    request["date_mode"] = "recent5"
    request.pop("start_date")
    request.pop("end_date")

    with TestClient(app) as client:
        created = client.post("/api/v1/condition-research", json=request)
        assert created.status_code == 202
        wait_for_job(client, created.json()["data"]["job_id"])

    assert service.config.end_date == date(2026, 9, 11)
    assert service.config.start_date == date(2021, 9, 11)


def test_ranking_value_must_be_cross_stock_comparable(tmp_path: Path) -> None:
    """按价格排序只是「谁股价贵谁排前面」，必须在 API 层就拦掉。"""

    request = payload(tmp_path)
    request["ranking_value"] = "price.close"

    app = create_app(auto_sync_enabled=False)
    with TestClient(app) as client:
        assert client.post("/api/v1/condition-research", json=request).status_code == 422

    request["ranking_value"] = "technical.rsi14"
    request["rank_order"] = "asc"
    request["ranking_top_n"] = 3
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", bars())
    service = FakeConditionResearchService()
    app = create_app(
        market_data_store=store,
        condition_research_service=service,
        auto_sync_enabled=False,
    )
    with TestClient(app) as client:
        created = client.post("/api/v1/condition-research", json=request)
        assert created.status_code == 202
        wait_for_job(client, created.json()["data"]["job_id"])

    assert service.config.ranking_value == "technical.rsi14"
    assert service.config.rank_order == "asc"
    assert service.config.ranking_top_n == 3
