from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from selector_app.market_data.models import StockRef
from selector_app.strategy_pool.selection import (
    common_calendar_windows,
    slices_for_common_windows,
    summarize_objective_winners,
)
from selector_app.strategy_pool.service import (
    MARKET_POOL_ALGORITHM_VERSION,
    MarketStrategyPoolService,
    _fingerprint,
    _portfolio_strategies,
)
from selector_app.swing_research.combinations import split_trading_days


def test_common_calendar_windows_use_shared_dates_and_purge_each_label_boundary() -> None:
    dates = [date(2020, 1, 1) + timedelta(days=day) for day in range(100)]

    windows = common_calendar_windows(dates, purge_days=5)

    assert windows["train"] == (dates[0], dates[54])
    assert windows["validation"] == (dates[60], dates[74])
    assert windows["holdout"] == (dates[80], dates[94])


def test_per_stock_calendar_split_supports_stocks_listed_in_2021() -> None:
    dates = pd.date_range("2021-01-04", periods=1_300, freq="B").to_numpy()

    slices, metadata = split_trading_days(dates, horizon=10)

    assert slices["train"].stop > slices["train"].start
    assert slices["validation"].stop > slices["validation"].start
    assert slices["holdout"].stop > slices["holdout"].start
    assert metadata["train"]["end"] < metadata["validation"]["start"]
    assert metadata["validation"]["end"] < metadata["holdout"]["start"]
    assert metadata["train"]["purged_days"] == 10
    assert metadata["validation"]["purged_days"] == 10
    assert metadata["holdout"]["purged_days"] == 10


def test_objective_winners_rank_only_validation_metrics_and_keep_risk_warnings() -> None:
    result = {
        "data_fingerprint": "bars-v1",
        "results_by_sharpe": [
            _candidate(
                "a",
                annual_return=0.2,
                sharpe=1.5,
                holdout_sharpe=99,
                validation_trades=12,
                holdout_trades=6,
            ),
            _candidate(
                "b",
                annual_return=0.5,
                sharpe=1.1,
                holdout_sharpe=0,
                validation_trades=12,
                holdout_trades=6,
            ),
        ],
        "results_by_return": [
            _candidate(
                "b",
                annual_return=0.5,
                sharpe=1.1,
                holdout_sharpe=0,
                validation_trades=12,
                holdout_trades=6,
            ),
            _candidate(
                "a",
                annual_return=0.2,
                sharpe=1.5,
                holdout_sharpe=99,
                validation_trades=12,
                holdout_trades=6,
            ),
        ],
    }

    winners = summarize_objective_winners(result)

    assert winners["annual_return"]["candidate_id"] == "b"
    assert winners["sharpe"]["candidate_id"] == "a"
    assert winners["sharpe"]["metrics"]["holdout"]["sharpe"] == 99
    assert winners["sharpe"]["quality"]["badges"] == ["样本较充分", "风险高"]


def test_objective_winners_prefer_adequate_sample_over_a_thin_high_score() -> None:
    board = [
        # 一两笔交易就能把年化 / 夏普顶到天上，但那不是可以拿去实盘的证据。
        _candidate("thin", annual_return=0.9, sharpe=9.9, validation_trades=4, holdout_trades=2),
        _candidate("solid", annual_return=0.3, sharpe=1.2, validation_trades=12, holdout_trades=6),
    ]
    result = {"data_fingerprint": "bars-v1", "results_by_sharpe": board, "results_by_return": board}

    winners = summarize_objective_winners(result)

    assert winners["sharpe"]["candidate_id"] == "solid"
    assert winners["annual_return"]["candidate_id"] == "solid"
    assert winners["sharpe"]["quality"]["sample_adequate"] is True


def test_objective_winners_exclude_candidates_holding_past_the_cap() -> None:
    """平均持仓超上限的候选直接出局，分数再高也不行。

    20 个交易日的上限按年化常数 252/365 折算 ≈ 27.6 个自然日：验证段平均
    40 个自然日的候选超限，20 个自然日的在限内。
    """

    board = [
        _candidate(
            "long", annual_return=0.9, sharpe=9.9, validation_trades=12, avg_holding_days=40
        ),
        _candidate(
            "short", annual_return=0.3, sharpe=1.2, validation_trades=12, avg_holding_days=20
        ),
    ]
    result = {"data_fingerprint": "bars-v1", "results_by_sharpe": board, "results_by_return": board}

    winners = summarize_objective_winners(result)

    assert winners["sharpe"]["candidate_id"] == "short"
    assert winners["annual_return"]["candidate_id"] == "short"


def test_objective_winners_hold_cap_is_hard_and_can_be_disabled() -> None:
    """门槛没有"全不合格就退回"的豁免：全超限就出空，否则长持仓策略照样进池。"""

    board = [
        _candidate(
            "long", annual_return=0.9, sharpe=9.9, validation_trades=12, avg_holding_days=40
        ),
    ]
    result = {"data_fingerprint": "bars-v1", "results_by_sharpe": board, "results_by_return": board}

    relaxed = summarize_objective_winners(result, max_hold_days=None)
    assert relaxed["sharpe"]["candidate_id"] == "long"

    tightened = summarize_objective_winners(result, max_hold_days=10)
    assert tightened["sharpe"] is None
    assert tightened["annual_return"] is None


def test_objective_winners_do_not_reject_candidates_without_holding_metric() -> None:
    """门槛拦"明知持仓过长"，不拦"缺数据"：旧候选没有该指标时照常参选。"""

    board = [_candidate("legacy", annual_return=0.5, sharpe=1.5, validation_trades=12)]
    result = {"data_fingerprint": "bars-v1", "results_by_sharpe": board, "results_by_return": board}

    winners = summarize_objective_winners(result)

    assert winners["sharpe"]["candidate_id"] == "legacy"


def test_objective_winners_return_empty_when_every_candidate_is_thin() -> None:
    """样本门槛没有退回：全都不足就出空。

    薄样本上的分数是噪声——一两笔交易的胜率和夏普跟掷硬币没有区别，
    把它选进池子等于给噪声背书。空槽位（该股暂无值得信的策略）才是诚实输出。
    """

    board = [
        _candidate("low", annual_return=0.1, sharpe=0.5, validation_trades=1, holdout_trades=1),
        _candidate("high", annual_return=0.4, sharpe=1.4, validation_trades=2, holdout_trades=1),
    ]
    result = {"data_fingerprint": "bars-v1", "results_by_sharpe": board, "results_by_return": []}

    winners = summarize_objective_winners(result)

    assert winners["sharpe"] is None
    assert winners["annual_return"] is None


def test_objective_eligibility_does_not_change_when_holdout_trade_count_changes() -> None:
    board = [
        _candidate("a", annual_return=0.5, sharpe=1.0, validation_trades=10, holdout_trades=2),
        _candidate("b", annual_return=0.2, sharpe=1.0, validation_trades=10, holdout_trades=3),
    ]
    before = summarize_objective_winners({"results_by_return": board, "results_by_sharpe": []})[
        "annual_return"
    ]
    board[0]["metrics"]["holdout"]["closed_trades"] = 3
    after = summarize_objective_winners({"results_by_return": board, "results_by_sharpe": []})[
        "annual_return"
    ]

    assert before["candidate_id"] == "a"
    assert after["candidate_id"] == "a"
    # Holdout remains visible in the descriptive badge even though it cannot alter eligibility.
    assert before["quality"]["sample_adequate"] is False
    assert after["quality"]["sample_adequate"] is True


def test_objective_without_finite_validation_metric_has_no_strategy() -> None:
    result = {
        "results_by_sharpe": [
            _candidate("bad", annual_return=None, sharpe=float("nan"), holdout_sharpe=1.0)
        ],
        "results_by_return": [],
    }

    winners = summarize_objective_winners(result)

    assert winners["annual_return"] is None
    assert winners["sharpe"] is None


def test_shared_split_windows_project_onto_later_listing_without_local_resplitting() -> None:
    calendar = [date(2020, 1, 1) + timedelta(days=day) for day in range(100)]
    windows = common_calendar_windows(calendar, purge_days=5)
    listing_dates = calendar[40:]

    slices = slices_for_common_windows(listing_dates, windows)

    assert listing_dates[slices["train"]][0] == calendar[40]
    assert listing_dates[slices["train"]][-1] == calendar[54]
    assert listing_dates[slices["validation"]][0] == calendar[60]
    assert listing_dates[slices["holdout"]][0] == calendar[80]


def test_stock_window_algorithm_version_invalidates_old_no_strategy_checkpoints() -> None:
    market_fingerprint = "bars-v1"
    previous_config = _fingerprint({"algorithm_version": "market-pool-v3"})
    updated_config = _fingerprint({"algorithm_version": MARKET_POOL_ALGORITHM_VERSION})

    assert MARKET_POOL_ALGORITHM_VERSION == "market-pool-v5"
    assert updated_config != previous_config
    assert not MarketStrategyPoolService._can_reuse(
        (market_fingerprint, previous_config, "no_strategy"),
        market_fingerprint,
        updated_config,
    )


def test_portfolio_strategy_selection_uses_objective_ties_drawdown_then_symbol() -> None:
    refs = [
        StockRef(market="SH", code="600000", path=Path("/tmp/600000.day")),
        StockRef(market="SH", code="600001", path=Path("/tmp/600001.day")),
    ]
    entries = [
        _pool_entry("600000", annual=0.3, sharpe=1.0, drawdown=-0.2),
        _pool_entry("600001", annual=0.2, sharpe=1.8, drawdown=-0.1),
    ]

    annual, annual_priority = _portfolio_strategies(entries, refs, "annual_return")
    sharpe, sharpe_priority = _portfolio_strategies(entries, refs, "sharpe")

    assert annual_priority["SH600000"] > annual_priority["SH600001"]
    assert sharpe_priority["SH600001"] > sharpe_priority["SH600000"]
    assert annual["SH600000"]["stop_loss_pct"] == 0.08
    assert sharpe["SH600001"]["sell_rule"].signals == ("technical.macd",)


def _candidate(
    candidate_id: str,
    *,
    annual_return: float | None,
    sharpe: float,
    holdout_sharpe: float = 1.0,
    validation_trades: int = 2,
    holdout_trades: int = 1,
    avg_holding_days: float | None = None,
) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "candidate_hash": candidate_id,
        "buy_rule": {"signals": [f"buy.{candidate_id}"]},
        "sell_rule": {"signals": [f"sell.{candidate_id}"]},
        "config": {"stop_loss_pct": 0.08, "take_profit_pct": None},
        "metrics": {
            "validation": {
                "annual_return": annual_return,
                "sharpe": sharpe,
                "win_rate": 0.6,
                "payoff_ratio": 1.8,
                "max_drawdown": -0.35 if candidate_id == "a" else -0.1,
                "closed_trades": validation_trades,
                "avg_holding_days": avg_holding_days,
            },
            "holdout": {"sharpe": holdout_sharpe, "closed_trades": holdout_trades},
        },
    }


def _pool_entry(code: str, *, annual: float, sharpe: float, drawdown: float) -> dict[str, object]:
    candidate = {
        "candidate_id": code,
        "buy_rule": {
            "conditions": [
                {"left_value": "technical.rsi14", "operator": "lt", "right_constant": 30}
            ]
        },
        "sell_rule": {"signals": ["technical.macd"]},
        "config": {"stop_loss_pct": 0.08},
        "metrics": {
            "validation": {
                "annual_return": annual,
                "sharpe": sharpe,
                "max_drawdown": drawdown,
            }
        },
    }
    return {
        "market": "SH",
        "code": code,
        "automatic_candidates": {"annual_return": candidate, "sharpe": candidate},
        "manual_overrides": {"annual_return": None, "sharpe": None},
    }
