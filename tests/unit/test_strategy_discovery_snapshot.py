from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from selector_app.formulas.registry import FORMULA_REGISTRY
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
    DiscoverySnapshot,
    _ArrayFormulaResult,
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


def test_required_mask_keeps_required_conditions_and_ors_generated_conditions() -> None:
    required = ScreenCondition(
        left_value="technical.ma5",
        operator="gt",
        right_constant=10.0,
    )
    generated_one = ScreenCondition(
        left_value="technical.ma20",
        operator="gt",
        right_constant=20.0,
    )
    generated_two = ScreenCondition(
        left_value="technical.ma20",
        operator="lt",
        right_constant=5.0,
    )
    rule = RuleGroup(
        conditions=(generated_one, generated_two),
        combine_mode="any",
        required_conditions=(required,),
    )
    results = {
        "technical": _ArrayFormulaResult(
            values={
                "ma5": np.asarray([9.0, 11.0, 9.0, 11.0]),
                "ma20": np.asarray([10.0, 21.0, 10.0, 4.0]),
            },
            signals={},
        )
    }

    mask = DiscoverySnapshot._candidate_mask(
        object(),
        rule,
        (required,),
        np.asarray([True, True, False, True]),
        results,
    )

    assert mask.tolist() == [False, True, False, True]

    refined_mask = DiscoverySnapshot._candidate_mask(
        object(),
        rule,
        (),
        np.ones(4, dtype=bool),
        results,
    )

    assert refined_mask.tolist() == [False, True, False, True]


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
    # 预热必须覆盖 recommended_bars 而不是 minimum_bars，否则长窗口指标
    # （rsrs_zscore 需 268 根）在窗口里绝大多数是 NaN，挖掘与全量复核会打架。
    recommended = max(
        FORMULA_REGISTRY.formula(formula_id).recommended_bars
        for formula_id in FORMULA_REGISTRY.all_formula_ids()
    )
    assert snapshot_metadata["warmup_bars"] >= recommended
    assert snapshot_metadata["snapshot_bytes"] > 0


def test_single_security_snapshot_needs_no_ranking_and_matches_direct_run(tmp_path: Path) -> None:
    dates = pd.date_range("2024-01-01", periods=120, freq="D")
    close = 10.0 + np.linspace(0.0, 2.0, len(dates)) + np.sin(np.arange(len(dates)) / 3.0)
    store_path = tmp_path / "market.duckdb"
    store = DuckDbMarketDataStore(store_path)
    store.replace_local_bars(
        "SH",
        "600000",
        "stock",
        pd.DataFrame(
            {
                "date": dates,
                "open": close - 0.1,
                "high": close + 0.4,
                "low": close - 0.4,
                "close": close,
                "volume": np.full(len(dates), 100_000.0),
                "amount": close * 100_000.0,
                "bar_status": "completed",
            }
        ),
    )
    payload = {
        "universe": "codes",
        "codes": ["SH:600000"],
        "single_security_mode": True,
        "ranking_value": None,
        "selected_signals": [],
        "combine_mode": "all",
        "minimum_matches": None,
        "buy_rule": {
            "signals": [],
            "conditions": [
                {
                    "left_value": "technical.rsi14",
                    "operator": "lte",
                    "right_value": None,
                    "right_constant": 60.0,
                }
            ],
            "combine_mode": "all",
            "minimum_matches": None,
        },
        "sell_rule": {
            "signals": [],
            "conditions": [
                {
                    "left_value": "technical.rsi14",
                    "operator": "gte",
                    "right_value": None,
                    "right_constant": 60.0,
                }
            ],
            "combine_mode": "all",
            "minimum_matches": None,
        },
        "max_positions": 1,
        "start_date": "2024-01-01",
        "end_date": "2024-04-29",
        "date_mode": "custom",
    }
    base = PortfolioBacktestRequest.model_validate(payload).model_dump(mode="json")
    experiment = DiscoveryRequest(
        base_config=base,
        splits={
            "search": {"start_date": "2024-01-01", "end_date": "2024-02-15"},
            "validation": {"start_date": "2024-02-16", "end_date": "2024-03-15"},
            "holdout": {"start_date": "2024-03-16", "end_date": "2024-04-29"},
        },
        candidate_mode="risk_only",
        max_trials=1,
        min_trades=0,
        max_drawdown=None,
        multifidelity_enabled=False,
    )
    config = PortfolioBacktestRequest.model_validate(payload).to_config()
    portfolio = PortfolioBacktestService(adapter=DuckDbMarketDataAdapter(store))

    ordinary = portfolio.run(config)
    with build_discovery_snapshot(store, experiment) as snapshot:
        contexts = snapshot.contexts_for_config(config, reuse_candidate_contexts=True)
        prepared = portfolio.run_prepared_contexts(
            config,
            contexts,
            total_candidates=snapshot.total_candidates,
            include_details=False,
        )

    assert len(contexts) == 1
    assert config.single_security_mode is True
    assert config.ranking_value in contexts[0].values
    assert np.all(contexts[0].values[config.ranking_value] == 0)
    assert prepared.performance == ordinary.performance
    assert prepared.trades == ordinary.trades
    assert prepared.single_security_mode is True


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


def test_worker_snapshot_can_skip_all_persistent_indicator_cache_io(tmp_path: Path) -> None:
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

    with build_discovery_snapshot(
        store,
        experiment,
        persist_indicator_cache=False,
    ) as snapshot:
        assert snapshot.build_metadata["indicator_computed"] > 0

    with duckdb.connect(str(store.database_path), read_only=True) as connection:
        row = connection.execute("SELECT COUNT(*) FROM indicator_cache").fetchone()
    assert row == (0,)


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
