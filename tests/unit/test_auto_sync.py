from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from selector_app.market_data.auto_sync import AutomaticMarketSyncScheduler, auto_sync_due

SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_auto_sync_is_due_when_never_synced_or_last_sync_was_before_today() -> None:
    today = datetime(2026, 9, 1, 9, 0, tzinfo=SHANGHAI)

    assert auto_sync_due(None, now=today) is True
    assert auto_sync_due("2026-08-31T23:59:00+08:00", now=today) is True
    assert auto_sync_due("2026-09-01T08:00:00+08:00", now=today) is False


def test_auto_sync_treats_invalid_metadata_as_due() -> None:
    today = datetime(2026, 9, 1, 9, 0, tzinfo=SHANGHAI)

    assert auto_sync_due("not-a-timestamp", now=today) is True


def test_auto_sync_does_not_use_day_files_as_an_online_sync_source(tmp_path) -> None:
    day_file = tmp_path / "sh" / "lday" / "sh600000.day"
    day_file.parent.mkdir(parents=True)
    day_file.write_bytes(b"local data")

    class EmptyStore:
        def status(self):
            return SimpleNamespace(instrument_count=0, last_online_sync_at=None)

    class Runner:
        def has_active_jobs(self, descriptions):
            del descriptions
            return False

        def submit_callable(self, *args, **kwargs):
            raise AssertionError("没有 DuckDB 标的时，自动在线同步不应读取或导入 .day 文件")

    scheduler = AutomaticMarketSyncScheduler(
        store=EmptyStore(),
        runner=Runner(),
        market_sync=object(),
        precomputer=object(),
        vipdoc_path=tmp_path,
    )

    assert scheduler.submit_if_due() is None
