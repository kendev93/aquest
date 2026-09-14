from __future__ import annotations

from datetime import date

import pytest

from selector_app.web.date_ranges import resolve_date_range


def test_recent_five_years_is_resolved_against_latest_completed_bar() -> None:
    assert resolve_date_range(
        mode="recent5",
        requested_start=None,
        requested_end=None,
        available_start="2010-01-04",
        available_end="2026-09-11",
    ) == (date(2021, 9, 11), date(2026, 9, 11))


def test_full_history_returns_the_available_store_bounds() -> None:
    assert resolve_date_range(
        mode="full",
        requested_start=None,
        requested_end=None,
        available_start="2010-01-04",
        available_end="2026-09-11",
    ) == (date(2010, 1, 4), date(2026, 9, 11))


def test_explicit_dates_take_precedence_for_legacy_clients() -> None:
    assert resolve_date_range(
        mode="recent5",
        requested_start=date(2024, 1, 1),
        requested_end=date(2024, 12, 31),
        available_start="2010-01-04",
        available_end="2026-09-11",
    ) == (date(2024, 1, 1), date(2024, 12, 31))


def test_custom_mode_requires_both_dates() -> None:
    with pytest.raises(ValueError, match="自定义日期范围必须同时设置开始日期和结束日期"):
        resolve_date_range(
            mode="custom",
            requested_start=date(2024, 1, 1),
            requested_end=None,
            available_start="2010-01-04",
            available_end="2026-09-11",
        )


def test_recent_mode_requires_available_completed_data() -> None:
    with pytest.raises(ValueError, match="行情数据库没有已完成日线"):
        resolve_date_range(
            mode="recent5",
            requested_start=None,
            requested_end=None,
            available_start=None,
            available_end=None,
        )
