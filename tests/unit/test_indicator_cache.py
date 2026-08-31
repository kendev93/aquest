from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from selector_app.backtest.models import BacktestConfig
from selector_app.backtest.service import BacktestService
from selector_app.formulas.registry import FORMULA_REGISTRY, FormulaRegistry
from selector_app.indicators.cache import IndicatorCache, frame_fingerprint
from selector_app.indicators.precompute import IndicatorPrecomputeService
from selector_app.market_data.adapter import DuckDbMarketDataAdapter
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.portfolio_backtest.models import PortfolioBacktestConfig
from selector_app.portfolio_backtest.service import PortfolioBacktestService
from selector_app.screening.engine import ScreenEngine
from selector_app.screening.models import ScanConfig
from selector_app.strategy_fitness.models import StrategyFitnessConfig
from selector_app.strategy_fitness.service import StrategyFitnessService


def _bars(length: int = 80) -> pd.DataFrame:
    close = 10.0 + np.linspace(0.0, 8.0, length) + np.sin(np.arange(length) / 3.0)
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=length, freq="D"),
            "open": close - 0.2,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.full(length, 100_000.0),
            "amount": close * 100_000.0,
            "bar_status": "completed",
        }
    )


def _counting_registry(calls: dict[str, int]) -> FormulaRegistry:
    definition = FORMULA_REGISTRY.formula("indicator_three")
    original = definition.calculate

    def calculate(frame: pd.DataFrame):
        calls["indicator_three"] = calls.get("indicator_three", 0) + 1
        return original(frame)

    return FormulaRegistry((replace(definition, calculate=calculate),))


def test_persistent_cache_reuses_formula_result_without_recalculating(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    frame = _bars()
    calls: dict[str, int] = {}
    registry = _counting_registry(calls)

    first = IndicatorCache(store, registry=registry).get_or_compute(
        frame,
        ("indicator_three",),
        market="SH",
        code="600000",
    )["indicator_three"]
    second = IndicatorCache(store, registry=registry).get_or_compute(
        frame.copy(deep=True),
        ("indicator_three",),
        market="SH",
        code="600000",
    )["indicator_three"]

    assert calls["indicator_three"] == 1
    assert np.array_equal(first.signals["accumulation_zone"], second.signals["accumulation_zone"])
    np.testing.assert_allclose(first.values["varo7"], second.values["varo7"], equal_nan=True)


def test_cache_fingerprint_invalidates_after_market_data_changes(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    frame = _bars()
    calls: dict[str, int] = {}
    registry = _counting_registry(calls)
    cache = IndicatorCache(store, registry=registry)

    cache.get_or_compute(frame, ("indicator_three",), market="SH", code="600000")
    changed = frame.copy(deep=True)
    changed.loc[50, "close"] += 1.0
    cache.get_or_compute(changed, ("indicator_three",), market="SH", code="600000")

    assert frame_fingerprint(frame) != frame_fingerprint(changed)
    assert calls["indicator_three"] == 2


def test_formula_version_and_corrupt_payload_are_cache_misses(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    frame = _bars()
    calls: dict[str, int] = {}
    version_one = _counting_registry(calls)
    cache = IndicatorCache(store, registry=version_one)
    cache.get_or_compute(frame, ("indicator_three",), market="SH", code="600000")

    definition = FORMULA_REGISTRY.formula("indicator_three")

    def versioned_calculate(data: pd.DataFrame):
        calls["indicator_three"] = calls.get("indicator_three", 0) + 1
        return definition.calculate(data)

    version_two = FormulaRegistry(
        (replace(definition, cache_version="2", calculate=versioned_calculate),)
    )
    IndicatorCache(store, registry=version_two).get_or_compute(
        frame,
        ("indicator_three",),
        market="SH",
        code="600000",
    )

    fingerprint = frame_fingerprint(frame)
    store.write_indicator_cache(
        market="SH",
        code="600000",
        timeframe="daily",
        price_basis="raw",
        formula_id="indicator_three",
        formula_version="2",
        data_fingerprint=fingerprint,
        data_start=date(2024, 1, 1),
        data_end=date(2024, 3, 20),
        data_length=len(frame),
        values_blob=b"not-a-numpy-file",
        signals_blob=b"not-a-numpy-file",
    )
    IndicatorCache(store, registry=version_two).get_or_compute(
        frame,
        ("indicator_three",),
        market="SH",
        code="600000",
    )

    assert calls["indicator_three"] == 3


def test_screen_engine_uses_shared_indicator_cache(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars())
    adapter = DuckDbMarketDataAdapter(store)
    calls: dict[str, int] = {}
    registry = _counting_registry(calls)
    cache = IndicatorCache(store, registry=registry)
    engine = ScreenEngine(adapter=adapter, registry=registry, indicator_cache=cache)
    config = ScanConfig(
        selected_signals=("indicator_three.accumulation_zone",),
        combine_mode="any",
        minimum_matches=None,
        universe="all",
        universe_file=None,
        vipdoc_path=str(tmp_path),
        workers=1,
        period="daily",
    )

    first = engine.scan(config)
    second = engine.scan(config)

    assert calls["indicator_three"] == 1
    assert first.summary_dict() == second.summary_dict()
    assert [match.to_dict() for match in first.results] == [
        match.to_dict() for match in second.results
    ]


def test_backtest_and_portfolio_reuse_the_same_persistent_result(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars(120))
    adapter = DuckDbMarketDataAdapter(store)
    calls: dict[str, int] = {}
    registry = _counting_registry(calls)
    cache = IndicatorCache(store, registry=registry)

    backtest_config = BacktestConfig(
        market="SH",
        code="600000",
        vipdoc_path=str(tmp_path),
        buy_signal="indicator_three.prepare_rally",
        sell_signal="indicator_three.end_zone",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 4, 29),
        initial_cash=100_000.0,
    )
    first = BacktestService(adapter=adapter, registry=registry, indicator_cache=cache).run(
        backtest_config
    )
    second = BacktestService(adapter=adapter, registry=registry, indicator_cache=cache).run(
        backtest_config
    )

    assert calls["indicator_three"] == 1
    assert first.to_dict() == second.to_dict()

    portfolio_config = PortfolioBacktestConfig(
        vipdoc_path=str(tmp_path),
        universe="all",
        selected_signals=("indicator_three.accumulation_zone",),
        combine_mode="any",
        minimum_matches=None,
        ranking_value="indicator_three.varo7",
        max_positions=1,
        sell_signal="indicator_three.end_zone",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 4, 29),
        initial_cash=100_000.0,
    )
    portfolio = PortfolioBacktestService(adapter=adapter, registry=registry, indicator_cache=cache)
    portfolio.run(portfolio_config)

    assert calls["indicator_three"] == 1


def test_strategy_fitness_reuses_cached_formula_across_time_windows(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars(120))
    adapter = DuckDbMarketDataAdapter(store)
    calls: dict[str, int] = {}
    registry = _counting_registry(calls)
    cache = IndicatorCache(store, registry=registry)
    strategy = PortfolioBacktestConfig(
        vipdoc_path=str(tmp_path),
        universe="all",
        selected_signals=("indicator_three.accumulation_zone",),
        combine_mode="any",
        minimum_matches=None,
        ranking_value="indicator_three.varo7",
        max_positions=1,
        stop_loss_pct=0.2,
        start_date=date(2024, 1, 1),
        end_date=date(2024, 4, 29),
        initial_cash=100_000.0,
    )

    report = StrategyFitnessService(
        adapter=adapter,
        registry=registry,
        indicator_cache=cache,
    ).run(
        StrategyFitnessConfig(
            strategy=strategy,
            train_ratio=0.5,
            validation_ratio=0.25,
            min_trades=1,
            max_test_drawdown=1.0,
        )
    )

    assert report.processed == 1
    assert calls["indicator_three"] == 1


def test_precompute_service_materializes_all_builtin_formulas(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars(120))
    adapter = DuckDbMarketDataAdapter(store)
    cache = IndicatorCache(store)

    report = IndicatorPrecomputeService(adapter=adapter, cache=cache).run(
        vipdoc_path=str(tmp_path),
        universe="all",
    )

    assert report.total_candidates == 1
    assert report.processed == 1
    assert report.computed == 3
    assert report.errors == 0

    cold_cache = IndicatorCache(store)
    cold_cache.get_or_compute(
        store.read_bars("SH", "600000"),
        FORMULA_REGISTRY.all_formula_ids(),
        market="SH",
        code="600000",
    )
    assert cold_cache.metrics()["persistent_hits"] == 3
    assert cold_cache.metrics()["computed"] == 0
