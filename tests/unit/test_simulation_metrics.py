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
    positions = [{"size": 10, "mark": 12.0, "cost": 100.0}]

    result = detail_metrics(
        equity=equity,
        initial_cash=100.0,
        trades=trades,
        positions=positions,
        benchmark=benchmark,
        holdings=holdings,
    )

    trading = result["trading"]
    assert trading["closed"] == 2
    assert trading["win_rate"] == pytest.approx(0.5)
    assert trading["payoff_ratio"] == pytest.approx(2.0)
    assert trading["realized_pnl"] == pytest.approx(5.0)
    assert trading["unrealized_pnl"] == pytest.approx(20.0)
    assert trading["avg_rank"] == pytest.approx(1.0)

    risk = result["risk"]
    assert risk["max_drawdown"] == pytest.approx(1 - 105 / 110)
    assert risk["current_drawdown"] == pytest.approx(1 - 105 / 110)
    assert risk["max_drawdown_days"] == 1
    assert risk["sample_days"] == 3
    assert risk["volatility"] is None and risk["sharpe"] is None

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
        trades=[],
        positions=[],
        benchmark=None,
        holdings=None,
    )
    assert enough["risk"]["volatility"] is not None
    assert enough["risk"]["sharpe"] is not None
