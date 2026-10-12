from __future__ import annotations

from datetime import date

import pytest

from aquest.strategy_discovery.replay import (
    build_period_windows,
    build_replay_candidate,
    calculate_period_metrics,
)


def test_period_windows_use_calendar_years_and_clamp_to_replay_range() -> None:
    windows = build_period_windows(date(2024, 1, 1), date(2026, 9, 8))

    by_key = {window.key: window for window in windows}
    assert by_key["ytd"].start_date == date(2026, 1, 1)
    assert by_key["one_year"].start_date == date(2025, 9, 8)
    assert by_key["two_year"].start_date == date(2024, 9, 8)
    assert by_key["year_2024"].start_date == date(2024, 1, 1)
    assert by_key["year_2024"].end_date == date(2024, 12, 31)
    assert by_key["year_2026"].end_date == date(2026, 9, 8)
    assert by_key["year_2026"].partial is True
    assert by_key["ytd"].partial is True


def test_period_windows_handle_february_29_without_invalid_dates() -> None:
    windows = build_period_windows(date(2024, 2, 29), date(2024, 3, 1))

    assert windows[1].start_date == date(2024, 2, 29)
    assert windows[2].start_date == date(2024, 2, 29)


def test_period_metrics_derive_return_drawdown_sharpe_and_trades_from_curve() -> None:
    equity = [
        {"date": "2024-01-01", "total": 100.0},
        {"date": "2024-06-30", "total": 120.0},
        {"date": "2024-12-31", "total": 110.0},
    ]
    trades = [
        {
            "date": "2024-06-30",
            "direction": "SELL",
            "pnl": 20.0,
            "cost_basis": 100.0,
            "rejected": False,
        }
    ]

    metrics = calculate_period_metrics(
        equity,
        trades,
        key="year_2024",
        label="2024",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
    )

    assert metrics.bars == 3
    assert metrics.total_return == pytest.approx(0.1)
    assert metrics.max_drawdown == pytest.approx(1 / 12)
    assert metrics.total_trades == 1
    assert metrics.sharpe is not None
    assert metrics.start_date == "2024-01-01"
    assert metrics.end_date == "2024-12-31"


def test_period_metrics_mark_short_windows_as_insufficient() -> None:
    metrics = calculate_period_metrics(
        [{"date": "2024-01-01", "total": 100.0}],
        [],
        key="ytd",
        label="今年以来",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
    )

    assert metrics.bars == 1
    assert metrics.total_return is None
    assert metrics.max_drawdown is None
    assert metrics.sharpe is None
    assert metrics.diagnostic == "样本不足：至少需要 2 个资金曲线点"


def test_replay_candidate_carries_full_metrics_and_turnover() -> None:
    candidate = build_replay_candidate(
        {
            "candidate_hash": "candidate-1",
            "human_readable_rules": "测试策略",
            "config": {"initial_cash": 1_000.0},
            "metrics": {"validation": {"annual_return": 0.1, "sharpe": 1.0}},
        },
        {
            "performance": {
                "total_return": 0.2,
                "annual_return": 0.1,
                "max_drawdown": 0.05,
                "sharpe": 1.2,
                "total_trades": 1.0,
                "win_rate": 1.0,
            },
            "equity_curve": [
                {"date": "2024-01-01", "total": 1_000.0},
                {"date": "2024-01-02", "total": 1_200.0},
            ],
            "trades": [
                {"date": "2024-01-01", "direction": "BUY", "size": 10, "price": 10},
                {"date": "2024-01-02", "direction": "SELL", "size": 10, "price": 12},
            ],
            "data_fingerprint": "fingerprint",
        },
        rank=1,
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 2),
    )

    assert candidate.overall_metrics["turnover"] == pytest.approx(0.22)
    assert candidate.backtest["performance"]["turnover"] == pytest.approx(0.22)
