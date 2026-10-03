from __future__ import annotations

from datetime import date
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import numpy as np
import pandas as pd

from selector_app.market_data.adapter import DuckDbMarketDataAdapter
from selector_app.market_data.models import StockRef
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.screening.rules import RuleGroup
from selector_app.strategy_pool.resources import GiB, MemoryStatus
from selector_app.strategy_pool.service import MarketStrategyPoolService
from selector_app.strategy_pool.store import DuckDbStrategyPoolStore


class FakeIndividualResearchService:
    def __init__(self) -> None:
        self.configs = []
        self.full_period_ranges = []

    def run(self, config, progress_callback=None, abort_check=None):
        self.configs.append(config)
        if progress_callback is not None:
            progress_callback(100, 100)
        candidate = {
            "candidate_id": f"candidate-{config.code}",
            "candidate_hash": f"hash-{config.code}",
            "buy_rule": {"signals": ["technical.macd"]},
            "sell_rule": {"signals": ["technical.rsi14"]},
            "config": {"stop_loss_pct": 0.08},
            "metrics": {
                "validation": {
                    "annual_return": 0.2,
                    "sharpe": 1.1,
                    "max_drawdown": -0.12,
                    "closed_trades": 8,
                },
                "holdout": {"closed_trades": 4},
            },
        }
        return {
            "data_fingerprint": f"result-{config.code}",
            "results_by_return": [candidate],
            "results_by_sharpe": [candidate],
        }

    def full_period_metrics(self, candidate, start_date, end_date):
        del candidate
        self.full_period_ranges.append((start_date, end_date))
        return {"total_return": 0.31, "annual_return": 0.11}


class FakePortfolioBacktestService:
    def __init__(self) -> None:
        self.configs = []

    def run(self, config, progress_callback=None, abort_check=None):
        self.configs.append(config)
        if progress_callback is not None:
            progress_callback(2, 2)
        return SimpleNamespace(
            start_date=config.start_date.isoformat(),
            end_date=config.end_date.isoformat(),
            performance={"annual_return": 0.12, "sharpe": 1.1},
            benchmark={"total_return": 0.1},
            risk_controls={},
            trades=({"direction": "BUY"}, {"direction": "SELL"}),
            data_fingerprint="portfolio-bars-v1",
        )


def _bars(start: str, count: int) -> pd.DataFrame:
    close = np.linspace(10.0, 16.0, count)
    return pd.DataFrame(
        {
            "date": pd.date_range(start, periods=count, freq="D"),
            "open": close,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": np.full(count, 5_000.0),
            "amount": close * 5_000,
            "bar_status": "completed",
        }
    )


def test_full_pool_research_uses_common_windows_and_saves_each_symbol_incrementally(
    tmp_path,
) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars("2020-01-01", 60))
    store.replace_local_bars("SZ", "000001", "stock", _bars("2020-01-11", 50))
    adapter = DuckDbMarketDataAdapter(store)
    pool_store = DuckDbStrategyPoolStore(store)
    pool_store.initialize()
    pool_store.set_manual_override(
        "SH",
        "600000",
        "annual_return",
        {
            "candidate_id": "kept-manual",
            "buy_rule": {"signals": ["technical.macd"]},
            "sell_rule": {"signals": ["technical.rsi14"]},
            "config": {"stop_loss_pct": 0.1},
            "metrics": {
                "validation": {
                    "annual_return": 0.18,
                    "sharpe": 0.9,
                    "max_drawdown": -0.2,
                }
            },
        },
    )
    individual = FakeIndividualResearchService()
    portfolio = FakePortfolioBacktestService()
    service = MarketStrategyPoolService(
        store=store,
        adapter=adapter,
        individual_service=individual,
        pool_store=pool_store,
        portfolio_service=portfolio,
    )

    result = service.run(
        {"mode": "full", "risk_trials": 3_000, "horizon": 5},
        job_id="batch-1",
        entry_writer=lambda item: pool_store.persist_worker_entry(item, source_job_id="batch-1"),
    )

    first = pool_store.get("SH", "600000")
    second = pool_store.get("SZ", "000001")
    assert result["completed_symbols"] == 2
    assert individual.configs[0].split_windows == individual.configs[1].split_windows
    assert individual.configs[1].split_windows["train"][0] == date(2020, 1, 1)
    assert first["manual_overrides"]["annual_return"]["candidate_id"] == "kept-manual"
    assert first["automatic_candidates"]["annual_return"]["candidate_id"] == "candidate-600000"
    assert (
        first["automatic_candidates"]["annual_return"]["metrics"]["full_period"]["total_return"]
        == 0.31
    )
    assert first["automatic_candidates"]["sharpe"]["candidate_ref"] == "annual_return"
    assert first["benchmark"]["validation"]["total_return"] > 0
    assert first["benchmark"]["holdout"]["total_return"] > 0
    assert second["source_job_id"] == "batch-1"
    assert result["holdout_portfolios"]["annual_return"]["available"] is True
    assert result["holdout_portfolios"]["sharpe"]["available"] is True
    assert [config.start_date for config in portfolio.configs] == [
        individual.configs[0].split_windows["holdout"][0],
        individual.configs[0].split_windows["holdout"][0],
    ]
    assert all(config.max_positions == 5 for config in portfolio.configs)
    assert all(len(config.symbol_strategies) == 2 for config in portfolio.configs)

    with store._connect() as connection:
        connection.execute(
            "UPDATE strategy_pool_entries SET benchmark_json = NULL "
            "WHERE market = 'SH' AND code = '600000'"
        )

    resumed = service.run(
        {"mode": "full", "risk_trials": 3_000, "horizon": 5},
        job_id="batch-2",
        entry_writer=lambda item: pool_store.persist_worker_entry(item, source_job_id="batch-2"),
    )

    assert resumed["skipped_symbols"] == 2
    assert len(individual.configs) == 2
    assert pool_store.get("SH", "600000")["benchmark"]["validation"]["total_return"] > 0

    with store._connect() as connection:
        connection.execute(
            "UPDATE strategy_pool_entries SET benchmark_json = NULL "
            "WHERE market = 'SH' AND code = '600000'"
        )
    single = service.run(
        {"mode": "single", "market": "SH", "code": "600000", "risk_trials": 1_000},
        job_id="single-rerun",
        entry_writer=lambda item: pool_store.persist_worker_entry(
            item,
            source_job_id="single-rerun",
        ),
    )
    assert single["single_candidate"]["benchmark"]["validation"]["total_return"] > 0
    assert single["single_candidate"]["saved_to_pool"] is True
    refreshed = pool_store.get("SH", "600000")
    assert refreshed["benchmark"]["validation"]["total_return"] > 0
    assert refreshed["source_job_id"] == "single-rerun"
    assert refreshed["automatic_candidates"]["annual_return"]["candidate_id"] == "candidate-600000"
    assert refreshed["manual_overrides"]["annual_return"]["candidate_id"] == "kept-manual"


def test_no_strategy_entry_is_not_reused_on_full_reselection() -> None:
    prior = ("market-v1", "config-v1", "no_strategy", False)

    assert not MarketStrategyPoolService._can_reuse(prior, "market-v1", "config-v1")
    assert MarketStrategyPoolService._can_reuse(
        ("market-v1", "config-v1", "ready", True),
        "market-v1",
        "config-v1",
    )


def test_current_strategy_snapshot_includes_holds_beyond_five_portfolio_slots(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "strategy-holdings.duckdb")
    adapter = DuckDbMarketDataAdapter(store)
    pool_store = DuckDbStrategyPoolStore(store)
    pool_store.initialize()
    refs = [
        StockRef(
            market="SH",
            code=f"60000{index}",
            path=Path(),
            name=f"标的{index}",
        )
        for index in range(6)
    ]
    strategy_ids = [f"{ref.market}{ref.code}" for ref in refs]
    configs = []

    class HoldingPortfolio:
        def run(self, config, progress_callback=None, abort_check=None):
            del progress_callback, abort_check
            configs.append(config)
            holdings = [
                {
                    "market": ref.market,
                    "code": ref.code,
                    "name": ref.name,
                    "size": 100,
                    "entry_price": 10,
                    "unrealized_pnl_pct": 0.15,
                }
                for ref in refs
                if f"{ref.market}:{ref.code}" in config.codes
            ]
            return SimpleNamespace(
                current_state={"as_of": "2026-10-01", "holdings": holdings},
                trades=(),
                data_fingerprint="current-holdings",
            )

    service = MarketStrategyPoolService(
        store=store,
        adapter=adapter,
        individual_service=FakeIndividualResearchService(),
        pool_store=pool_store,
        portfolio_service=HoldingPortfolio(),
    )

    snapshot = service._current_strategy_snapshot(
        refs,
        {
            symbol: {
                "candidate_id": symbol,
                "buy_rule": RuleGroup(signals=("technical.macd",)),
                "sell_rule": RuleGroup(signals=("technical.rsi14",)),
            }
            for symbol in strategy_ids
        },
        {symbol: 1.0 for symbol in strategy_ids},
        start_date=date(2020, 1, 1),
        latest_date=date(2026, 10, 1),
        progress_callback=None,
        progress_index=0,
        progress_total=1,
        abort_check=None,
    )

    assert len(snapshot["holdings"]) == 6
    assert set(snapshot["holdings"][0]) == {
        "market",
        "code",
        "name",
        "status",
        "holding_return_pct",
    }
    assert snapshot["holdings"][0]["status"] == "held"
    assert snapshot["holdings"][0]["holding_return_pct"] == 0.15
    assert configs[0].max_positions == 6
    assert len(configs[0].codes) == 6


def test_full_reselection_runs_research_again_for_no_strategy_entries(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "no-strategy.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars("2020-01-01", 60))
    adapter = DuckDbMarketDataAdapter(store)
    pool_store = DuckDbStrategyPoolStore(store)
    pool_store.initialize()

    class NoStrategyService:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, config, progress_callback=None, abort_check=None):
            del config, progress_callback, abort_check
            self.calls += 1
            return {
                "data_fingerprint": "same-bars",
                "results_by_return": [],
                "results_by_sharpe": [],
            }

    individual = NoStrategyService()
    service = MarketStrategyPoolService(
        store=store,
        adapter=adapter,
        individual_service=individual,
        pool_store=pool_store,
        portfolio_service=FakePortfolioBacktestService(),
    )

    def persist(entry):
        pool_store.persist_worker_entry(entry, source_job_id=str(entry["source_job_id"]))

    first = service.run(
        {"mode": "full", "risk_trials": 1_000, "horizon": 5},
        job_id="no-strategy-first",
        entry_writer=persist,
    )
    second = service.run(
        {"mode": "full", "risk_trials": 1_000, "horizon": 5},
        job_id="no-strategy-second",
        entry_writer=persist,
    )

    assert first["failed_symbols"] == second["failed_symbols"] == 0
    assert second["skipped_symbols"] == 0
    assert individual.calls == 2
    assert pool_store.get("SH", "600000")["status"] == "no_strategy"


def test_reselection_backfills_full_period_return_without_researching_cached_rules(
    tmp_path,
) -> None:
    store = DuckDbMarketDataStore(tmp_path / "return-backfill.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars("2020-01-01", 60))
    adapter = DuckDbMarketDataAdapter(store)
    pool_store = DuckDbStrategyPoolStore(store)
    pool_store.initialize()
    individual = FakeIndividualResearchService()
    service = MarketStrategyPoolService(
        store=store,
        adapter=adapter,
        individual_service=individual,
        pool_store=pool_store,
        portfolio_service=FakePortfolioBacktestService(),
    )

    service.run(
        {"mode": "full", "risk_trials": 3_000, "horizon": 5},
        job_id="initial-full-return",
        entry_writer=lambda item: pool_store.persist_worker_entry(
            item,
            source_job_id="initial-full-return",
        ),
    )
    entry = pool_store.get("SH", "600000")
    annual = dict(entry["automatic_candidates"]["annual_return"])
    annual_metrics = dict(annual["metrics"])
    annual_metrics.pop("full_period")
    annual["metrics"] = annual_metrics
    pool_store.upsert_automatic(
        market="SH",
        code="600000",
        name="浦发银行",
        annual_candidate=annual,
        sharpe_candidate=entry["automatic_candidates"]["sharpe"],
        data_fingerprint=entry["data_fingerprint"],
        market_fingerprint=entry["market_fingerprint"],
        config_fingerprint=entry["config_fingerprint"],
        source_job_id="legacy-without-full-return",
        benchmark=entry["benchmark"],
    )
    backfilled: list[dict[str, object]] = []

    result = service.run(
        {"mode": "full", "risk_trials": 3_000, "horizon": 5},
        job_id="backfill-full-return",
        entry_writer=backfilled.append,
    )

    assert result["skipped_symbols"] == 1
    assert len(individual.configs) == 1
    assert backfilled[0]["annual_candidate"]["metrics"]["full_period"]["total_return"] == 0.31


def test_full_pool_research_runs_tickers_concurrently_when_memory_allows(
    tmp_path,
    monkeypatch,
) -> None:
    store = DuckDbMarketDataStore(tmp_path / "parallel.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars("2020-01-01", 60))
    store.replace_local_bars("SZ", "000001", "stock", _bars("2020-01-11", 50))
    adapter = DuckDbMarketDataAdapter(store)
    barrier = Barrier(2, timeout=5)

    class ConcurrentFake:
        def run(self, config, progress_callback=None, abort_check=None):
            barrier.wait()
            candidate = {
                "candidate_id": config.code,
                "candidate_hash": config.code,
                "buy_rule": {"signals": ["technical.macd"]},
                "sell_rule": {"signals": ["technical.rsi14"]},
                "config": {},
                "metrics": {
                    "validation": {
                        "annual_return": 0.1,
                        "sharpe": 1.0,
                        "max_drawdown": -0.1,
                    }
                },
            }
            return {
                "market_fingerprint": f"bars-{config.code}",
                "results_by_return": [candidate],
                "results_by_sharpe": [candidate],
            }

    monkeypatch.setattr(
        "selector_app.strategy_pool.service.memory_status",
        lambda: MemoryStatus(total_bytes=8 * GiB, available_bytes=8 * GiB, source="test"),
    )
    monkeypatch.setattr("selector_app.strategy_pool.service.visible_cpu_count", lambda: 4)
    monkeypatch.setattr("selector_app.strategy_pool.service.get_cpu_load_average_1m", lambda: 0.0)
    saved: list[dict[str, object]] = []
    service = MarketStrategyPoolService(
        store=store,
        adapter=adapter,
        individual_service=ConcurrentFake(),
        individual_service_factory=ConcurrentFake,
        max_pool_workers=8,
    )
    assert service._max_pool_workers == 2

    result = service.run(
        {"mode": "full", "risk_trials": 1_000, "horizon": 5},
        job_id="parallel-job",
        entry_writer=saved.append,
    )

    assert result["completed_symbols"] == 2
    assert len(saved) == 2
