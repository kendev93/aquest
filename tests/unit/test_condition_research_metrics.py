from __future__ import annotations

import pytest

from selector_app.condition_research.metrics import summarize_backtests


def report(market: str, code: str, trades: list[dict[str, object]], **metrics: float) -> dict:
    return {
        "market": market,
        "code": code,
        "name": code,
        "trades": trades,
        "performance": {
            "total_return": metrics.get("total_return", 0.0),
            "annual_return": metrics.get("annual_return", 0.0),
            "max_drawdown": metrics.get("max_drawdown", 0.0),
            "sharpe": metrics.get("sharpe", 0.0),
        },
    }


def sell(pnl: float, cost_basis: float) -> dict[str, object]:
    return {
        "direction": "SELL",
        "pnl": pnl,
        "cost_basis": cost_basis,
        "rejected": False,
    }


def test_summary_pools_closed_trades_without_building_a_portfolio_curve() -> None:
    reports = [
        report("SH", "600000", [sell(100.0, 1_000.0), sell(-100.0, 1_000.0)]),
        report("SZ", "000001", [sell(200.0, 2_000.0)]),
    ]

    summary = summarize_backtests(reports)

    pooled = summary["pooled_trades"]
    assert pooled["closed_trades"] == 3
    assert pooled["win_rate"] == pytest.approx(2 / 3)
    assert pooled["payoff_ratio"] == pytest.approx(1.0)
    assert pooled["profit_factor"] == pytest.approx(3.0)
    assert pooled["mean_trade_return"] == pytest.approx(1 / 30)
    assert "total_return" not in summary
    assert "sharpe" not in summary


def test_summary_reports_stock_equal_statistics_and_candidate_improvement() -> None:
    baseline = [
        report("SH", "600000", [sell(100.0, 1_000.0), sell(-100.0, 1_000.0)]),
        report("SZ", "000001", [sell(50.0, 1_000.0), sell(-50.0, 1_000.0)]),
    ]
    candidate = [
        report("SH", "600000", [sell(100.0, 1_000.0), sell(-100.0, 1_000.0)]),
        report("SZ", "000001", [sell(50.0, 1_000.0)]),
    ]

    summary = summarize_backtests(candidate, baseline_reports=baseline)

    assert summary["pooled_trades"]["win_rate"] == pytest.approx(2 / 3)
    assert summary["per_stock"]["median_win_rate"] == pytest.approx(0.75)
    assert summary["per_stock"]["improved_stock_count"] == 1
    assert summary["per_stock"]["improved_stock_ratio"] == pytest.approx(0.5)


def test_summary_handles_stocks_without_closed_trades() -> None:
    summary = summarize_backtests([report("SH", "600000", [])])

    assert summary["pooled_trades"]["closed_trades"] == 0
    assert summary["pooled_trades"]["win_rate"] is None
    assert summary["per_stock"]["median_win_rate"] is None
