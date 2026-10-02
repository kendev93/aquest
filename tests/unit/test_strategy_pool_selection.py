from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from selector_app.market_data.models import StockRef
from selector_app.strategy_pool.selection import (
    common_calendar_windows,
    slices_for_common_windows,
    summarize_objective_winners,
)
from selector_app.strategy_pool.service import _portfolio_strategies


def test_common_calendar_windows_use_shared_dates_and_purge_each_label_boundary() -> None:
    dates = [date(2020, 1, 1) + timedelta(days=day) for day in range(100)]

    windows = common_calendar_windows(dates, purge_days=5)

    assert windows["train"] == (dates[0], dates[54])
    assert windows["validation"] == (dates[60], dates[74])
    assert windows["holdout"] == (dates[80], dates[94])


def test_objective_winners_rank_only_validation_metrics_and_keep_risk_warnings() -> None:
    result = {
        "data_fingerprint": "bars-v1",
        "results_by_sharpe": [
            _candidate("a", annual_return=0.2, sharpe=1.5, holdout_sharpe=99),
            _candidate("b", annual_return=0.5, sharpe=1.1, holdout_sharpe=0),
        ],
        "results_by_return": [
            _candidate("b", annual_return=0.5, sharpe=1.1, holdout_sharpe=0),
            _candidate("a", annual_return=0.2, sharpe=1.5, holdout_sharpe=99),
        ],
    }

    winners = summarize_objective_winners(result)

    assert winners["annual_return"]["candidate_id"] == "b"
    assert winners["sharpe"]["candidate_id"] == "a"
    assert winners["sharpe"]["metrics"]["holdout"]["sharpe"] == 99
    assert winners["sharpe"]["quality"]["badges"] == ["样本不足", "风险高"]


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
    holdout_sharpe: float,
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
                "closed_trades": 2,
            },
            "holdout": {"sharpe": holdout_sharpe, "closed_trades": 1},
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
