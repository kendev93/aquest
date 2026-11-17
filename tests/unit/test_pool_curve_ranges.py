from __future__ import annotations

from datetime import date

import pytest

from aquest.web.routers.strategy_pool import _exit_reasons, _range_start, _window_stats


def test_range_start_covers_calendar_windows() -> None:
    end = date(2026, 11, 17)
    assert _range_start("all", end, "2026-01-01") == ""
    assert _range_start("created", end, "2026-01-01") == "2026-01-01"
    assert _range_start("ytd", end, "2026-01-01") == "2026-01-01"
    assert _range_start("1y", end, "2026-01-01") == "2025-11-17"
    assert _range_start("2y", end, "2026-01-01") == "2024-11-17"
    assert _range_start("3y", end, "2026-01-01") == "2023-11-17"


def test_range_start_clamps_leap_day() -> None:
    # 2024-02-29 往前推一年没有 2 月 29 日：取 2 月 28 日，别让快捷区间整只失效。
    assert _range_start("1y", date(2024, 2, 29), "") == "2023-02-28"


def test_window_stats_uses_account_equity() -> None:
    points = [
        {"date": "2026-01-01", "total": 100.0},
        {"date": "2026-01-02", "total": 120.0},
        {"date": "2026-01-03", "total": 90.0},
        {"date": "2026-01-04", "total": 110.0},
    ]
    total_return, drawdown = _window_stats(points)
    assert total_return == pytest.approx(0.10)
    # 峰值 120 回落到 90 就是 -25%，窗口内最深的那一段。
    assert drawdown == pytest.approx(-0.25)


def test_window_stats_needs_two_points() -> None:
    assert _window_stats([]) == (None, None)
    assert _window_stats([{"total": 100.0}]) == (None, None)


def test_exit_reasons_buckets_sells_and_sums_net_pnl() -> None:
    trades = [
        {"direction": "BUY", "pnl": 0.0},
        {"direction": "SELL", "reason": "跌破均线", "pnl": 120.0},
        {"direction": "SELL", "reason": "止损", "risk_trigger": "stop_loss", "pnl": -80.0},
        {"direction": "SELL", "reason": "止盈", "risk_trigger": "take_profit", "pnl": 50.0},
        {"direction": "SELL", "reason": "持有到期", "pnl": -20.0},
        # 被拒的卖单不是成交：不能计入出场分布。
        {
            "direction": "SELL",
            "reason": "止损",
            "risk_trigger": "stop_loss",
            "pnl": -999.0,
            "rejected": True,
        },
    ]
    rows = _exit_reasons(trades)
    assert [row["key"] for row in rows] == ["rule", "stop_loss", "take_profit", "max_hold"]
    by_key = {row["key"]: row for row in rows}
    assert by_key["rule"] == {"key": "rule", "label": "规则卖出", "trades": 1, "pnl": 120.0}
    assert by_key["stop_loss"]["pnl"] == -80.0
    assert by_key["max_hold"]["trades"] == 1
