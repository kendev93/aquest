from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.screening.models import ScreenCondition
from selector_app.screening.rules import RuleGroup
from selector_app.strategy_discovery.candidates import build_random_candidate
from selector_app.strategy_discovery.models import DiscoveryRequest, StrategyCandidate
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


def test_risk_only_service_searches_exit_controls_and_preserves_fixed_strategy() -> None:
    portfolio = FakePortfolioService()
    base_config = {
        "vipdoc_path": "/tmp/vipdoc",
        "universe": "all",
        "start_date": "2024-01-01",
        "end_date": "2024-12-31",
        "selected_signals": [],
        "combine_mode": "all",
        "minimum_matches": None,
        "buy_rule": {
            "signals": [],
            "conditions": [{"left_value": "technical.ma5", "operator": "gt", "right_constant": 10}],
            "required_conditions": [],
            "combine_mode": "all",
            "minimum_matches": None,
        },
        "sell_rule": {
            "signals": [],
            "conditions": [
                {"left_value": "technical.rsi14", "operator": "gte", "right_constant": 70}
            ],
            "required_conditions": [],
            "combine_mode": "all",
            "minimum_matches": None,
        },
        "ranking_value": "technical.ma5",
        "rank_order": "asc",
        "max_positions": 3,
        "rebalance_frequency": "weekly",
        "initial_cash": 100_000.0,
        "commission": 0.0002,
        "min_commission": 0.2,
        "stamp_tax": 0.001,
        "slippage": 0.01,
        "execution": "next_close",
        "atr_period": 14,
    }
    risk_request = DiscoveryRequest(
        base_config=base_config,
        splits={
            "search": {"start_date": "2024-01-01", "end_date": "2024-07-01"},
            "validation": {"start_date": "2024-07-02", "end_date": "2024-10-01"},
            "holdout": {"start_date": "2024-10-02", "end_date": "2024-12-31"},
        },
        candidate_mode="risk_only",
        max_trials=6,
        validation_candidates=6,
        holdout_candidates=20,
        min_trades=1,
        max_drawdown=None,
        max_complexity=4,
        multifidelity_enabled=False,
        seed=17,
    )

    result = StrategyDiscoveryService(portfolio_service=portfolio).run(risk_request)

    assert result["mode"] == "risk_optimization"
    assert len(str(result["configuration_fingerprint"])) == 64
    candidates = result["results_by_sharpe"]
    assert candidates
    for candidate in candidates:
        assert candidate["metrics"]["train"]["annual_return"] == 0.1
        assert candidate["metrics"]["validation"]["annual_return"] == 0.1
        assert candidate["metrics"]["holdout"].get("annual_return") == 0.1, {
            "metrics": candidate["metrics"],
            "validation_metadata": result["validation_metadata"],
        }
        config = candidate["config"]
        for key in (
            "buy_rule",
            "sell_rule",
            "ranking_value",
            "rank_order",
            "max_positions",
            "rebalance_frequency",
            "initial_cash",
            "commission",
            "min_commission",
            "stamp_tax",
            "slippage",
            "execution",
            "atr_period",
        ):
            assert config[key] == base_config[key]
        assert config["start_date"] == "2024-07-02"
        assert config["end_date"] == "2024-10-01"
    assert all(call.ranking_value == "technical.ma5" for call in portfolio.calls)


def test_service_capabilities_describe_the_unified_indicator_catalog() -> None:
    capabilities = StrategyDiscoveryService(portfolio_service=FakePortfolioService()).capabilities()

    assert capabilities["enabled"] is True
    assert capabilities["signal_count"] == len(capabilities["signals"])
    assert capabilities["signal_count"] >= 15
    assert "breakout_signal.breakthrough" in {item["id"] for item in capabilities["signals"]}
    assert capabilities["indicator_count"] >= 20
    assert "technical.ma5" in {item["id"] for item in capabilities["indicators"]}


def test_real_service_capabilities_use_the_configured_worker_count(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SELECTOR_DISCOVERY_WORKERS", "2")

    capabilities = StrategyDiscoveryService(
        store=DuckDbMarketDataStore(tmp_path / "market.duckdb")
    ).capabilities()

    assert capabilities["evaluation_workers"] == 2


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


def test_service_refines_a_parent_candidate_and_returns_both_leaderboards() -> None:
    parent_request = request()
    parent = build_random_candidate(parent_request, np.random.default_rng(7))
    experiment = {
        "history_id": "parent-1",
        "request": parent_request.to_dict(),
        "results_by_sharpe": [parent.to_dict()],
        "results_by_return": [],
        "data_fingerprint": "a" * 64,
    }
    portfolio = FakePortfolioService()

    result = StrategyDiscoveryService(portfolio_service=portfolio).refine(
        experiment,
        parent.candidate_hash,
        max_trials=4,
        seed=23,
    )

    assert result["mode"] == "refinement"
    assert result["refinement"]["parent_history_id"] == "parent-1"
    assert result["refinement"]["parent_candidate_hash"] == parent.candidate_hash
    assert result["results_by_sharpe"]
    assert result["results_by_return"]


def test_service_allows_refinement_when_parent_data_fingerprint_changed() -> None:
    parent_request = request()
    parent = build_random_candidate(parent_request, np.random.default_rng(7))
    experiment = {
        "history_id": "parent-1",
        "request": parent_request.to_dict(),
        "results_by_sharpe": [parent.to_dict()],
        "results_by_return": [],
        "data_fingerprint": "changed-data",
    }

    result = StrategyDiscoveryService(portfolio_service=FakePortfolioService()).refine(
        experiment,
        parent.candidate_hash,
        max_trials=4,
        seed=23,
    )

    assert result["refinement"]["data_changed"] is True
    assert result["refinement"]["parent_data_fingerprint"] == "changed-data"


def test_service_rejects_an_empty_market_store_before_search(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="请先同步行情"):
        StrategyDiscoveryService(store=DuckDbMarketDataStore(tmp_path / "empty.duckdb")).run(
            request()
        )


def test_real_duckdb_service_evaluates_candidates_in_configurable_process_pool(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SELECTOR_DISCOVERY_WORKERS", "2")
    dates = pd.date_range("2024-01-01", periods=80, freq="D")
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
            "search": {"start_date": "2024-01-01", "end_date": "2024-01-30"},
            "validation": {"start_date": "2024-01-31", "end_date": "2024-02-10"},
            "holdout": {"start_date": "2024-02-11", "end_date": "2024-02-20"},
        },
        indicator_ids=("technical.ma5",),
        signal_ids=(),
        max_conditions_per_side=0,
        max_trials=8,
        validation_candidates=2,
        holdout_candidates=1,
        min_trades=0,
        max_drawdown=None,
        multifidelity_enabled=True,
        seed=19,
    )

    events: list[tuple[int, int, str, dict[str, object]]] = []

    def candidate_builder(discovery_request: DiscoveryRequest, rng: object) -> StrategyCandidate:
        choice = getattr(rng, "choice")
        threshold = float(choice((0.0, 1.0, 2.0, 3.0, 4.0, 5.0)))
        return StrategyCandidate(
            buy_rule=RuleGroup(
                conditions=(
                    ScreenCondition(
                        left_value="technical.ma5",
                        operator="gt",
                        right_constant=threshold,
                    ),
                ),
            ),
            sell_rule=RuleGroup(
                conditions=(
                    ScreenCondition(
                        left_value="technical.ma5",
                        operator="lt",
                        right_constant=-1.0,
                    ),
                ),
            ),
            ranking_value="technical.ma5",
            rank_order="desc",
            max_positions=1,
            base_config=discovery_request.base_config,
        )

    result = StrategyDiscoveryService(store=store).run(
        experiment,
        candidate_builder=candidate_builder,
        progress_callback=lambda current, total, phase, metadata: events.append(
            (current, total, phase, dict(metadata))
        ),
    )

    assert result["search_metadata"]["evaluation_mode"] == "process_pool"
    assert result["search_metadata"]["evaluation_workers"] == 2
    assert result["algorithm_metadata"]["data_snapshot"] == "shared_memmap"
    assert result["algorithm_metadata"]["snapshot_build_seconds"] >= 0
    assert result["algorithm_metadata"]["snapshot_metrics"]["indicator_compute_seconds"] >= 0
    stage_durations = result["stage_durations_seconds"]
    assert stage_durations["snapshot"] >= 0
    assert stage_durations["search"] >= 0
    assert stage_durations["validation"] >= 0
    assert stage_durations["holdout"] >= 0
    assert result["algorithm_metadata"]["checkpoint_metrics"]["captured"] >= 1
    assert result["algorithm_metadata"]["checkpoint_metrics"]["resume_attempts"] >= 1
    assert events[0][2] == "preparing_snapshot"
    assert len(result["data_fingerprint"]) == 64
