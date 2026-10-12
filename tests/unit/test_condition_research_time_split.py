from __future__ import annotations

from datetime import date, timedelta

from aquest.condition_research.time_split import (
    DateWindow,
    resolve_window_count,
    split_calendar_range,
    split_windows,
)


def test_calendar_split_is_ordered_non_overlapping_and_covers_the_full_range() -> None:
    splits = split_calendar_range(date(2020, 1, 1), date(2024, 12, 31))

    assert splits.train.start_date == date(2020, 1, 1)
    assert splits.validation.start_date == date(2023, 1, 1)
    assert splits.holdout.end_date == date(2024, 12, 31)
    assert splits.train.end_date < splits.validation.start_date
    assert splits.validation.end_date < splits.holdout.start_date


def test_calendar_split_uses_actual_trading_days_when_available() -> None:
    sessions = [date(2023, 12, day) for day in (25, 26, 27, 28, 29)] + [
        date(2024, 1, day) for day in (2, 3, 4, 5, 8, 9)
    ]

    splits = split_calendar_range(
        date(2023, 12, 25),
        date(2024, 1, 9),
        train_ratio=0.6,
        validation_ratio=0.2,
        trading_dates=sessions,
    )

    assert splits.train.end_date == date(2024, 1, 2)
    assert splits.validation.start_date == date(2024, 1, 3)
    assert splits.validation.end_date == date(2024, 1, 4)
    assert splits.holdout.start_date == date(2024, 1, 5)


def test_split_rejects_ranges_too_short_for_three_distinct_periods() -> None:
    try:
        split_calendar_range(date(2025, 1, 1), date(2025, 1, 2))
    except ValueError as exc:
        assert "至少需要" in str(exc)
    else:
        raise AssertionError("short ranges must be rejected")


def test_windows_are_contiguous_and_cover_the_range_exactly() -> None:
    windows = split_windows(date(2024, 1, 1), date(2024, 12, 31), 4)

    assert len(windows) == 4
    assert windows[0].start_date == date(2024, 1, 1)
    assert windows[-1].end_date == date(2024, 12, 31)
    for previous, current in zip(windows, windows[1:]):
        # 首尾相接：中间不留空档，也不重叠一天。
        assert current.start_date == previous.end_date + timedelta(days=1)


def test_window_lengths_differ_by_at_most_one_day() -> None:
    windows = split_windows(date(2024, 1, 1), date(2024, 12, 31), 5)

    lengths = [(item.end_date - item.start_date).days + 1 for item in windows]

    assert max(lengths) - min(lengths) <= 1
    assert sum(lengths) == 366


def test_window_count_is_downgraded_when_the_range_cannot_hold_them() -> None:
    """宁可少几窗，也不让每窗短到没有意义。"""

    assert resolve_window_count(date(2024, 1, 1), date(2024, 3, 31), 5) == 3  # 91 天 / 30
    assert resolve_window_count(date(2024, 1, 1), date(2024, 1, 20), 5) == 1
    assert resolve_window_count(date(2024, 1, 1), date(2024, 12, 31), 1) == 1

    windows = split_windows(date(2024, 1, 1), date(2024, 3, 31), 5)

    assert len(windows) == 3
    assert all((item.end_date - item.start_date).days + 1 >= 30 for item in windows)


def test_single_window_is_the_whole_range() -> None:
    assert split_windows(date(2024, 1, 1), date(2024, 12, 31), 1) == (
        DateWindow(date(2024, 1, 1), date(2024, 12, 31)),
    )


def test_windows_reject_an_inverted_range() -> None:
    try:
        split_windows(date(2024, 12, 31), date(2024, 1, 1), 2)
    except ValueError as exc:
        assert "不能晚于" in str(exc)
    else:
        raise AssertionError("inverted ranges must be rejected")
