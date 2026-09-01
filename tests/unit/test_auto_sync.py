from datetime import datetime
from zoneinfo import ZoneInfo

from selector_app.market_data.auto_sync import auto_sync_due

SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_auto_sync_is_due_when_never_synced_or_last_sync_was_before_today() -> None:
    today = datetime(2026, 9, 1, 9, 0, tzinfo=SHANGHAI)

    assert auto_sync_due(None, now=today) is True
    assert auto_sync_due("2026-08-31T23:59:00+08:00", now=today) is True
    assert auto_sync_due("2026-09-01T08:00:00+08:00", now=today) is False


def test_auto_sync_treats_invalid_metadata_as_due() -> None:
    today = datetime(2026, 9, 1, 9, 0, tzinfo=SHANGHAI)

    assert auto_sync_due("not-a-timestamp", now=today) is True
