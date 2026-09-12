from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from selector_app.indicators.cache import IndicatorCache
from selector_app.market_data.adapter import DuckDbMarketDataAdapter
from selector_app.market_data.models import StockRef
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.portfolio_backtest.service import PortfolioBacktestService
from selector_app.screening.models import ScreenCondition
from selector_app.screening.rules import RuleGroup
from selector_app.strategy_discovery.models import DiscoveryRequest, StrategyCandidate
from selector_app.strategy_discovery.snapshot import (
    SNAPSHOT_SYMBOL_BATCH_SIZE,
    _iter_ref_batches,
    build_discovery_snapshot,
    snapshot_window,
)
from selector_app.web.schemas import PortfolioBacktestRequest


def _experiment(store_path: Path) -> DiscoveryRequest:
    return DiscoveryRequest(
        base_config={
            "vipdoc_path": str(store_path.parent),
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
            "search": {"start_date": "2024-01-01", "end_date": "2024-02-29"},
            "validation": {"start_date": "2024-03-01", "end_date": "2024-03-10"},
            "holdout": {"start_date": "2024-03-11", "end_date": "2024-03-20"},
        },
        indicator_ids=("technical.ma5", "technical.ma20"),
        signal_ids=(),
        required_buy_conditions=(
            ScreenCondition(
                left_value="technical.ma5",
                operator="cross_up",
                right_value="technical.ma20",
            ),
        ),
        required_sell_conditions=(
            ScreenCondition(
                left_value="technical.ma5",
                operator="cross_down",
                right_value="technical.ma20",
            ),
        ),
        max_conditions_per_side=1,
        max_trials=1,
        min_trades=0,
        max_drawdown=None,
        multifidelity_enabled=False,
        seed=7,
    )


def _candidate_config() -> object:
    return PortfolioBacktestRequest.model_validate(
        {
            "universe": "all",
            "selected_signals": [],
            "combine_mode": "all",
            "minimum_matches": None,
            "buy_rule": {
                "signals": [],
                "conditions": [
                    {
                        "left_value": "technical.ma5",
                        "operator": "gt",
                        "right_value": None,
                        "right_constant": 1.0,
                    }
                ],
                "combine_mode": "all",
                "minimum_matches": None,
            },
            "ranking_value": "technical.ma5",
            "rank_order": "desc",
            "max_positions": 1,
            "rebalance_frequency": "daily",
            "sell_rule": {
                "signals": [],
                "conditions": [
                    {
                        "left_value": "technical.ma5",
                        "operator": "lt",
                        "right_value": None,
                        "right_constant": 100.0,
                    }
                ],
                "combine_mode": "all",
                "minimum_matches": None,
            },
            "start_date": "2024-01-01",
            "end_date": "2024-03-20",
        }
    ).to_config()


def test_snapshot_ref_batches_are_bounded() -> None:
    refs = [StockRef("SH", f"{index:06d}", Path()) for index in range(65)]

    batches = list(_iter_ref_batches(refs))

    assert [len(batch) for batch in batches] == [SNAPSHOT_SYMBOL_BATCH_SIZE, 1]
    assert [ref.code for ref in batches[0] + batches[1]] == [f"{index:06d}" for index in range(65)]


def test_snapshot_window_uses_all_discovery_split_boundaries(tmp_path: Path) -> None:
    window = snapshot_window(_experiment(tmp_path / "market.duckdb"))

    assert window.start == date(2024, 1, 1)
    assert window.end == date(2024, 3, 20)


def test_snapshot_replay_matches_ordinary_portfolio_backtest(tmp_path: Path) -> None:
    dates = pd.date_range("2024-01-01", periods=100, freq="D")
    close = np.linspace(10.0, 14.0, len(dates))
    store_path = tmp_path / "market.duckdb"
    store = DuckDbMarketDataStore(store_path)
    store.replace_local_bars(
        "SH",
        "600000",
        "stock",
        pd.DataFrame(
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
        ),
    )
    experiment = _experiment(store_path)
    candidate = StrategyCandidate(
        buy_rule=RuleGroup(
            signals=("indicator_three.suppress_main",),
            conditions=(
                ScreenCondition(
                    left_value="technical.ma5",
                    operator="cross_up",
                    right_value="technical.ma20",
                ),
            ),
        ),
        sell_rule=RuleGroup(
            signals=("indicator_one.wash",),
            conditions=(
                ScreenCondition(
                    left_value="technical.ma5",
                    operator="cross_down",
                    right_value="technical.ma20",
                ),
            ),
        ),
        ranking_value="indicator_three.var1",
        rank_order="desc",
        max_positions=1,
        stop_loss_mode="atr",
        stop_loss_atr_multiplier=2.0,
        stop_loss_cooldown_days=3,
        take_profit_mode="pct",
        take_profit_pct=0.1,
        trailing_stop_mode="pct",
        trailing_stop_pct=0.05,
        trailing_stop_cooldown_days=2,
        base_config=experiment.base_config,
    )
    config = PortfolioBacktestRequest.model_validate(
        {
            **candidate.to_config(),
            "start_date": "2024-01-01",
            "end_date": "2024-03-20",
        }
    ).to_config()
    portfolio = PortfolioBacktestService(
        adapter=DuckDbMarketDataAdapter(store),
        indicator_cache=IndicatorCache(),
    )

    ordinary = portfolio.run(config)
    with build_discovery_snapshot(store, experiment) as snapshot:
        prepared = portfolio.run_prepared_contexts(
            config,
            snapshot.contexts_for_config(config),
            total_candidates=snapshot.total_candidates,
            include_details=False,
        )
        snapshot_metadata = dict(snapshot.build_metadata)

    assert prepared.performance == ordinary.performance
    assert prepared.trades == ordinary.trades
    assert prepared.data_fingerprint == ordinary.data_fingerprint
    assert prepared.states == ()
    assert prepared.ranking_events == ()
    assert snapshot_metadata["snapshot_start_date"] == "2024-01-01"
    assert snapshot_metadata["snapshot_end_date"] == "2024-03-20"
    assert snapshot_metadata["snapshot_rows"] == 80
    assert snapshot_metadata["warmup_bars"] >= 1
    assert snapshot_metadata["snapshot_bytes"] > 0


def test_snapshot_reuses_persistent_indicator_cache_on_repeated_builds(tmp_path: Path) -> None:
    dates = pd.date_range("2024-01-01", periods=80, freq="D")
    close = np.linspace(10.0, 14.0, len(dates))
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars(
        "SH",
        "600000",
        "stock",
        pd.DataFrame(
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
        ),
    )
    experiment = replace(
        _experiment(store.database_path),
        required_buy_conditions=(),
        required_sell_conditions=(),
    )

    with build_discovery_snapshot(store, experiment) as first_snapshot:
        first_context = first_snapshot.contexts_for_config(
            PortfolioBacktestRequest.model_validate(
                {
                    "universe": "all",
                    "selected_signals": [],
                    "combine_mode": "all",
                    "minimum_matches": None,
                    "buy_rule": {
                        "signals": [],
                        "conditions": [
                            {
                                "left_value": "technical.ma5",
                                "operator": "gt",
                                "right_value": None,
                                "right_constant": 1.0,
                            }
                        ],
                        "combine_mode": "all",
                        "minimum_matches": None,
                    },
                    "ranking_value": "technical.ma5",
                    "rank_order": "desc",
                    "max_positions": 1,
                    "rebalance_frequency": "daily",
                    "sell_rule": {
                        "signals": [],
                        "conditions": [
                            {
                                "left_value": "technical.ma5",
                                "operator": "lt",
                                "right_value": None,
                                "right_constant": 100.0,
                            }
                        ],
                        "combine_mode": "all",
                        "minimum_matches": None,
                    },
                    "start_date": "2024-01-01",
                    "end_date": "2024-03-20",
                }
            ).to_config()
        )[0]
        first_metadata = dict(first_snapshot.build_metadata)

    with build_discovery_snapshot(store, experiment) as second_snapshot:
        second_context = second_snapshot.contexts_for_config(
            PortfolioBacktestRequest.model_validate(
                {
                    "universe": "all",
                    "selected_signals": [],
                    "combine_mode": "all",
                    "minimum_matches": None,
                    "buy_rule": {
                        "signals": [],
                        "conditions": [
                            {
                                "left_value": "technical.ma5",
                                "operator": "gt",
                                "right_value": None,
                                "right_constant": 1.0,
                            }
                        ],
                        "combine_mode": "all",
                        "minimum_matches": None,
                    },
                    "ranking_value": "technical.ma5",
                    "rank_order": "desc",
                    "max_positions": 1,
                    "rebalance_frequency": "daily",
                    "sell_rule": {
                        "signals": [],
                        "conditions": [
                            {
                                "left_value": "technical.ma5",
                                "operator": "lt",
                                "right_value": None,
                                "right_constant": 100.0,
                            }
                        ],
                        "combine_mode": "all",
                        "minimum_matches": None,
                    },
                    "start_date": "2024-01-01",
                    "end_date": "2024-03-20",
                }
            ).to_config()
        )[0]
        second_metadata = dict(second_snapshot.build_metadata)

    assert first_metadata["indicator_computed"] > 0
    assert second_metadata["indicator_persistent_hits"] > 0
    np.testing.assert_allclose(
        first_context.values["technical.ma5"],
        second_context.values["technical.ma5"],
        equal_nan=True,
    )


def test_snapshot_reuses_base_contexts_for_repeated_candidate_evaluations(tmp_path: Path) -> None:
    dates = pd.date_range("2024-01-01", periods=80, freq="D")
    close = np.linspace(10.0, 14.0, len(dates))
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars(
        "SH",
        "600000",
        "stock",
        pd.DataFrame(
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
        ),
    )
    experiment = replace(
        _experiment(store.database_path),
        required_buy_conditions=(),
        required_sell_conditions=(),
    )
    config = _candidate_config()

    with build_discovery_snapshot(store, experiment) as snapshot:
        first = snapshot.contexts_for_config(config)
        second = snapshot.contexts_for_config(config)
        metrics = snapshot.context_cache_metrics

    assert len(first) == len(second) == 1
    assert metrics["base_contexts_built"] == 1
    assert metrics["base_context_cache_hits"] > 0
    assert metrics["base_context_cache_limit"] == 256
    assert metrics["date_index_cache_limit"] == 512
