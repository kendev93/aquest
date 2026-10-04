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


def test_full_history_stops_at_the_retained_history_start() -> None:
    """「全历史」是库里全部，但不得早于保留起点。

    1990-2014 那段候选池小到不能代表市场（1990 年全市场只有 4 只股票），摆出来只会
    让人误选。库里真的清干净到 2015 起时，这个下限是空操作；它防的是"将来又有路径
    把旧数据灌回来"。
    """

    assert resolve_date_range(
        mode="full",
        requested_start=None,
        requested_end=None,
        available_start="2010-01-04",
        available_end="2026-09-11",
    ) == (date(2015, 1, 1), date(2026, 9, 11))
    # 数据本来就干净时不改变任何东西
    assert resolve_date_range(
        mode="full",
        requested_start=None,
        requested_end=None,
        available_start="2016-03-01",
        available_end="2026-09-11",
    ) == (date(2016, 3, 1), date(2026, 9, 11))


def test_full_history_rejects_a_store_that_starts_before_the_cutoff() -> None:
    with pytest.raises(ValueError, match="没有可用样本"):
        resolve_date_range(
            mode="full",
            requested_start=None,
            requested_end=None,
            available_start="2010-01-04",
            available_end="2014-06-30",
        )


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
