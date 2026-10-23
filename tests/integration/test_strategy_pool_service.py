from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import numpy as np
import pandas as pd

from aquest.individual_research.service import InsufficientResearchHistoryError
from aquest.market_data.adapter import DuckDbMarketDataAdapter
from aquest.market_data.models import StockRef
from aquest.market_data.store import DuckDbMarketDataStore
from aquest.market_data.store_base import MarketDataStoreError
from aquest.screening.rules import RuleGroup
from aquest.strategy_pool import service as pool_service
from aquest.strategy_pool.resources import GiB, MemoryStatus
from aquest.strategy_pool.service import (
    MIN_LISTING_HISTORY_DAYS,
    MIN_RESEARCH_BARS,
    RESEARCH_WARMUP_BARS,
    MarketStrategyPoolService,
)
from aquest.strategy_pool.store import DuckDbStrategyPoolStore


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
                        "end_date": date(2020, 6, 30).isoformat(),
                    },
                    "validation": {
                        "start_date": date(2020, 7, 1).isoformat(),
                        "end_date": date(2020, 12, 31).isoformat(),
                    },
                    "holdout": {
                        "start_date": date(2021, 1, 1).isoformat(),
                        "end_date": date(2022, 1, 31).isoformat(),
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
    # 测试标的的历史跨度必须跨过研究准入门槛（全市场统一 730 天，见
    # MIN_LISTING_HISTORY_DAYS）：条数少的夹具按更大的日距展开，保证
    # 首根到末根 ≥ 740 天，否则所有短夹具都会被「上市历史不足」拦下，
    # 测的就不再是它们本来要测的东西。
    step = max(1, -(-740 // max(count - 1, 1)))
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
    # 2016 年后上市的池内标的：前 268 根只作预热，预热后至少留一年样本。
    assert all(config.warmup_bars == 268 for config in individual.configs)
    assert all(config.min_research_bars == 244 for config in individual.configs)
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


def test_late_listed_entries_warm_up_from_their_own_bars_and_invalidate_old_windows(
    tmp_path,
) -> None:
    """每只标的都按自己的 268 根预热，口径不再分老票新票。

    老票在 2016-01-01 之前只有 2015 年那 244 个交易日，凑不满 268，缺的部分由研究
    起点后移来补（2026-10-23 用户决策）。存量条目的一次性失效改用算法版本整体升一位，
    不再给 2016 后上市的标的单独挂指纹后缀。
    """

    store = DuckDbMarketDataStore(tmp_path / "warmup-window.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars("2015-01-01", 40))
    store.replace_local_bars("SZ", "000001", "stock", _bars("2020-01-01", 40))
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

    def persist(entry):
        pool_store.persist_worker_entry(entry, source_job_id=str(entry["source_job_id"]))

    payload = {"mode": "full", "risk_trials": 1_000, "horizon": 5}
    service.run(payload, job_id="warmup-first", entry_writer=persist)

    {config.code: config for config in individual.configs}
    # 两只拿到的预热与样本要求完全一样，不再按上市日分支。
    assert {config.warmup_bars for config in individual.configs} == {RESEARCH_WARMUP_BARS}
    assert {config.min_research_bars for config in individual.configs} == {MIN_RESEARCH_BARS}
    old = pool_store.get("SH", "600000")
    late = pool_store.get("SZ", "000001")
    assert old["config_fingerprint"] == late["config_fingerprint"]

    # 模拟口径升级前落库的条目：指纹是旧值，下一轮必须重研；另一只照常复用。
    with store._connect() as connection:
        connection.execute(
            "UPDATE strategy_pool_entries SET config_fingerprint = ? "
            "WHERE market = 'SZ' AND code = '000001'",
            ["stale-window-fingerprint"],
        )

    second = service.run(payload, job_id="warmup-second", entry_writer=persist)
    # 两只都留在队列里：600000 是要补全区间指标的欠账（假研究服务没写那一段），
    # 补完算跳过、不重新寻优；只有指纹过期的 000001 真去研究。
    assert second["total_symbols"] == 2
    assert second["skipped_symbols"] == 1
    assert len(individual.configs) == 3
    assert individual.configs[-1].code == "000001"
    assert individual.configs[-1].warmup_bars == RESEARCH_WARMUP_BARS
    refreshed = pool_store.get("SZ", "000001")
    assert refreshed["config_fingerprint"] == old["config_fingerprint"]


def test_full_period_replay_uses_the_effective_warmup_start(tmp_path) -> None:
    """研究服务后移了起点后，池里的全区间回测不能又回到上市首日。"""

    store = DuckDbMarketDataStore(tmp_path / "warmup-full-period.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars("2020-01-01", 60))
    adapter = DuckDbMarketDataAdapter(store)
    pool_store = DuckDbStrategyPoolStore(store)
    pool_store.initialize()

    class WarmStartFake(FakeIndividualResearchService):
        def run(self, config, progress_callback=None, abort_check=None):
            result = super().run(config, progress_callback, abort_check)
            result["start_date"] = (config.start_date + timedelta(days=500)).isoformat()
            return result

    individual = WarmStartFake()
    service = MarketStrategyPoolService(
        store=store,
        adapter=adapter,
        individual_service=individual,
        pool_store=pool_store,
        portfolio_service=FakePortfolioBacktestService(),
    )
    service.run(
        {"mode": "full", "risk_trials": 1_000, "horizon": 5},
        job_id="warmup-full-period",
        entry_writer=lambda item: pool_store.persist_worker_entry(
            item,
            source_job_id="warmup-full-period",
        ),
    )

    assert individual.full_period_ranges
    assert individual.full_period_ranges[0][0] == individual.configs[0].start_date + timedelta(
        days=500
    )


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


def test_full_research_prescan_keeps_recheck_due_ready_entry_in_queue(tmp_path) -> None:
    """预扫描与提交循环的重查判定必须同口径（2026-10-13 修复）。

    弱策略到期的 ready 条目若被预扫描当成纯复用剔掉，提交循环里的重查分支
    就永远轮不到——这里端到端验证它会重新寻优，且进度总数把它计入队列。
    """

    store = DuckDbMarketDataStore(tmp_path / "recheck-prescan.duckdb")
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

    def persist(entry):
        pool_store.persist_worker_entry(entry, source_job_id=str(entry["source_job_id"]))

    first = service.run(
        {"mode": "full", "risk_trials": 3_000, "horizon": 5},
        job_id="recheck-first",
        entry_writer=persist,
    )
    assert first["total_symbols"] == 1
    assert len(individual.configs) == 1

    # 把全区间指标改成「样本不足」（平仓交易 5 笔 < 20）制造弱策略，
    # 再把 updated_at 回拨到重查窗口（21 天）之外。
    entry = pool_store.get("SH", "600000")
    annual = dict(entry["automatic_candidates"]["annual_return"])
    annual_metrics = dict(annual["metrics"])
    annual_metrics["full_period"] = {"closed_trades": 5, "total_return": 0.9}
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
        source_job_id="recheck-weak",
        benchmark=entry["benchmark"],
    )
    aged = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
    with store._connect() as connection:
        connection.execute(
            "UPDATE strategy_pool_entries SET updated_at = ? "
            "WHERE market = 'SH' AND code = '600000'",
            [aged],
        )

    second = service.run(
        {"mode": "full", "risk_trials": 3_000, "horizon": 5},
        job_id="recheck-second",
        entry_writer=persist,
    )

    # 预扫描没有把它剔掉：仍计入任务总数并重新寻优，而不是纯复用。
    assert second["total_symbols"] == 1
    assert second["skipped_symbols"] == 0
    assert len(individual.configs) == 2


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


def _young_bars(start: str, count: int) -> pd.DataFrame:
    """按真实日距展开的短历史（上市未满两年），用来测排期而不是研究本身。"""

    close = np.linspace(10.0, 12.0, count)
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


def test_full_research_defers_insufficient_history_to_a_scheduled_date(tmp_path) -> None:
    """历史样本不足不再每天占位：未满两年的排到「首日 + 730 天」，其余每 30 天重查。

    2026-10-19 实测一轮 80 只的增量队列里 59 只是这么被反复重查的（全是上市未满一年），
    进度条 100% 而「已完成」只有几十，用户以为任务没干活。
    """

    store = DuckDbMarketDataStore(tmp_path / "insufficient-history.duckdb")
    # 老标的：上市跨度已经够两年，研究却仍切不出三段 → 走 30 天节奏。
    bars = _bars("2015-01-01", 40)
    store.replace_local_bars("SH", "600000", "stock", bars)
    # 新标的：首根日线起不满两年（268 根预热 + 一年样本）→ 能算出确切的重算日子。
    store.replace_local_bars("SZ", "000001", "stock", _young_bars("2021-01-01", 60))
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

    progress: list[dict[str, object]] = []
    first = service.run(
        {"mode": "full", "risk_trials": 1_000},
        job_id="first",
        entry_writer=writer,
        progress_callback=lambda _index, _total, _phase, metadata: progress.append(
            dict(metadata or {})
        ),
    )
    second = service.run(
        {"mode": "full", "risk_trials": 1_000},
        job_id="second",
        entry_writer=writer,
    )

    item = pool_store.get("SH", "600000")
    young = pool_store.get("SZ", "000001")
    assert first["failed_symbols"] == 0
    assert first["total_symbols"] == first["skipped_symbols"] == 2
    # 未满两年是政策闸，连研究都不该开始。
    assert individual.calls == 1
    assert item["status"] == "insufficient_history"
    assert young["status"] == "insufficient_history"
    assert item["data_fingerprint"] == "data-1"
    # 重算日子写在错误里：用户在详情里就能看到它哪天回来，不用自己算。
    assert "2023-01-01 起自动重算" in (young["last_error"] or "")
    # 进度元数据要单独报出这个数：它本来混在 skipped 里，摊不开的话进度条 100% 而
    # 「已完成」只有几十，看着就像任务没干活（2026-10-19 用户报告）。
    assert progress[-1]["insufficient_symbols"] == 2
    # 「已完成」按 completed − skipped 算，所以这里必须还是 0；摊开的那一项才是 2。
    assert progress[-1]["completed_symbols"] == progress[-1]["skipped_symbols"] == 2

    # 首轮没有排期：这两只都是真跑了研究才知道不足（一个当场被政策闸掉）。
    assert first["deferred_symbols"] == 0
    # 同一天再点增量：两只都排在未来，队列里一只都不剩。
    assert second["total_symbols"] == 0
    assert second["skipped_symbols"] == second["failed_symbols"] == 0
    assert individual.calls == 1
    assert item["data_fingerprint"] == "data-1"
    assert second["deferred_symbols"] == 2
    # 最早的那天就是用户能等到结果的日期：新股 2023-01-01，老标的要等 30 天节奏。
    assert second["next_eligible_date"] == "2023-01-01"

    estimate = service.estimate_incremental({"mode": "full", "risk_trials": 1_000})
    assert estimate["pending_symbols"] == 0
    assert estimate["deferred_symbols"] == 2
    # 排期出去的量单独算一项，不混进「可复用」：用户看到「待重算 0」时得知道
    # 这两只是哪天回来，而不是以为它们已经有策略了。
    assert estimate["reusable_symbols"] == 0
    assert (
        estimate["pending_symbols"] + estimate["reusable_symbols"] + estimate["deferred_symbols"]
        == estimate["universe_symbols"]
    )
    assert estimate["next_eligible_date"] == "2023-01-01"

    single_cached = service.run(
        {"mode": "single", "market": "SH", "code": "600000", "risk_trials": 1_000},
        job_id="single-cached",
        entry_writer=writer,
    )
    assert single_cached["single_candidate"]["status"] == "insufficient_history"
    assert single_cached["skipped_symbols"] == 1
    assert individual.calls == 1

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
    # 显式单股重跑不受排期影响：用户点名要研究这只，当天就得重查。
    assert individual.calls == 2


def test_insufficient_deferral_dates_and_fallback_rhythm() -> None:
    """排期口径：算得出日期的用「首日 + 730 天」，算不出的按 30 天节奏回查。"""

    ref = StockRef(
        market="SH",
        code="600000",
        path=Path("SH600000"),
        first_date=date(2021, 1, 1),
    )
    now = datetime(2026, 10, 19, 12, 0)
    prior = ("market-1", "config-1", "insufficient_history", False, now)
    defer = MarketStrategyPoolService._insufficient_deferral

    assert MIN_LISTING_HISTORY_DAYS == 730
    # 排到首日 + 730 天（自身 268 根预热 + 244 根研究样本 ≈ 512 根），
    # 哪怕行情截至日还在那之前。
    assert defer(ref, prior, data_end=date(2022, 2, 9), now=now) == date(2023, 1, 1)
    # 攒满两年的当天就回队列：上次失败在两年之前，30 天节奏早就到期了。
    crossed = ("market-1", "config-1", "insufficient_history", False, datetime(2021, 1, 5))
    assert defer(ref, crossed, data_end=date(2023, 1, 1), now=now) is None
    # 历史够长却仍然不足（切不出三段）：没有确切日子可排，按 30 天节奏回查。
    old_bars = StockRef(
        market="SH", code="600000", path=Path("SH600000"), first_date=date(2015, 1, 1)
    )
    assert defer(old_bars, prior, data_end=date(2026, 10, 16), now=now) == date(2026, 11, 18)
    assert defer(old_bars, prior, data_end=date(2026, 10, 16), now=datetime(2026, 12, 1)) is None
    # 时间戳缺失时宁可重查一次：结论会被刷新进库，排期没有依据。
    no_stamp = ("market-1", "config-1", "insufficient_history", False, None)
    assert defer(old_bars, no_stamp, data_end=date(2026, 10, 16), now=now) is None
    # 排期只管历史样本不足；ready / no_strategy / failed 一律照常走各自判定。
    for status in ("ready", "partial", "no_strategy", "failed"):
        assert defer(ref, ("m", "c", status, True, now), data_end=date(2021, 2, 9), now=now) is None
    assert defer(ref, None, data_end=date(2021, 2, 9), now=now) is None
    # 首根日线读不到（新入库、目录里还没有）时按 30 天节奏，不假装排得出日期。
    no_first = StockRef(market="SH", code="600000", path=Path("SH600000"), first_date=None)
    assert defer(no_first, prior, data_end=date(2026, 10, 16), now=now) == date(2026, 11, 18)


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
    monkeypatch.setattr("aquest.strategy_pool.service.NO_STRATEGY_RECHECK_DAYS", 0)
    third = service.run(
        {"mode": "full", "risk_trials": 1_000, "horizon": 5},
        job_id="no-strategy-third",
        entry_writer=persist,
    )
    assert third["total_symbols"] == 1
    assert third["skipped_symbols"] == 0
    assert individual.calls == 2
    assert pool_store.get("SH", "600000")["status"] == "no_strategy"


def test_full_research_defers_symbols_listed_less_than_two_years(tmp_path) -> None:
    """上市历史不满两年的标的不研究：268 根预热 + 一年样本都不足。

    年线级指标在短历史上整段是 NaN，训练/验证段也覆盖不了一个完整的市场
    周期——机械通过样本门槛选出的策略统计上不可信。落 insufficient_history
    （按首日 + 730 天排期），攒满两年后自动纳入，无需人工干预。
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
    assert "上市历史不足" in (young["last_error"] or "")


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
    monkeypatch.setattr("aquest.strategy_pool.service.POOL_RAMP_INTERVAL_SECONDS", 0.0)

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
        "aquest.strategy_pool.service.memory_status",
        lambda: MemoryStatus(total_bytes=8 * GiB, available_bytes=8 * GiB, source="test"),
    )
    monkeypatch.setattr("aquest.strategy_pool.service.visible_cpu_count", lambda: 4)
    monkeypatch.setattr("aquest.strategy_pool.service.get_cpu_load_average_1m", lambda: 0.0)
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

    from aquest.screening.jobs import TaskCancelledError

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


class _BusyMarketResearchService(FakeIndividualResearchService):
    """前 N 次运行报「行情库被占用」，模拟 backend 独占写库时拿不到只读句柄。"""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures
        self.calls = 0

    def run(self, config, progress_callback=None, abort_check=None):  # noqa: ANN001
        self.calls += 1
        if self.failures > 0:
            self.failures -= 1
            raise MarketDataStoreError(
                "行情数据库当前被其它进程占用或存在瞬态 I/O 错误，请稍后重试"
            )
        return super().run(config, progress_callback, abort_check)


def _busy_market_setup(tmp_path):  # noqa: ANN001, ANN202
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars("2020-01-01", 60))
    store.replace_local_bars("SZ", "000001", "stock", _bars("2020-01-11", 50))
    pool_store = DuckDbStrategyPoolStore(store)
    pool_store.initialize()
    return store, pool_store


def test_pool_research_retries_a_symbol_while_the_market_db_is_busy(tmp_path, monkeypatch) -> None:
    """backend 落库/写跟踪快照的窗口不该白丢整只标的已完成的研究。"""

    store, pool_store = _busy_market_setup(tmp_path)
    individual = _BusyMarketResearchService(failures=2)
    service = MarketStrategyPoolService(
        store=store,
        adapter=DuckDbMarketDataAdapter(store),
        individual_service=individual,
        pool_store=pool_store,
    )
    pauses: list[float] = []
    monkeypatch.setattr(pool_service.time, "sleep", pauses.append)

    result = service.run(
        {"mode": "full", "risk_trials": 3_000, "horizon": 5},
        job_id="retry-1",
        entry_writer=lambda item: pool_store.persist_worker_entry(item, source_job_id="retry-1"),
    )

    assert result["failed_symbols"] == 0
    assert result["completed_symbols"] == 2
    assert result["errors"] == []
    # 两次失败各退避一次（30 秒、90 秒），然后同一只标的重新研究成功。
    assert len(pauses) == 2
    assert individual.calls >= 4
    assert pool_store.get("SH", "600000")["automatic_candidates"]["annual_return"] is not None
    assert pool_store.get("SH", "600000")["last_error"] in (None, "")


def test_pool_research_reports_contention_after_exhausting_retries(tmp_path, monkeypatch) -> None:
    """退避重跑用尽才记失败，而且原因要写成人能看懂的一句。"""

    store, pool_store = _busy_market_setup(tmp_path)
    individual = _BusyMarketResearchService(failures=99)
    service = MarketStrategyPoolService(
        store=store,
        adapter=DuckDbMarketDataAdapter(store),
        individual_service=individual,
        pool_store=pool_store,
    )
    pauses: list[float] = []
    monkeypatch.setattr(pool_service.time, "sleep", pauses.append)

    result = service.run(
        {"mode": "full", "risk_trials": 3_000, "horizon": 5},
        job_id="retry-2",
        entry_writer=lambda item: pool_store.persist_worker_entry(item, source_job_id="retry-2"),
    )

    assert result["failed_symbols"] == 2
    assert result["completed_symbols"] == 0
    # 每只标的：首跑 + 2 次退避重跑 = 3 次尝试，退避 30 秒再 90 秒。
    assert sorted(pauses) == [30.0, 30.0, 90.0, 90.0]
    assert individual.calls == 6
    message = " ".join(item["error"] for item in result["errors"])
    assert "行情库被占用" in message
    assert "退避重跑 2 次" in message
    assert "增量更新策略池" in message


def test_estimate_reports_exactly_what_the_next_run_will_do(tmp_path) -> None:
    """预估必须和任务预扫描给出同一个数：用户是拿这个数字决定要不要点增量更新的。"""

    store, pool_store = _busy_market_setup(tmp_path)
    service = MarketStrategyPoolService(
        store=store,
        adapter=DuckDbMarketDataAdapter(store),
        individual_service=FakeIndividualResearchService(),
        pool_store=pool_store,
    )
    payload = {"mode": "full", "risk_trials": 3_000, "horizon": 5}

    def persist(item):  # noqa: ANN001
        pool_store.persist_worker_entry(item, source_job_id="first-run")

    first = service.run(payload, job_id="first-run", entry_writer=persist)
    estimate = service.estimate_incremental(payload)
    second = service.run(payload, job_id="second-run", entry_writer=persist)

    assert first["total_symbols"] == 2
    # 预估的「待重算」= 下一次任务真正排进队列的量；「可复用」= 被预扫描剔掉的量。
    assert estimate["pending_symbols"] == second["total_symbols"]
    assert estimate["universe_symbols"] == 2
    # 三个数得刚好摊开整个标的池：待重算 + 可复用 + 排期 = 全市场。
    assert (
        estimate["pending_symbols"] + estimate["reusable_symbols"] + estimate["deferred_symbols"]
        == estimate["universe_symbols"]
    )
    assert estimate["risk_trials"] == 3_000
    assert estimate["data_end"] is not None


def test_estimate_counts_every_symbol_when_rebuilding(tmp_path) -> None:
    """清空重建不复用任何存量，预估也得照同一个口径说「全部要跑」。"""

    store, pool_store = _busy_market_setup(tmp_path)
    service = MarketStrategyPoolService(
        store=store,
        adapter=DuckDbMarketDataAdapter(store),
        individual_service=FakeIndividualResearchService(),
        pool_store=pool_store,
    )
    service.run(
        {"mode": "full", "risk_trials": 3_000, "horizon": 5},
        job_id="third-run",
        entry_writer=lambda item: pool_store.persist_worker_entry(item, source_job_id="third-run"),
    )

    estimate = service.estimate_incremental(
        {"mode": "full", "rebuild": True, "risk_trials": 3_000, "horizon": 5}
    )

    assert estimate["rebuild"] is True
    assert estimate["pending_symbols"] == 2
    assert estimate["reusable_symbols"] == 0
