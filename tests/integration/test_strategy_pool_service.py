from __future__ import annotations

from datetime import date
from threading import Barrier
from types import SimpleNamespace

import numpy as np
import pandas as pd

from selector_app.market_data.adapter import DuckDbMarketDataAdapter
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.strategy_pool.resources import GiB, MemoryStatus
from selector_app.strategy_pool.service import MarketStrategyPoolService
from selector_app.strategy_pool.store import DuckDbStrategyPoolStore


class FakeIndividualResearchService:
    def __init__(self) -> None:
        self.configs = []

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
    assert first["automatic_candidates"]["sharpe"]["candidate_ref"] == "annual_return"
    assert second["source_job_id"] == "batch-1"
    assert result["holdout_portfolios"]["annual_return"]["available"] is True
    assert result["holdout_portfolios"]["sharpe"]["available"] is True
    assert [config.start_date for config in portfolio.configs] == [
        individual.configs[0].split_windows["holdout"][0],
        individual.configs[0].split_windows["holdout"][0],
    ]
    assert all(config.max_positions == 5 for config in portfolio.configs)
    assert all(len(config.symbol_strategies) == 2 for config in portfolio.configs)

    resumed = service.run(
        {"mode": "full", "risk_trials": 3_000, "horizon": 5},
        job_id="batch-2",
        entry_writer=lambda item: pool_store.persist_worker_entry(item, source_job_id="batch-2"),
    )

    assert resumed["skipped_symbols"] == 2
    assert len(individual.configs) == 2


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
    monkeypatch.setattr("selector_app.strategy_pool.service.get_cpu_load_average_1m", lambda: 0.0)
    saved: list[dict[str, object]] = []
    service = MarketStrategyPoolService(
        store=store,
        adapter=adapter,
        individual_service=ConcurrentFake(),
        individual_service_factory=ConcurrentFake,
        max_pool_workers=2,
    )

    result = service.run(
        {"mode": "full", "risk_trials": 1_000, "horizon": 5},
        job_id="parallel-job",
        entry_writer=saved.append,
    )

    assert result["completed_symbols"] == 2
    assert len(saved) == 2
