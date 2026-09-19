from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from selector_app.indicators.precompute import PRECOMPUTE_MAX_BARS
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


def test_auto_sync_submits_one_online_refresh_per_day() -> None:
    """Schedule against the scheduler directly - no lifespan, no job threads.

    The same scenario used to be asserted through the running web app, which
    took ~29s because the scheduler polls store status on every tick while
    holding the DuckDB write lock, starving the sync's own metadata write.
    """

    today = datetime(2026, 9, 1, 9, 0, tzinfo=SHANGHAI)

    class Store:
        def status(self):
            return SimpleNamespace(
                instrument_count=1,
                # Past the precompute ceiling so the sync skips the expensive
                # indicator pass; this test is about scheduling, not caching.
                bar_count=PRECOMPUTE_MAX_BARS + 1,
                last_online_sync_at=None,
            )

    class Runner:
        def __init__(self) -> None:
            self.submitted: list[str] = []
            self.active = False

        def has_active_jobs(self, descriptions):
            del descriptions
            return self.active

        def get(self, job_id):
            del job_id
            return None

        def is_cancel_requested(self, job_id):
            del job_id
            return False

        def submit_callable(self, target, *, description, job_id, exclusive_group):
            del target, description, exclusive_group
            self.submitted.append(job_id)
            self.active = True
            return job_id

    class Sync:
        def __init__(self) -> None:
            self.configs: list[object] = []

        def sync(self, config, progress_callback=None, *, detail_callback=None, abort_check=None):
            del progress_callback, detail_callback, abort_check
            self.configs.append(config)
            return {"written_bars": 1, "errors": 0}

    runner = Runner()
    sync = Sync()
    scheduler = AutomaticMarketSyncScheduler(
        store=Store(),
        runner=runner,
        market_sync=sync,
        precomputer=object(),
        clock=lambda: today,
        interval_seconds=60,
    )

    assert scheduler.submit_if_due() is not None
    # Same local day, and the first submission is still queued.
    assert scheduler.submit_if_due() is None
    assert len(runner.submitted) == 1

    payload = scheduler._run_sync(lambda done, total: None)
    config = sync.configs[0]
    assert config.provider == "eastmoney"
    assert config.prefer_store_targets is False
    assert config.refresh_names is False
    assert payload["written_bars"] == 1
