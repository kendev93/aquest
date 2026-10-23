import logging
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from aquest.indicators.precompute import PRECOMPUTE_MAX_BARS
from aquest.market_data.auto_sync import AutomaticMarketSyncScheduler, auto_sync_due

SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_auto_sync_is_due_when_never_synced_or_last_sync_was_before_today() -> None:
    today = datetime(2026, 9, 1, 9, 0, tzinfo=SHANGHAI)

    assert auto_sync_due(None, now=today) is True
    assert auto_sync_due("2026-08-31T23:59:00+08:00", now=today) is True
    assert auto_sync_due("2026-09-01T08:00:00+08:00", now=today) is False


def test_auto_sync_treats_invalid_metadata_as_due() -> None:
    today = datetime(2026, 9, 1, 9, 0, tzinfo=SHANGHAI)

    assert auto_sync_due("not-a-timestamp", now=today) is True


def test_auto_sync_skips_holiday_after_calendar_check_and_persists_the_check_date() -> None:
    today = datetime(2026, 10, 2, 19, 0, tzinfo=SHANGHAI)

    class Store:
        def __init__(self) -> None:
            self.metadata: dict[str, str] = {}

        def status(self):
            return SimpleNamespace(
                instrument_count=1,
                bar_count=100,
                data_end="2026-09-30",
                last_online_sync_at="2026-09-30T18:31:00+08:00",
            )

        def get_meta(self, key):
            return self.metadata.get(key)

        def set_meta(self, key, value):
            self.metadata[key] = value

        def save_market_trading_calendar(
            self,
            trade_dates,
            *,
            source,
            coverage_start=None,
            coverage_end=None,
        ):
            self.saved_calendar = (tuple(trade_dates), source, coverage_start, coverage_end)
            return len(tuple(trade_dates))

        def read_authoritative_trading_dates(self, start_date, end_date):
            """按刚落库的那一份日历回答「<= end_date 的交易日」，覆盖不够就抛错。"""

            dates, _source, coverage_start, coverage_end = self.saved_calendar
            if coverage_start > start_date or coverage_end < end_date:
                raise ValueError("权威交易日历未覆盖完整日期范围")
            return [item for item in dates if start_date <= item <= end_date]

    class Runner:
        def __init__(self) -> None:
            self.submitted = []

        def has_active_jobs(self, descriptions):
            del descriptions
            return False

        def submit_callable(self, *args, **kwargs):
            self.submitted.append((args, kwargs))

    class Sync:
        def __init__(self) -> None:
            self.calendar_calls = 0

        def fetch_trading_calendar(self, start_date, end_date, *, timeout):
            del start_date, end_date, timeout
            self.calendar_calls += 1
            return (date(2026, 9, 29), date(2026, 9, 30))

    store = Store()
    runner = Runner()
    sync = Sync()
    scheduler = AutomaticMarketSyncScheduler(
        store=store,
        runner=runner,
        market_sync=sync,
        precomputer=object(),
        clock=lambda: today,
    )

    assert scheduler.submit_if_due() is None
    assert store.metadata["last_auto_sync_check_date"] == "2026-10-02"
    assert store.metadata["last_auto_sync_check_session"] == "2026-09-30"
    assert store.saved_calendar[0][-1] == date(2026, 9, 30)
    assert store.saved_calendar[1] == "eastmoney_sse_composite"
    assert store.saved_calendar[2] == today.date() - timedelta(days=120)
    assert store.saved_calendar[3] == today.date()
    assert runner.submitted == []

    restarted = AutomaticMarketSyncScheduler(
        store=store,
        runner=runner,
        market_sync=sync,
        precomputer=object(),
        clock=lambda: today,
    )
    assert restarted.submit_if_due() is None
    assert sync.calendar_calls == 1


def test_auto_sync_uses_latest_exchange_session_as_provider_end_date() -> None:
    now = [datetime(2026, 10, 9, 10, 0, tzinfo=SHANGHAI)]

    class Store:
        def status(self):
            return SimpleNamespace(
                instrument_count=1,
                bar_count=PRECOMPUTE_MAX_BARS + 1,
                data_end="2026-09-30",
                last_online_sync_at="2026-10-02T00:16:41+08:00",
            )

        def __init__(self) -> None:
            self.saved_calendar = None
            self.metadata: dict[str, str] = {}

        def get_meta(self, key):
            return self.metadata.get(key)

        def set_meta(self, key, value):
            self.metadata[key] = value

        def save_market_trading_calendar(
            self,
            trade_dates,
            *,
            source,
            coverage_start=None,
            coverage_end=None,
        ):
            self.saved_calendar = (tuple(trade_dates), source, coverage_start, coverage_end)
            return len(tuple(trade_dates))

        def read_authoritative_trading_dates(self, start_date, end_date):
            """按刚落库的那一份日历回答「<= end_date 的交易日」，覆盖不够就抛错。"""

            dates, _source, coverage_start, coverage_end = self.saved_calendar
            if coverage_start > start_date or coverage_end < end_date:
                raise ValueError("权威交易日历未覆盖完整日期范围")
            return [item for item in dates if start_date <= item <= end_date]

    class Runner:
        def __init__(self) -> None:
            self.target = None

        def has_active_jobs(self, descriptions):
            del descriptions
            return False

        def submit_callable(self, target, **kwargs):
            del kwargs
            self.target = target

        def is_cancel_requested(self, job_id):
            del job_id
            return False

    class Sync:
        def __init__(self) -> None:
            self.configs = []
            self.calendar_calls = 0

        def fetch_trading_calendar(self, start_date, end_date, *, timeout):
            del start_date, end_date, timeout
            self.calendar_calls += 1
            return (date(2026, 9, 30), date(2026, 10, 9))

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
        clock=lambda: now[0],
    )

    assert scheduler.submit_if_due() is None
    # 收盘前不会发起同步，但日历照取：它现在是「今天算不算交易日」的唯一依据，
    # 而且必须独立于同步动作，否则一次手动同步就能让它连着几天不刷新。
    assert sync.calendar_calls == 1
    now[0] = datetime(2026, 10, 9, 19, 0, tzinfo=SHANGHAI)
    assert scheduler.submit_if_due() is not None
    assert runner.target is not None
    runner.target(lambda _current, _total: None)
    assert sync.configs[0].end_date == date(2026, 10, 9)
    # 19:00 这一轮用的是早上已落库的日历，没有再取第二次。
    assert sync.calendar_calls == 1


def test_auto_sync_falls_back_to_natural_date_when_calendar_is_unavailable() -> None:
    now = datetime(2026, 10, 9, 19, 0, tzinfo=SHANGHAI)

    class Store:
        def status(self):
            return SimpleNamespace(
                instrument_count=1,
                bar_count=100,
                data_end="2026-09-30",
                last_online_sync_at="2026-10-08T18:31:00+08:00",
            )

    class Runner:
        def __init__(self) -> None:
            self.target = None

        def has_active_jobs(self, descriptions):
            del descriptions
            return False

        def submit_callable(self, target, **kwargs):
            del kwargs
            self.target = target

        def is_cancel_requested(self, job_id):
            del job_id
            return False

    class Sync:
        def fetch_trading_calendar(self, start_date, end_date, *, timeout):
            del start_date, end_date, timeout
            raise TimeoutError("calendar provider unavailable")

        def sync(self, config, progress_callback=None, *, detail_callback=None, abort_check=None):
            del progress_callback, detail_callback, abort_check
            self.config = config
            return {"written_bars": 1, "errors": 0}

    runner = Runner()
    sync = Sync()
    scheduler = AutomaticMarketSyncScheduler(
        store=Store(),
        runner=runner,
        market_sync=sync,
        precomputer=SimpleNamespace(run=lambda **_kwargs: SimpleNamespace(to_dict=lambda: {})),
        clock=lambda: now,
    )

    assert scheduler.submit_if_due() is not None
    assert runner.target is not None
    runner.target(lambda _current, _total: None)
    assert sync.config.end_date == now.date()


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


def test_auto_sync_invalidates_the_cache_and_precomputes_incrementally() -> None:
    """自动同步也要走手动同步那套：清内存缓存 + 增量 + 按行情末年锚定。

    少了清缓存这一步，同日重写行情后指标缓存会继续返回旧值；少了
    ``end_year``，历史数据停在较早年份时「最近五年」会少算早期年份。
    """

    today = datetime(2026, 9, 1, 9, 0, tzinfo=SHANGHAI)

    class Store:
        def status(self):
            return SimpleNamespace(
                instrument_count=1,
                bar_count=10,
                data_end="2024-06-30",
                last_online_sync_at=None,
            )

    class Runner:
        def has_active_jobs(self, descriptions):
            del descriptions
            return False

        def get(self, job_id):
            del job_id
            return None

        def is_cancel_requested(self, job_id):
            del job_id
            return False

    class Sync:
        def sync(self, config, progress_callback=None, *, detail_callback=None, abort_check=None):
            del config, progress_callback, detail_callback, abort_check
            return {"written_bars": 1, "errors": 0}

    class Precomputer:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def run(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(to_dict=lambda: {"status": "completed"})

    class Cache:
        def __init__(self) -> None:
            self.cleared = 0

        def clear_memory(self) -> None:
            self.cleared += 1

    precomputer = Precomputer()
    cache = Cache()
    scheduler = AutomaticMarketSyncScheduler(
        store=Store(),
        runner=Runner(),
        market_sync=Sync(),
        precomputer=precomputer,
        clock=lambda: today,
        indicator_cache=cache,
    )

    scheduler._run_sync(lambda done, total: None)

    assert cache.cleared == 1
    assert precomputer.calls[0]["only_changed"] is True
    # 行情库停在 2024 年，窗口终点就不能是系统当前年份。
    assert precomputer.calls[0]["end_year"] == 2024


def test_auto_sync_precompute_failure_does_not_fail_the_sync() -> None:
    """缓存清理本身也不能把同步拖垮。"""

    today = datetime(2026, 9, 1, 9, 0, tzinfo=SHANGHAI)

    class Store:
        def status(self):
            return SimpleNamespace(
                instrument_count=1,
                bar_count=PRECOMPUTE_MAX_BARS + 1,
                last_online_sync_at=None,
            )

    class Runner:
        def has_active_jobs(self, descriptions):
            del descriptions
            return False

        def is_cancel_requested(self, job_id):
            del job_id
            return False

    class Sync:
        def sync(self, config, progress_callback=None, *, detail_callback=None, abort_check=None):
            del config, progress_callback, detail_callback, abort_check
            return {"written_bars": 1, "errors": 0}

    class ExplodingCache:
        def clear_memory(self) -> None:
            raise RuntimeError("cache poisoned")

    scheduler = AutomaticMarketSyncScheduler(
        store=Store(),
        runner=Runner(),
        market_sync=Sync(),
        precomputer=object(),
        clock=lambda: today,
        indicator_cache=ExplodingCache(),
    )

    payload = scheduler._run_sync(lambda done, total: None)

    assert payload["written_bars"] == 1


class _AlwaysDueStore:
    """永远「今天还没同步」：只考察调度，不掺行情判定。"""

    def status(self):
        return SimpleNamespace(
            instrument_count=1,
            bar_count=PRECOMPUTE_MAX_BARS + 1,
            last_online_sync_at=None,
        )


class _FlippableRunner:
    """一个能被测试拨到任意终态的假任务队列。"""

    def __init__(self) -> None:
        self.state: SimpleNamespace | None = SimpleNamespace(status="running", error=None)
        self.submits = 0

    def has_active_jobs(self, descriptions):
        del descriptions
        return False

    def get(self, job_id):
        del job_id
        return self.state

    def submit_callable(self, target, **kwargs):
        del target, kwargs
        self.submits += 1
        self.state = SimpleNamespace(status="running", error=None)


def _backoff_scheduler(runner: _FlippableRunner) -> AutomaticMarketSyncScheduler:
    return AutomaticMarketSyncScheduler(
        store=_AlwaysDueStore(),
        runner=runner,
        market_sync=object(),
        precomputer=object(),
        clock=lambda: datetime(2026, 9, 1, 19, 0, tzinfo=SHANGHAI),
        interval_seconds=15 * 60,
    )


def test_auto_sync_backs_off_after_repeated_failures_and_resets_on_success(caplog) -> None:
    """源站限流或断网时，每 15 分钟重敲一次全市场只会更糟：15 → 30 → 60 → 120 分钟，封顶 2 小时。"""

    runner = _FlippableRunner()
    scheduler = _backoff_scheduler(runner)

    assert scheduler.submit_if_due() is not None
    assert scheduler.next_check_delay_seconds() == 15 * 60

    with caplog.at_level(logging.WARNING, logger="aquest.market_data.auto_sync"):
        for streak, expected_minutes in ((1, 30), (2, 60), (3, 120), (4, 120)):
            runner.state = SimpleNamespace(status="failed", error="源站限流")
            assert scheduler.submit_if_due() is not None
            assert scheduler.next_check_delay_seconds() == expected_minutes * 60
            assert f"连续 {streak} 次" in caplog.text

        # 每一档都要留下可读的等待时长，否则运维只能靠猜。
        assert "120.0 分钟后再检查调度：源站限流" in caplog.text

    runner.state = SimpleNamespace(status="completed", error=None)
    assert scheduler.submit_if_due() is not None
    assert scheduler.next_check_delay_seconds() == 15 * 60


def test_auto_sync_backs_off_when_the_submission_itself_fails() -> None:
    """提交都失败（队列满、线程池炸）同样是失败，不能继续按 15 分钟硬撞。"""

    class RejectingRunner(_FlippableRunner):
        def submit_callable(self, target, **kwargs):
            del target, kwargs
            raise RuntimeError("后台任务队列已满")

    scheduler = _backoff_scheduler(RejectingRunner())

    with pytest.raises(RuntimeError, match="后台任务队列已满"):
        scheduler.submit_if_due()

    assert scheduler.next_check_delay_seconds() == 30 * 60


def test_auto_sync_keeps_the_normal_interval_for_cancelled_and_recycled_jobs() -> None:
    """用户主动取消不是故障；任务记录被回收时判不出成败，也不能当成失败。"""

    runner = _FlippableRunner()
    scheduler = _backoff_scheduler(runner)

    assert scheduler.submit_if_due() is not None
    runner.state = SimpleNamespace(status="cancelled", error=None)
    assert scheduler.submit_if_due() is not None
    assert scheduler.next_check_delay_seconds() == 15 * 60

    runner.state = None
    assert scheduler.submit_if_due() is not None
    assert scheduler.next_check_delay_seconds() == 15 * 60
    assert scheduler._active_job_id is not None
