from __future__ import annotations

import hashlib
import threading
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi.testclient import TestClient

from selector_app.backtest.service import BacktestService
from selector_app.individual_research.models import IndividualResearchConfig
from selector_app.individual_research.service import IndividualStockResearchService
from selector_app.market_data.adapter import DuckDbMarketDataAdapter
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.portfolio_backtest.service import PortfolioBacktestService
from selector_app.screening.jobs import TaskCancelledError
from selector_app.web.app import create_app
from selector_app.web.schemas import PortfolioBacktestRequest


def _bars() -> pd.DataFrame:
    close = 10.0 + np.linspace(0.0, 2.0, 120) + np.sin(np.arange(120) / 3.0)
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=120, freq="D"),
            "open": close - 0.1,
            "high": close + 0.4,
            "low": close - 0.4,
            "close": close,
            "volume": np.full(120, 50_000.0),
            "amount": close * 50_000.0,
            "bar_status": "completed",
        }
    )


def _payload(tmp_path: Path) -> dict[str, object]:
    return {
        "market": "SH",
        "code": "600000",
        "date_mode": "custom",
        "start_date": "2024-01-01",
        "end_date": "2024-04-29",
        "value_ids": ["technical.rsi14"],
        "signal_ids": [],
        "risk_trials": 1000,
    }


class FakeIndividualResearchService:
    def __init__(self, *, cancel: bool = False) -> None:
        self.config = None
        self.started = threading.Event()
        self.cancel = cancel

    def run(self, config, progress_callback=None, abort_check=None):
        self.config = config
        self.started.set()
        if self.cancel:
            for _ in range(200):
                if abort_check is not None and abort_check():
                    raise TaskCancelledError("个股研究已取消")
                time.sleep(0.005)
        if progress_callback is not None:
            progress_callback(100, 100)
        return {
            "market": config.market,
            "code": config.code,
            "marker": "individual-research-complete",
            "results_by_sharpe": [],
            "results_by_return": [],
        }


def _wait_for_job(client: TestClient, job_id: str) -> dict[str, object]:
    for _ in range(1000):
        response = client.get(f"/api/v1/individual-research/jobs/{job_id}")
        assert response.status_code == 200
        state = response.json()["data"]
        if state["status"] in {"completed", "failed", "cancelled"}:
            return state
        time.sleep(0.01)
    raise AssertionError("individual research job did not finish")


def test_individual_research_job_persists_history_and_resolves_dates(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars())
    service = FakeIndividualResearchService()
    app = create_app(
        market_data_store=store,
        individual_research_service=service,
        auto_sync_enabled=False,
    )

    with TestClient(app) as client:
        created = client.post("/api/v1/individual-research", json=_payload(tmp_path))
        assert created.status_code == 202, created.text
        job_id = created.json()["data"]["job_id"]
        state = _wait_for_job(client, job_id)
        history = client.get("/api/v1/individual-research/history")
        detail = client.get(f"/api/v1/individual-research/history/{job_id}")

    assert state["status"] == "completed"
    assert state["result"]["marker"] == "individual-research-complete"
    assert history.status_code == 200
    assert history.json()["data"][0]["history_id"] == job_id
    assert detail.status_code == 200
    assert detail.json()["data"]["backtest_type"] == "individual_research"
    assert service.config.start_date == date(2024, 1, 1)
    assert service.config.end_date == date(2024, 4, 29)
    assert service.config.code == "600000"


def test_full_history_range_uses_the_selected_stocks_own_bar_dates(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    selected_bars = _bars()
    store.replace_local_bars("SH", "600000", "stock", selected_bars)
    other_bars = _bars()
    other_bars["date"] = pd.date_range("2018-01-01", periods=len(other_bars), freq="D")
    store.replace_local_bars("SZ", "000001", "stock", other_bars)
    service = FakeIndividualResearchService()
    app = create_app(
        market_data_store=store,
        individual_research_service=service,
        auto_sync_enabled=False,
    )

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/individual-research",
            json={"market": "SH", "code": "600000", "value_ids": ["technical.rsi14"]},
        )
        assert created.status_code == 202, created.text
        state = _wait_for_job(client, created.json()["data"]["job_id"])
        detail = client.get(f"/api/v1/individual-research/history/{state['job_id']}")

    assert state["status"] == "completed", state
    assert service.config.start_date == selected_bars["date"].iloc[0].date()
    assert service.config.end_date == selected_bars["date"].iloc[-1].date()
    assert detail.json()["data"]["request"]["date_mode"] == "full"


def test_individual_research_job_can_be_cancelled(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars())
    service = FakeIndividualResearchService(cancel=True)
    app = create_app(
        market_data_store=store,
        individual_research_service=service,
        auto_sync_enabled=False,
    )

    with TestClient(app) as client:
        created = client.post("/api/v1/individual-research", json=_payload(tmp_path))
        assert created.status_code == 202, created.text
        job_id = created.json()["data"]["job_id"]
        assert service.started.wait(timeout=2)
        cancelled = client.post(f"/api/v1/individual-research/jobs/{job_id}/cancel")
        state = _wait_for_job(client, job_id)

    assert cancelled.status_code == 200
    assert state["status"] == "cancelled"


def test_single_security_backtest_runs_without_ranking_output(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars())
    request = PortfolioBacktestRequest.model_validate(
        {
            "universe": "codes",
            "codes": ["SH:600000"],
            "single_security_mode": True,
            "ranking_value": None,
            "buy_rule": {
                "conditions": [
                    {
                        "left_value": "technical.rsi14",
                        "operator": "lte",
                        "right_constant": 85,
                    }
                ],
                "combine_mode": "all",
            },
            "sell_rule": {
                "conditions": [
                    {
                        "left_value": "technical.rsi14",
                        "operator": "gte",
                        "right_constant": 45,
                    }
                ],
                "combine_mode": "all",
            },
            "date_mode": "custom",
            "start_date": "2024-01-01",
            "end_date": "2024-04-29",
        }
    )

    report = PortfolioBacktestService(adapter=DuckDbMarketDataAdapter(store)).run(
        request.to_config()
    )
    payload = report.to_dict()

    assert payload["single_security_mode"] is True
    assert payload["processed"] == 1
    assert payload["max_positions"] == 1
    assert payload["ranking_value"] is None
    assert payload["rank_order"] is None
    assert payload["ranking_events"] == []


class FakeExitOptimizer:
    def __init__(self) -> None:
        self.requests = []

    def run(self, request, *, progress_callback=None, abort_check=None):
        self.requests.append(request)
        config = dict(request.base_config)
        config.update(
            {
                "stop_loss_pct": 0.08,
                "take_profit_pct": 0.2,
                "stop_loss_cooldown_days": 3,
                "take_profit_cooldown_days": 0,
                "trailing_stop_pct": None,
            }
        )
        candidate_hash = hashlib.sha256(
            repr((config.get("buy_rule"), config.get("sell_rule"))).encode()
        ).hexdigest()
        candidate = {
            "candidate_hash": candidate_hash,
            "config": config,
            "human_readable_rules": "单股条件规则 · 止损 8% · 止盈 20%",
            "metrics": {
                "train": {
                    "sharpe": 0.7,
                    "annual_return": 0.12,
                    "total_trades": 12,
                    "closed_trades": 12,
                },
                "validation": {
                    "sharpe": 0.8,
                    "annual_return": 0.15,
                    "total_return": 0.05,
                    "max_drawdown": -0.2,
                    "win_rate": 0.6,
                    "payoff_ratio": 1.6,
                    "total_trades": 6,
                    "closed_trades": 6,
                },
            },
        }
        if progress_callback is not None:
            progress_callback(1, 1, "completed", {})
        return {"results_by_sharpe": [candidate], "results_by_return": [candidate]}


def test_single_stock_job_runs_all_stages_and_saves_three_split_candidates(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    dates = pd.bdate_range("2020-01-01", periods=1000)
    index = np.arange(len(dates))
    close = 20.0 + np.sin(index * 2 * np.pi / 45.0) * 2.0 + index * 0.002
    frame = pd.DataFrame(
        {
            "date": dates,
            "open": close,
            "high": close + 0.25,
            "low": close - 0.25,
            "close": close,
            "volume": 100_000.0,
            "amount": close * 100_000.0,
            "bar_status": "completed",
        }
    )
    store.replace_local_bars("SH", "600000", "stock", frame)
    adapter = DuckDbMarketDataAdapter(store)
    exit_optimizer = FakeExitOptimizer()
    service = IndividualStockResearchService(
        adapter=adapter,
        backtest_service=BacktestService(adapter=adapter),
        portfolio_service=PortfolioBacktestService(adapter=adapter),
        strategy_service=exit_optimizer,
    )
    config = IndividualResearchConfig(
        market="SH",
        code="600000",
        name="浦发银行",
        start_date=dates[0].date(),
        end_date=dates[-1].date(),
        value_ids=("technical.rsi14",),
        signal_ids=(),
        candidate_limit=2,
        risk_trials=1000,
    )
    app = create_app(
        market_data_store=store,
        individual_research_service=service,
        auto_sync_enabled=False,
    )

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/individual-research",
            json={
                "market": config.market,
                "code": config.code,
                "date_mode": "custom",
                "start_date": config.start_date.isoformat(),
                "end_date": config.end_date.isoformat(),
                "value_ids": list(config.value_ids),
                "signal_ids": [],
                "candidate_limit": config.candidate_limit,
                "risk_trials": config.risk_trials,
            },
        )
        assert created.status_code == 202, created.text
        job_id = created.json()["data"]["job_id"]
        state = _wait_for_job(client, job_id)
        history = client.get("/api/v1/individual-research/history")
        saved = client.get(f"/api/v1/individual-research/history/{job_id}")

    assert state["status"] == "completed", state
    result = state["result"]
    assert result["single_security_mode"] is True
    assert result["split_metadata"]["train"]["days"] > 0
    assert result["split_metadata"]["validation"]["days"] > 0
    assert result["split_metadata"]["holdout"]["days"] > 0
    assert result["split_metadata"]["purge_trading_days"] == 10
    assert result["results_by_sharpe"]
    assert result["results_by_sharpe"][0]["config"]["ranking_value"] is None
    assert result["results_by_sharpe"][0]["config"]["codes"] == ["SH:600000"]
    assert result["results_by_sharpe"][0]["metrics"]["holdout"]["closed_trades"] > 0
    full_backtest_id = result["results_by_sharpe"][0]["full_backtest_id"]
    full_backtest = result["full_backtests"][full_backtest_id]
    assert full_backtest["bars"] == len(dates)
    assert full_backtest["equity_curve"]
    assert full_backtest["trades"]
    assert saved.json()["data"]["result"]["full_backtests"][full_backtest_id]["trades"]
    assert history.json()["data"][0]["history_id"] == job_id
    assert saved.json()["data"]["backtest_type"] == "individual_research"
    assert exit_optimizer.requests
    assert all(request.holdout_candidates == 0 for request in exit_optimizer.requests)
