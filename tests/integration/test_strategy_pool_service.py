from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import numpy as np
import pandas as pd

from selector_app.individual_research.service import InsufficientResearchHistoryError
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
        stock_start = date(2020, 1, 11) if config.code == "000001" else date(2020, 1, 1)
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
            "split_metadata": {
                "method": "按该股交易日顺序切分",
                "observed_windows": {
                    "train": {
                        "start_date": stock_start.isoformat(),
                        "end_date": date(2020, 1, 20).isoformat(),
                    },
                    "validation": {
                        "start_date": date(2020, 1, 25).isoformat(),
                        "end_date": date(2020, 2, 5).isoformat(),
                    },
                    "holdout": {
                        "start_date": date(2020, 2, 10).isoformat(),
                        "end_date": date(2020, 2, 20).isoformat(),
                    },
                },
            },
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
    # 测试标的的历史跨度必须跨过研究准入门槛（上市满一年，见
    # MIN_LISTING_HISTORY_DAYS）：条数少的夹具按更大的日距展开，保证
    # 首根到末根 ≥ 370 天，否则所有短夹具都会被「上市未满一年」拦下，
    # 测的就不再是它们本来要测的东西。
    step = max(1, -(-370 // max(count - 1, 1)))
    return pd.DataFrame(
        {
            "date": pd.date_range(start, periods=count, freq=f"{step}D"),
            "open": close,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": np.full(count, 5_000.0),
            "amount": close * 5_000,
            "bar_status": "completed",
        }
    )


def test_full_pool_research_splits_each_symbol_and_saves_each_symbol_incrementally(
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
    # 研究窗口 = 标的自身数据的首末根（夹具的日距是自适应的，不硬编码日期）。
    spans = {
        f"{item.market}{item.code}": (item.first_date, item.last_date)
        for item in store.list_instruments(instrument_types=("stock",))
    }
    assert [(config.start_date, config.end_date) for config in individual.configs] == [
        spans["SH600000"],
        spans["SZ000001"],
    ]
    assert all(config.split_windows is None for config in individual.configs)
    assert all(config.common_split_metadata is None for config in individual.configs)
    assert first["manual_overrides"]["annual_return"]["candidate_id"] == "kept-manual"
    assert first["automatic_candidates"]["annual_return"]["candidate_id"] == "candidate-600000"
    assert (
        first["automatic_candidates"]["annual_return"]["metrics"]["full_period"]["total_return"]
        == 0.31
    )
    assert first["automatic_candidates"]["sharpe"]["candidate_ref"] == "annual_return"
    split_metadata = first["automatic_candidates"]["annual_return"]["split_metadata"]
    assert split_metadata["observed_windows"]["train"]["start_date"] == "2020-01-01"
    second_split = second["automatic_candidates"]["annual_return"]["split_metadata"]
    assert second_split["observed_windows"]["train"]["start_date"] == "2020-01-11"
    assert first["benchmark"]["validation"]["total_return"] > 0
    assert first["benchmark"]["holdout"]["total_return"] > 0
    assert second["source_job_id"] == "batch-1"
    assert "holdout_portfolios" not in result
    assert portfolio.configs == []

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
    assert individual.configs[-1].split_windows is None
    refreshed = pool_store.get("SH", "600000")
    assert refreshed["benchmark"]["validation"]["total_return"] > 0
    assert refreshed["source_job_id"] == "single-rerun"
    assert refreshed["automatic_candidates"]["annual_return"]["candidate_id"] == "candidate-600000"
    assert refreshed["manual_overrides"]["annual_return"]["candidate_id"] == "kept-manual"


def test_no_strategy_entry_is_reused_until_recheck_window_elapses() -> None:
    """「没有策略」窗口内复用、到期重查；配置一变立即作废。"""

    now = datetime(2026, 10, 8, 12, 0, 0)
    fresh = ("market-v1", "config-v1", "no_strategy", False, datetime(2026, 10, 8))
    stale = ("market-v1", "config-v1", "no_strategy", False, datetime(2026, 8, 8))

    assert MarketStrategyPoolService._can_reuse(fresh, "config-v1", now=now)
    assert not MarketStrategyPoolService._can_reuse(stale, "config-v1", now=now)
    assert not MarketStrategyPoolService._can_reuse(fresh, "config-v2", now=now)
    # 行情指纹变了也不影响：复用只看「上次结论 + 结论的新鲜度」。
    assert MarketStrategyPoolService._can_reuse(
        ("market-v2", "config-v1", "ready", True, now),
        "config-v1",
        now=now,
    )


def test_only_selected_strategies_and_fresh_no_strategy_are_reused() -> None:
    now = datetime(2026, 10, 8, 12, 0, 0)
    prior = ("market-v1", "config-v1", "insufficient_history", False, now)

    assert not MarketStrategyPoolService._can_reuse(prior, "config-v1", now=now)
    assert not MarketStrategyPoolService._can_reuse(prior, "config-v2", now=now)
    assert not MarketStrategyPoolService._can_reuse(
        ("market-v1", "config-v1", "failed", False, now),
        "config-v1",
        now=now,
    )
    assert MarketStrategyPoolService._can_reuse(
        ("market-v1", "config-v1", "no_strategy", False, now),
        "config-v1",
        now=now,
    )
    assert MarketStrategyPoolService._can_reuse(
        ("market-v1", "config-v1", "ready", False, now),
        "config-v1",
        now=now,
    )
    assert MarketStrategyPoolService._can_reuse(
        ("market-v1", "config-v1", "partial", False, now),
        "config-v1",
        now=now,
    )


def test_weak_ready_entry_is_researched_again_after_recheck_window() -> None:
    """弱策略（样本不足 / 跑输基准）到期重查：节奏与 no_strategy 一致（2026-10-09）。

    只看自动候选——手工覆盖是用户自己的决定，重新寻优不会替换它。
    """

    now = datetime(2026, 10, 9, 12, 0, 0)
    stale = ("market-v1", "config-v1", "ready", True, datetime(2026, 8, 1))
    recent = ("market-v1", "config-v1", "ready", True, datetime(2026, 10, 1))

    def entry(closed_trades: float, total_return: float, benchmark: float = 0.5) -> dict:
        return {
            "automatic_candidates": {
                "annual_return": {
                    "metrics": {
                        "full_period": {
                            "closed_trades": closed_trades,
                            "total_return": total_return,
                        }
                    }
                },
                "sharpe": {"candidate_ref": "annual_return"},
            },
            "benchmark": {"full_period": {"total_return": benchmark}},
        }

    weak = entry(5, 0.9)
    loser = entry(50, 0.1)
    strong = entry(50, 0.9)

    assert MarketStrategyPoolService._research_recheck_due(weak, stale, now=now)
    assert MarketStrategyPoolService._research_recheck_due(loser, stale, now=now)
    assert not MarketStrategyPoolService._research_recheck_due(strong, stale, now=now)
    # 30 天内不重查；条目缺失 / 时间戳缺失不强制重查（保持原复用行为）
    assert not MarketStrategyPoolService._research_recheck_due(weak, recent, now=now)
    assert not MarketStrategyPoolService._research_recheck_due(None, stale, now=now)
    assert not MarketStrategyPoolService._research_recheck_due(
        weak, ("market-v1", "config-v1", "ready", True, None), now=now
    )
    # 缺基准只判样本；candidate_ref 指向的目标缺失时无从判定
    assert MarketStrategyPoolService._research_recheck_due(
        {
            "automatic_candidates": {
                "annual_return": {"metrics": {"full_period": {"closed_trades": 5}}}
            },
            "benchmark": None,
        },
        stale,
        now=now,
    )
    assert not MarketStrategyPoolService._research_recheck_due(
        {
            "automatic_candidates": {
                "annual_return": None,
                "sharpe": {"candidate_ref": "annual_return"},
            },
            "benchmark": {"full_period": {"total_return": 0.5}},
        },
        stale,
        now=now,
    )


def test_shelf_life_and_post_launch_degradation_trigger_re_research() -> None:
    """保质期与上线后衰减也会触发重新寻优（2026-10-09 用户决策）。"""

    now = datetime(2026, 10, 9, 12, 0, 0)
    strong_entry = {
        "automatic_candidates": {
            "annual_return": {
                "metrics": {"full_period": {"closed_trades": 50, "total_return": 0.9}}
            }
        },
        "benchmark": {"full_period": {"total_return": 0.5}},
    }

    # 保质期：ready 结论超过 180 天无论表现如何都重算；没到期不触发
    aged = ("market-v1", "config-v1", "ready", True, datetime(2026, 3, 1))
    fresh = ("market-v1", "config-v1", "ready", True, datetime(2026, 8, 1))
    assert MarketStrategyPoolService._research_recheck_due(strong_entry, aged, now=now)
    assert not MarketStrategyPoolService._research_recheck_due(strong_entry, fresh, now=now)

    # 上线后衰减：满 60 个交易日且上线后跑输同窗口基准 10% 以上；三个条件
    # （窗口天数 / 收益 / 基准）任一缺失或未达标都不触发
    degraded = {
        "post_launch_days": 70,
        "post_launch_return": -0.05,
        "post_launch_benchmark_return": 0.15,
    }
    healthy = {
        "post_launch_days": 70,
        "post_launch_return": 0.2,
        "post_launch_benchmark_return": 0.15,
    }
    too_short = {
        "post_launch_days": 30,
        "post_launch_return": -0.05,
        "post_launch_benchmark_return": 0.15,
    }
    legacy_snapshot = {"post_launch_days": 70, "post_launch_return": -0.05}

    assert MarketStrategyPoolService._research_recheck_due(
        strong_entry,
        fresh,
        {"annual_return": degraded, "sharpe": {}},
        now=now,
    )
    assert not MarketStrategyPoolService._research_recheck_due(
        strong_entry,
        fresh,
        {"annual_return": healthy, "sharpe": {}},
        now=now,
    )
    assert not MarketStrategyPoolService._research_recheck_due(
        strong_entry,
        fresh,
        {"annual_return": too_short, "sharpe": {}},
        now=now,
    )
    # 老快照没有上线后基准字段：衰减判定不生效，保持复用
    assert not MarketStrategyPoolService._research_recheck_due(
        strong_entry,
        fresh,
        {"annual_return": legacy_snapshot, "sharpe": {}},
        now=now,
    )


def test_full_research_retries_insufficient_history_but_single_reuses_unchanged_inputs(
    tmp_path,
) -> None:
    store = DuckDbMarketDataStore(tmp_path / "insufficient-history.duckdb")
    bars = _bars("2021-01-01", 40)
    store.replace_local_bars("SH", "600000", "stock", bars)
    adapter = DuckDbMarketDataAdapter(store)
    pool_store = DuckDbStrategyPoolStore(store)
    pool_store.initialize()

    class InsufficientIndividualResearchService:
        calls = 0

        def run(self, _config, _progress_callback=None, _abort_check=None):
            self.calls += 1
            raise InsufficientResearchHistoryError(f"data-{self.calls}", {})

    individual = InsufficientIndividualResearchService()
    service = MarketStrategyPoolService(
        store=store,
        adapter=adapter,
        individual_service=individual,
        pool_store=pool_store,
    )

    def writer(item):
        pool_store.persist_worker_entry(
            item,
            source_job_id=str(item.get("source_job_id") or "job"),
        )

    first = service.run(
        {"mode": "full", "risk_trials": 1_000},
        job_id="first",
        entry_writer=writer,
    )
    second = service.run(
        {"mode": "full", "risk_trials": 1_000},
        job_id="second",
        entry_writer=writer,
    )

    item = pool_store.get("SH", "600000")
    assert individual.calls == 2
    assert first["failed_symbols"] == 0
    assert first["skipped_symbols"] == 1
    assert second["skipped_symbols"] == 1
    assert item["status"] == "insufficient_history"
    assert item["data_fingerprint"] == "data-2"

    single_cached = service.run(
        {"mode": "single", "market": "SH", "code": "600000", "risk_trials": 1_000},
        job_id="single-cached",
        entry_writer=writer,
    )
    assert single_cached["single_candidate"]["status"] == "insufficient_history"
    assert single_cached["skipped_symbols"] == 1
    assert individual.calls == 2

    bars.loc[0, "close"] += 0.1
    bars.loc[0, "open"] += 0.1
    bars.loc[0, "high"] += 0.1
    bars.loc[0, "low"] += 0.1
    store.replace_local_bars("SH", "600000", "stock", bars)
    single = service.run(
        {"mode": "single", "market": "SH", "code": "600000", "risk_trials": 1_000},
        job_id="single-insufficient",
        entry_writer=writer,
    )
    assert single["single_candidate"]["status"] == "insufficient_history"
    assert single["single_candidate"]["saved_to_pool"] is True
    assert single["skipped_symbols"] == 1
    assert pool_store.get("SH", "600000")["status"] == "insufficient_history"
    assert individual.calls == 3

    third = service.run(
        {"mode": "full", "risk_trials": 1_000},
        job_id="third",
        entry_writer=writer,
    )
    assert third["skipped_symbols"] == 1
    assert individual.calls == 4


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


def test_full_reselection_skips_fresh_no_strategy_and_reruns_stale_ones(
    tmp_path, monkeypatch
) -> None:
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
    # 刚跑完的 no_strategy 在重查窗口内：预过滤剔除，不计入任务总数，
    # 页面不再显示「0 / 5224」式的假工作量，也不会从浦发银行重新跑起。
    assert first["total_symbols"] == 1
    assert second["total_symbols"] == 0
    assert second["skipped_symbols"] == 1
    assert individual.calls == 1
    assert pool_store.get("SH", "600000")["status"] == "no_strategy"

    # 重查窗口归零（模拟时间流逝到下一个周期）：同一只票重新进入队列。
    monkeypatch.setattr("selector_app.strategy_pool.service.NO_STRATEGY_RECHECK_DAYS", 0)
    third = service.run(
        {"mode": "full", "risk_trials": 1_000, "horizon": 5},
        job_id="no-strategy-third",
        entry_writer=persist,
    )
    assert third["total_symbols"] == 1
    assert third["skipped_symbols"] == 0
    assert individual.calls == 2
    assert pool_store.get("SH", "600000")["status"] == "no_strategy"


def test_full_research_defers_symbols_listed_less_than_a_year(tmp_path) -> None:
    """上市不满一年的标的不研究：指标预热与分段样本都不足。

    年线级指标在短历史上整段是 NaN，训练/验证段也覆盖不了一个完整的市场
    周期——机械通过样本门槛选出的策略统计上不可信。落 insufficient_history
    （每次增量重查），攒满一年后自动纳入，无需人工干预。
    """

    store = DuckDbMarketDataStore(tmp_path / "young.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars("2020-01-01", 60))
    # 新标的：60 根日线挤在最近两个月，首根到末根只有 59 天。
    young_start = (datetime.now().date() - timedelta(days=120)).isoformat()
    young_close = np.linspace(10.0, 12.0, 60)
    store.replace_local_bars(
        "SZ",
        "000001",
        "stock",
        pd.DataFrame(
            {
                "date": pd.date_range(young_start, periods=60, freq="D"),
                "open": young_close,
                "high": young_close + 0.2,
                "low": young_close - 0.2,
                "close": young_close,
                "volume": np.full(60, 5_000.0),
                "amount": young_close * 5_000,
                "bar_status": "completed",
            }
        ),
    )
    adapter = DuckDbMarketDataAdapter(store)
    pool_store = DuckDbStrategyPoolStore(store)
    pool_store.initialize()

    class NoStrategyService:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def run(self, config, progress_callback=None, abort_check=None):
            del progress_callback, abort_check
            self.calls.append(f"{config.market}{config.code}")
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

    result = service.run(
        {"mode": "full", "risk_trials": 1_000, "horizon": 5},
        job_id="young-symbol",
        entry_writer=persist,
    )

    assert result["failed_symbols"] == 0
    # 新股被挡在研究之外：只有老标的进了研究服务。
    assert individual.calls == ["SH600000"]
    assert pool_store.get("SH", "600000")["status"] == "no_strategy"
    young = pool_store.get("SZ", "000001")
    assert young["status"] == "insufficient_history"
    assert "上市未满一年" in (young["last_error"] or "")


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
    monkeypatch.setattr("selector_app.strategy_pool.service.POOL_RAMP_INTERVAL_SECONDS", 0.0)

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
    assert service._max_pool_workers == 8

    result = service.run(
        {"mode": "full", "risk_trials": 1_000, "horizon": 5},
        job_id="parallel-job",
        entry_writer=saved.append,
    )

    assert result["completed_symbols"] == 2
    assert len(saved) == 2


def test_rebuild_discards_old_pool_and_researches_unchanged_symbols(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars("2020-01-01", 60))
    pool = DuckDbStrategyPoolStore(store)
    individual = FakeIndividualResearchService()
    service = MarketStrategyPoolService(
        store=store,
        adapter=DuckDbMarketDataAdapter(store),
        individual_service=individual,
        pool_store=pool,
    )

    def writer(entry):
        pool.persist_worker_entry(entry, source_job_id="new")

    service.run({"mode": "full"}, job_id="old", entry_writer=writer)
    pool.set_manual_override("SH", "600000", "annual_return", {"candidate_id": "old-manual"})
    pool.set_manual_override("SZ", "999999", "annual_return", {"candidate_id": "stale"})
    checkpoints = []

    def write(entry):
        checkpoints.append(entry)
        writer(entry)

    result = service.run({"mode": "full", "rebuild": True}, job_id="new", entry_writer=write)
    assert checkpoints[0] == {"reset": True}
    assert len(individual.configs) == 2
    assert result["skipped_symbols"] == 0
    assert pool.get("SZ", "999999") is None
    assert pool.get("SH", "600000")["manual_overrides"]["annual_return"] is None
    assert store.list_instruments()


def test_cancelled_rebuild_does_not_clear_existing_strategies(tmp_path) -> None:
    import pytest

    from selector_app.screening.jobs import TaskCancelledError

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars("2020-01-01", 60))
    pool = DuckDbStrategyPoolStore(store)
    pool.set_manual_override("SH", "600000", "annual_return", {"candidate_id": "old"})
    service = MarketStrategyPoolService(
        store=store,
        adapter=DuckDbMarketDataAdapter(store),
        individual_service=FakeIndividualResearchService(),
        pool_store=pool,
    )
    with pytest.raises(TaskCancelledError):
        service.run({"mode": "full", "rebuild": True}, job_id="cancelled", abort_check=lambda: True)
    assert pool.get("SH", "600000") is not None
