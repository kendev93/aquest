from datetime import date, datetime, timezone

from aquest.market_data.dates import parse_iso_date, parse_store_date


def test_parse_iso_date_accepts_dates_timestamps_and_none() -> None:
    assert parse_iso_date("2026-10-01") == date(2026, 10, 1)
    assert parse_iso_date("2026-10-01T18:30:00+08:00") == date(2026, 10, 1)
    assert parse_iso_date(datetime(2026, 10, 1, 18, 30)) == date(2026, 10, 1)
    assert parse_iso_date(datetime(2026, 10, 1, tzinfo=timezone.utc)) == date(2026, 10, 1)
    assert parse_iso_date(date(2026, 10, 1)) == date(2026, 10, 1)
    assert parse_iso_date("") is None
    assert parse_iso_date(None) is None


def test_parse_store_date_accepts_compact_and_iso_forms() -> None:
    assert parse_store_date("20261001") == date(2026, 10, 1)
    assert parse_store_date("2026-10-01") == date(2026, 10, 1)
    assert parse_store_date(datetime(2026, 10, 1, 18, 30)) == date(2026, 10, 1)
    assert parse_store_date("not-a-date") is None
    assert parse_store_date(None) is None
