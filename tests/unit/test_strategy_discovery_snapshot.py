from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from selector_app.indicators.cache import IndicatorCache
from selector_app.market_data.adapter import DuckDbMarketDataAdapter
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.portfolio_backtest.service import PortfolioBacktestService
from selector_app.screening.models import ScreenCondition
from selector_app.screening.rules import RuleGroup
from selector_app.strategy_discovery.models import DiscoveryRequest, StrategyCandidate
from selector_app.strategy_discovery.snapshot import build_discovery_snapshot
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


def test_snapshot_replay_matches_ordinary_portfolio_backtest(tmp_path: Path) -> None:
    dates = pd.date_range("2024-01-01", periods=80, freq="D")
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
        )

    assert prepared.performance == ordinary.performance
    assert prepared.trades == ordinary.trades
    assert prepared.data_fingerprint == ordinary.data_fingerprint
