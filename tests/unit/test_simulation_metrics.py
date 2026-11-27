from __future__ import annotations

from datetime import date, timedelta

import pytest

from aquest.strategy_pool.simulation_metrics import (
    day_return,
    detail_metrics,
    series_return,
)


def test_day_and_series_returns_use_the_first_net_value_as_baseline():
    assert day_return([{"date": "2026-11-27", "value": 107.47}], 100.0) == pytest.approx(0.0747)
    assert day_return(
        [
            {"date": "2026-11-26", "value": 110.0},
            {"date": "2026-11-27", "value": 105.0},
        ],
        100.0,
    ) == pytest.approx(105 / 110 - 1)
    assert day_return([], 100.0) is None

    assert series_return(
        {"dates": ["2026-11-23", "2026-11-27"], "totals": [4.0, 4.2]}
    ) == pytest.approx(0.05)
    assert series_return(None) is None
    assert series_return({"dates": [], "totals": [1.0]}) is None


def test_detail_metrics_cover_trading_risk_ranges_and_stages():
    equity = [
        {"date": "2026-11-24", "value": 100.0},
        {"date": "2026-11-25", "value": 110.0},
        {"date": "2026-11-26", "value": 105.0},
    ]
    benchmark = {
        "dates": ["2026-11-23", "2026-11-24", "2026-11-25", "2026-11-26"],
        "totals": [4.0, 4.1, 4.2, 4.3],
    }
    holdings = {
        "dates": ["2026-11-23", "2026-11-24", "2026-11-25", "2026-11-26"],
        "totals": [1.0, 1.02, 1.03, 1.04],
    }
    trades = [
        {"direction": "BUY", "rank": 1},
        {"direction": "BUY"},
        {"direction": "SELL", "pnl": 10.0},
        {"direction": "SELL", "pnl": -5.0},
    ]
    positions = [{"size": 5, "mark": 12.0, "cost": 50.0}]

    result = detail_metrics(
        equity=equity,
        initial_cash=100.0,
        cash=50.0,
        trades=trades,
        positions=positions,
        benchmark=benchmark,
        holdings=holdings,
    )

    trading = result["trading"]
    assert trading["closed"] == 2
    assert trading["win_rate"] == pytest.approx(0.5)
    assert trading["payoff_ratio"] == pytest.approx(2.0)
    assert trading["profit_factor"] == pytest.approx(2.0)
    assert trading["avg_win"] == pytest.approx(10.0)
    assert trading["avg_loss"] == pytest.approx(5.0)
    assert trading["best_trade"] == pytest.approx(10.0)
    assert trading["worst_trade"] == pytest.approx(-5.0)
    assert trading["expectancy"] == pytest.approx(2.5)
    assert trading["realized_pnl"] == pytest.approx(5.0)
    assert trading["unrealized_pnl"] == pytest.approx(10.0)
    assert trading["avg_rank"] == pytest.approx(1.0)

    risk = result["risk"]
    assert risk["max_drawdown"] == pytest.approx(1 - 105 / 110)
    assert risk["current_drawdown"] == pytest.approx(1 - 105 / 110)
    assert risk["max_drawdown_days"] == 1
    assert risk["sample_days"] == 3
    assert risk["volatility"] is None and risk["sharpe"] is None
    assert risk["avg_drawdown"] == pytest.approx((1 - 105 / 110) / 3)
    assert risk["underwater_ratio"] == pytest.approx(1 / 3)
    assert risk["max_daily_gain"] == pytest.approx(0.1)
    assert risk["max_daily_loss"] == pytest.approx(105 / 110 - 1)
    assert risk["daily_win_rate"] == pytest.approx(1 / 3)
    assert risk["max_up_streak"] == 1 and risk["max_down_streak"] == 1

    relative = result["relative"]
    assert relative["days"] == 3
    assert relative["beta"] is not None
    assert relative["correlation"] is not None
    assert relative["excess_max_drawdown"] is not None
    assert relative["tracking_error"] is None
    assert relative["information_ratio"] is None

    concentration = result["concentration"]
    assert concentration["max_weight"] == pytest.approx(60 / 105)
    assert concentration["top3_weight"] == pytest.approx(60 / 105)
    assert concentration["hhi"] == pytest.approx((60 / 105) ** 2)
    assert concentration["cash_ratio"] == pytest.approx(50 / 105)

    ranges = {row["key"]: row for row in result["ranges"]}
    near_month = ranges["1m"]
    assert near_month["start"] == "2026-11-24"
    assert near_month["strategy"] == pytest.approx(0.05)
    assert near_month["benchmark"] == pytest.approx(4.3 / 4.1 - 1)
    assert near_month["holdings"] == pytest.approx(1.04 / 1.02 - 1)
    assert near_month["excess_benchmark"] == pytest.approx(0.05 - (4.3 / 4.1 - 1))
    assert near_month["excess_holdings"] == pytest.approx(0.05 - (1.04 / 1.02 - 1))

    stages = {row["key"]: row for row in result["stages"]}
    assert stages["5d"]["days"] == 2
    assert stages["5d"]["start"] == "2026-11-24"
    assert stages["5d"]["strategy"] == pytest.approx(0.05)


def test_annualised_risk_metrics_require_twenty_samples():
    def curve(count: int) -> list[dict[str, object]]:
        return [
            {
                "date": (date(2026, 1, 1) + timedelta(days=index)).isoformat(),
                "value": 100.0 + index,
            }
            for index in range(count)
        ]

    short = detail_metrics(
        equity=curve(19),
        initial_cash=100.0,
        cash=0.0,
        trades=[],
        positions=[],
        benchmark=None,
        holdings=None,
    )
    assert short["risk"]["volatility"] is None
    assert short["risk"]["sharpe"] is None

    enough = detail_metrics(
        equity=curve(20),
        initial_cash=100.0,
        cash=0.0,
        trades=[],
        positions=[],
        benchmark=None,
        holdings=None,
    )
    assert enough["risk"]["volatility"] is not None
    assert enough["risk"]["sharpe"] is not None
