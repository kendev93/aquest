from __future__ import annotations

from datetime import date

from selector_app.condition_research.time_split import split_calendar_range


def test_calendar_split_is_ordered_non_overlapping_and_covers_the_full_range() -> None:
    splits = split_calendar_range(date(2020, 1, 1), date(2024, 12, 31))

    assert splits.train.start_date == date(2020, 1, 1)
    assert splits.validation.start_date == date(2023, 1, 1)
    assert splits.holdout.end_date == date(2024, 12, 31)
    assert splits.train.end_date < splits.validation.start_date
    assert splits.validation.end_date < splits.holdout.start_date


def test_split_rejects_ranges_too_short_for_three_distinct_periods() -> None:
    try:
        split_calendar_range(date(2025, 1, 1), date(2025, 1, 2))
    except ValueError as exc:
        assert "至少需要" in str(exc)
    else:
        raise AssertionError("short ranges must be rejected")
