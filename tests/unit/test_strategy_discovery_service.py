from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.strategy_discovery.models import DiscoveryRequest
from selector_app.strategy_discovery.service import StrategyDiscoveryService


class FakePortfolioService:
    def __init__(self) -> None:
        self.calls = []

    def run(self, config):
        self.calls.append(config)
        value = 0.1 if config.ranking_value == "technical.ma5" else 0.2
        return {
            "performance": {
                "total_return": value,
                "annual_return": value,
                "sharpe": value * 10,
                "max_drawdown": 0.1,
                "total_trades": 10.0,
                "win_rate": 0.5,
                "volatility": 0.2,
            },
            "trades": [],
            "data_fingerprint": "a" * 64,
        }


def request() -> DiscoveryRequest:
    return DiscoveryRequest(
        base_config={
            "vipdoc_path": "/tmp/vipdoc",
            "universe": "all",
            "max_positions": 3,
            "initial_cash": 100_000.0,
            "commission": 0.0001,
            "min_commission": 0.1,
            "stamp_tax": 0.001,
            "slippage": 0.0,
            "execution": "next_open",
        },
        splits={
            "search": {"start_date": "2024-01-01", "end_date": "2024-12-31"},
            "validation": {"start_date": "2025-01-01", "end_date": "2025-12-31"},
            "holdout": {"start_date": "2026-01-01", "end_date": "2026-12-31"},
        },
        indicator_ids=("technical.ma5", "technical.ma20"),
        signal_ids=(),
        max_conditions_per_side=1,
        max_trials=4,
        validation_candidates=2,
        holdout_candidates=2,
        min_trades=1,
        max_drawdown=None,
        multifidelity_enabled=False,
        seed=11,
    )


def test_service_runs_candidates_through_portfolio_backtest_and_returns_metadata() -> None:
    portfolio = FakePortfolioService()
    result = StrategyDiscoveryService(portfolio_service=portfolio).run(request())

    assert result["results_by_sharpe"]
    assert result["results_by_return"]
    assert result["data_fingerprint"] == "a" * 64
    assert result["algorithm_metadata"]["seed"] == 11
    assert len(portfolio.calls) >= 6


def test_service_capabilities_describe_the_unified_indicator_catalog() -> None:
    capabilities = StrategyDiscoveryService(portfolio_service=FakePortfolioService()).capabilities()

    assert capabilities["enabled"] is True
    assert capabilities["signal_count"] == 15
    assert capabilities["indicator_count"] >= 20
    assert "technical.ma5" in {item["id"] for item in capabilities["indicators"]}


def test_service_progress_reports_overall_estimate_and_current_phase() -> None:
    events: list[tuple[int, int, str, dict[str, object]]] = []

    StrategyDiscoveryService(portfolio_service=FakePortfolioService()).run(
        request(),
        progress_callback=lambda current, total, phase, metadata: events.append(
            (current, total, phase, dict(metadata))
        ),
    )

    assert events
    assert all("overall_progress" in metadata for _, _, _, metadata in events)
    assert all("estimated_seconds_remaining" in metadata for _, _, _, metadata in events)
    assert any(metadata["phase_current"] == current for current, _, _, metadata in events)
    assert 0.0 <= float(events[-1][3]["overall_progress"]) <= 1.0


def test_service_rejects_an_empty_local_market_store_before_search(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="先在.*导入"):
        StrategyDiscoveryService(store=DuckDbMarketDataStore(tmp_path / "empty.duckdb")).run(
            request()
        )


def test_real_duckdb_service_evaluates_candidates_in_eight_processes(tmp_path: Path) -> None:
    dates = pd.date_range("2024-01-01", periods=30, freq="D")
    close = np.linspace(10.0, 13.0, len(dates))
    frame = pd.DataFrame(
        {
            "date": dates,
            "open": close,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": np.full(len(dates), 100_000.0),
            "amount": close * 100_000.0,
            "bar_status": "completed",
        }
    )
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", frame)
    experiment = DiscoveryRequest(
        base_config={
            "vipdoc_path": str(tmp_path),
            "universe": "all",
            "max_positions": 1,
            "initial_cash": 100_000.0,
            "commission": 0.0001,
            "min_commission": 0.1,
            "stamp_tax": 0.001,
            "slippage": 0.0,
            "execution": "next_open",
        },
        splits={
            "search": {"start_date": "2024-01-01", "end_date": "2024-01-18"},
            "validation": {"start_date": "2024-01-19", "end_date": "2024-01-24"},
            "holdout": {"start_date": "2024-01-25", "end_date": "2024-01-30"},
        },
        indicator_ids=("technical.ma5",),
        signal_ids=(),
        max_conditions_per_side=0,
        max_trials=8,
        validation_candidates=2,
        holdout_candidates=1,
        min_trades=0,
        max_drawdown=None,
        multifidelity_enabled=False,
        seed=19,
    )

    result = StrategyDiscoveryService(store=store).run(experiment)

    assert result["search_metadata"]["evaluation_mode"] == "process_pool"
    assert result["search_metadata"]["evaluation_workers"] == 8
    assert len(result["data_fingerprint"]) == 64
