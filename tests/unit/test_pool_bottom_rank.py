from __future__ import annotations

from datetime import datetime, timedelta

from aquest.strategy_pool.service import (
    BOTTOM_RECHECK_DAYS,
    MIN_BOTTOM_RANK_ENTRIES,
    MarketStrategyPoolService,
    _bottom_ranked_symbols,
)


def _entry(
    value: float, objective: str = "annual_return", updated_at: datetime | None = None
) -> dict[str, object]:
    entry: dict[str, object] = {
        "automatic_candidates": {
            objective: {
                "metrics": {
                    "full_period": {
                        "annual_return": value,
                        "sharpe": value,
                        "win_rate": value,
                        "payoff_ratio": value,
                    }
                }
            }
        }
    }
    if updated_at is not None:
        entry["updated_at"] = updated_at
    return entry


def test_bottom_rank_takes_the_worst_tail_of_each_objective() -> None:
    entries = {
        ("SH", f"{600000 + i}"): _entry(float(i + 1)) for i in range(MIN_BOTTOM_RANK_ENTRIES)
    }
    bottom = _bottom_ranked_symbols(entries, {}, 10)
    # 20 只取 10% = 2 只；四项完全同序，最差的两只就是数值最小的两只。
    assert bottom == {("SH", "600000"), ("SH", "600001")}


def test_bottom_rank_skips_incomplete_and_manual_candidates() -> None:
    entries = {
        ("SH", f"{600000 + i}"): _entry(float(i + 1)) for i in range(MIN_BOTTOM_RANK_ENTRIES)
    }
    # 缺一项指标：不参与排名，也不占后分位名额（否则它会以「最差」挤掉别人）。
    broken = _entry(0.0)
    broken["automatic_candidates"]["annual_return"]["metrics"]["full_period"].pop("payoff_ratio")
    entries[("SH", "600099")] = broken
    # 手工覆盖用 candidate_ref 指向自动候选：排名只看自动候选，手工决定不排队重研。
    entries[("SH", "600098")] = {
        "automatic_candidates": {"annual_return": {"candidate_ref": "annual_return"}}
    }
    bottom = _bottom_ranked_symbols(entries, {}, 10)
    assert ("SH", "600099") not in bottom
    assert ("SH", "600098") not in bottom
    assert len(bottom) == 2


def test_bottom_rank_needs_enough_entries() -> None:
    entries = {
        ("SH", f"{600000 + i}"): _entry(float(i)) for i in range(MIN_BOTTOM_RANK_ENTRIES - 1)
    }
    assert _bottom_ranked_symbols(entries, {}, 10) == set()


def test_bottom_rank_candidate_waits_for_the_cooldown() -> None:
    now = datetime(2026, 11, 18, 12, 0)
    entry = {
        "automatic_candidates": {
            "annual_return": {
                "metrics": {
                    "validation": {"total_trades": 10.0},
                    "full_period": {"closed_trades": 30.0, "total_return": 0.5},
                }
            }
        },
        "benchmark": {"full_period": {"total_return": 0.2}},
    }
    aged = ("mf", "cf", "ready", True, now - timedelta(days=BOTTOM_RECHECK_DAYS))
    assert MarketStrategyPoolService._research_recheck_due(
        entry,
        aged,
        {},
        now=now,
        symbol=("SH", "600000"),
        bottom_symbols={("SH", "600000")},
    )
    fresh = ("mf", "cf", "ready", True, now - timedelta(days=BOTTOM_RECHECK_DAYS - 1))
    assert not MarketStrategyPoolService._research_recheck_due(
        entry,
        fresh,
        {},
        now=now,
        symbol=("SH", "600000"),
        bottom_symbols={("SH", "600000")},
    )
    # 不在后分位里的条目不因为这次改动改变复用行为。
    assert not MarketStrategyPoolService._research_recheck_due(
        entry,
        aged,
        {},
        now=now,
        symbol=("SH", "600001"),
        bottom_symbols={("SH", "600000")},
    )


def test_bottom_rank_prefers_fresh_tracking_metrics() -> None:
    """排名优先用每天刷新的跟踪口径：研究时好看、现在垫底的策略要进后分位。"""

    entries = {
        ("SH", f"{600000 + i}"): _entry(float(i + 1), updated_at=datetime(2026, 11, 10))
        for i in range(MIN_BOTTOM_RANK_ENTRIES)
    }
    summaries = {
        ("SH", "600019", "annual_return"): {
            "full_period": {
                "as_of_date": "2026-11-17",
                "annual_return": -10.0,
                "sharpe": -10.0,
                "win_rate": -10.0,
                "payoff_ratio": -10.0,
            }
        }
    }
    bottom = _bottom_ranked_symbols(entries, summaries, 10)
    # 600019 冻结口径最好，但最新回放垫底 → 必须进后分位；600000 本来就最差，仍在。
    assert ("SH", "600019") in bottom
    assert ("SH", "600000") in bottom
    assert len(bottom) == 2


def test_bottom_rank_ignores_summary_older_than_research() -> None:
    """摘要没覆盖到研究日（数据被清过/短窗口夹具）时回退冻结指标，不误判。"""

    entries = {
        ("SH", f"{600000 + i}"): _entry(float(i + 1), updated_at=datetime(2026, 11, 10))
        for i in range(MIN_BOTTOM_RANK_ENTRIES)
    }
    summaries = {
        ("SH", "600019", "annual_return"): {
            "full_period": {
                "as_of_date": "2026-10-01",
                "annual_return": -10.0,
                "sharpe": -10.0,
                "win_rate": -10.0,
                "payoff_ratio": -10.0,
            }
        }
    }
    bottom = _bottom_ranked_symbols(entries, summaries, 10)
    assert ("SH", "600019") not in bottom
    assert bottom == {("SH", "600000"), ("SH", "600001")}
