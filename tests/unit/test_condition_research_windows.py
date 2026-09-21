"""滚动复验的窗口聚合：什么算"优于基线"，什么算"没样本"。

两条最容易写错的口径：

1. 窗口内成交数不足时**必须从分子分母里一起剔掉**，不能记成"这个窗口没通过"
   —— 那会把"这几天没几笔成交"读成"这条条件在这个窗口失效了"。
2. 窗口数放不下时降级而不是报错，但降级这件事要写进 note，不能默默少几窗。
"""

from __future__ import annotations

from datetime import date

import pytest

from selector_app.condition_research.time_split import DateWindow
from selector_app.condition_research.windows import (
    build_baseline_window_payload,
    build_consistency,
    build_window_payload,
)

_WINDOW = DateWindow(date(2024, 1, 1), date(2024, 3, 31))


def _summary(trades: int, win_rate: float | None, improved_ratio: float | None = 0.6):
    return {
        "pooled_trades": {"closed_trades": trades, "win_rate": win_rate},
        "per_stock": {"improved_stock_ratio": improved_ratio},
    }


def _window(
    *,
    trades: int = 100,
    win_rate: float | None = 0.6,
    baseline_rate: float | None = 0.5,
    index: int = 1,
    window: DateWindow = _WINDOW,
    min_trades: int = 20,
) -> dict:
    return build_window_payload(
        index=index,
        window=window,
        summary=_summary(trades, win_rate),
        baseline_summary=_summary(trades, baseline_rate),
        min_trades=min_trades,
    )


def test_window_flags_improvement_against_the_same_window_baseline() -> None:
    better = _window(win_rate=0.6, baseline_rate=0.5)
    worse = _window(win_rate=0.45, baseline_rate=0.5)

    assert better["improved_win_rate"] is True
    assert worse["improved_win_rate"] is False


def test_window_without_a_baseline_is_not_claimed_as_improved() -> None:
    payload = _window(win_rate=0.6, baseline_rate=None)

    assert payload["improved_win_rate"] is None


def test_window_below_min_trades_is_marked_insufficient() -> None:
    payload = _window(trades=5, win_rate=0.9, min_trades=20)

    assert payload["insufficient_sample"] is True


def test_consistency_counts_only_usable_windows() -> None:
    """样本不足的窗口既不算通过也不算没通过，直接从分母里去掉。"""

    windows = [
        _window(index=1, trades=100, win_rate=0.6),
        _window(index=2, trades=2, win_rate=0.9, min_trades=20),  # 样本不足
        _window(index=3, trades=100, win_rate=0.45),
        _window(index=4, trades=100, win_rate=0.55),
    ]

    consistency = build_consistency(windows, requested=4, used=4, min_trades=20)

    assert consistency["evaluated"] == 3
    assert consistency["skipped"] == 1
    assert consistency["better_than_baseline"] == 2
    assert "未计入一致性统计" in (consistency["note"] or "")


def test_consistency_reports_median_and_worst_win_rate() -> None:
    windows = [
        _window(index=1, trades=100, win_rate=0.60),
        _window(index=2, trades=100, win_rate=0.50),
        _window(index=3, trades=100, win_rate=0.40),
    ]

    consistency = build_consistency(windows, requested=3, used=3, min_trades=20)

    assert consistency["median_win_rate"] == pytest.approx(0.50)
    assert consistency["worst_win_rate"] == pytest.approx(0.40)
    assert consistency["better_than_baseline"] == 1


def test_consistency_says_so_when_the_windows_were_downgraded() -> None:
    windows = [_window(index=1), _window(index=2)]

    consistency = build_consistency(windows, requested=5, used=2, min_trades=20)

    assert consistency["requested"] == 5
    assert consistency["used"] == 2
    assert "只够切 2 个窗口" in (consistency["note"] or "")


def test_consistency_with_no_usable_window_gives_none_instead_of_zero() -> None:
    windows = [_window(index=1, trades=1, min_trades=50)]

    consistency = build_consistency(windows, requested=1, used=1, min_trades=50)

    assert consistency["evaluated"] == 0
    assert consistency["better_than_baseline"] == 0
    assert consistency["median_win_rate"] is None
    assert consistency["worst_win_rate"] is None


def test_baseline_window_payload_carries_the_comparison_numbers() -> None:
    payload = build_baseline_window_payload(
        index=2,
        window=_WINDOW,
        baseline_summary=_summary(80, 0.48),
    )

    assert payload["index"] == 2
    assert payload["trades"] == 80
    assert payload["win_rate"] == pytest.approx(0.48)
    assert payload["calendar_days"] == 91
