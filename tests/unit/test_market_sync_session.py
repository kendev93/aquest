"""「今天到底是不是交易日」「这个范围是否已经下完」这两个判据的口径测试。

同步按钮和每日调度共用这里的函数，所以判错一次会让整轮行情要么白下一遍、要么整天不补。
"""

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aquest.market_data.sync_session import (
    CALENDAR_REFRESH_META_KEY,
    SYNCED_SESSION_META_KEY,
    SYNCED_SESSION_SCOPE_LIMIT,
    CalendarRefreshError,
    calendar_refresh_is_due,
    latest_completed_session,
    range_display_bound,
    read_synced_session,
    record_synced_session,
    refresh_calendar_when_stale,
    refresh_trading_calendar,
    sync_scope_fingerprint,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


class Store:
    """只实现判据要用的三件事：存日历、按覆盖读日历、读写 data_meta。"""

    def __init__(self) -> None:
        self.metadata: dict[str, str] = {}
        self.saved: tuple[tuple[date, ...], date, date] | None = None
        self.save_rows = 2

    def save_market_trading_calendar(
        self,
        trade_dates,
        *,
        source,
        coverage_start=None,
        coverage_end=None,
    ) -> int:
        del source
        self.saved = (tuple(trade_dates), coverage_start, coverage_end)
        return self.save_rows

    def read_authoritative_trading_dates(self, start_date, end_date):
        if self.saved is None:
            raise ValueError("还没有日历")
        dates, coverage_start, coverage_end = self.saved
        if coverage_start > start_date or coverage_end < end_date:
            raise ValueError("权威交易日历未覆盖完整日期范围")
        return [item for item in dates if start_date <= item <= end_date]

    def get_meta(self, key):
        return self.metadata.get(key)

    def set_meta(self, key, value):
        self.metadata[key] = value


def _fetcher(*sessions: date):
    calls: list[tuple[date, date]] = []

    def fetch(start_date, end_date, *, timeout):
        del timeout
        calls.append((start_date, end_date))
        return tuple(item for item in sessions if start_date <= item <= end_date)

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def test_calendar_refresh_runs_before_and_after_the_publish_time() -> None:
    """一天最多取两次：发布时点前一次把覆盖做到今天，之后一次才看得见今天这个交易日。

    上游在收盘发布之前不会把今天列进日历（2026-10-23 09:16 实测问它，最新交易日还是
    10-22）。只按"今天取过了"记一次的话，当晚就再也看不见新交易日，行情天天晚一天。
    """

    store = Store()
    morning = datetime(2026, 10, 22, 9, 5, tzinfo=SHANGHAI)
    fetcher = _fetcher(date(2026, 10, 21), date(2026, 10, 22))

    assert refresh_calendar_when_stale(store, fetcher, now=morning) is True
    assert store.metadata[CALENDAR_REFRESH_META_KEY] == morning.isoformat()
    assert len(fetcher.calls) == 1
    # 发布时点之前反复调度，不许多打一次请求。
    assert refresh_calendar_when_stale(store, fetcher, now=morning + timedelta(hours=3)) is False
    assert len(fetcher.calls) == 1
    # 18:30 之后必须再取一次，否则当天那根交易日进不了判据。
    assert (
        refresh_calendar_when_stale(
            store, fetcher, now=datetime(2026, 10, 22, 18, 40, tzinfo=SHANGHAI)
        )
        is True
    )
    assert len(fetcher.calls) == 2
    # 取过了就不再取；过零点重新开一段。
    assert (
        refresh_calendar_when_stale(store, fetcher, now=datetime(2026, 10, 22, 21, tzinfo=SHANGHAI))
        is False
    )
    assert len(fetcher.calls) == 2
    assert (
        refresh_calendar_when_stale(
            store, fetcher, now=datetime(2026, 10, 23, 0, 30, tzinfo=SHANGHAI)
        )
        is True
    )
    assert len(fetcher.calls) == 3

    # 写库失败不留记号，下一次调度就重试——否则日历会一直缺着。
    failing = Store()
    failing.save_rows = 0
    broken = _fetcher(date(2026, 10, 21))

    assert refresh_calendar_when_stale(failing, broken, now=morning) is False
    assert CALENDAR_REFRESH_META_KEY not in failing.metadata
    # 没有取日历的对象时直接跳过，不影响调度本身。
    assert refresh_calendar_when_stale(Store(), None, now=morning) is False


def test_calendar_refresh_is_due_ignores_broken_stamps() -> None:
    """记号写坏了（包括只记日期的老格式）都不能让日历从此不再刷新。"""

    morning = datetime(2026, 10, 22, 9, 5, tzinfo=SHANGHAI)
    published = datetime(2026, 10, 22, 18, 40, tzinfo=SHANGHAI)

    assert calendar_refresh_is_due(None, morning) is True
    assert calendar_refresh_is_due("", morning) is True
    assert calendar_refresh_is_due("昨天", morning) is True
    # 老格式只记日期：按"今天凌晨"理解，发布前那一段已经算取过，发布后仍然要再取。
    assert calendar_refresh_is_due("2026-10-22", morning) is False
    assert calendar_refresh_is_due("2026-10-22", published) is True
    # 正常记号：发布前取过就够一段，到了发布时点之后就该再取。
    assert calendar_refresh_is_due(morning.isoformat(), morning + timedelta(minutes=30)) is False
    assert calendar_refresh_is_due(morning.isoformat(), published) is True
    assert calendar_refresh_is_due(published.isoformat(), published + timedelta(hours=1)) is False


def test_calendar_refresh_reports_empty_provider_response_as_failure() -> None:
    store = Store()

    try:
        refresh_trading_calendar(store, _fetcher(), today=date(2026, 10, 22))
    except CalendarRefreshError as exc:
        assert "没有返回有效交易日" in str(exc)
    else:
        raise AssertionError("空日历必须按失败处理")
    assert store.saved is None


def test_latest_completed_session_follows_close_time_and_holidays() -> None:
    store = Store()
    today = date(2026, 10, 22)
    before = datetime(2026, 10, 22, 18, 0, tzinfo=SHANGHAI)
    after = datetime(2026, 10, 22, 19, 0, tzinfo=SHANGHAI)

    # 今天是交易日：18:30 之前当天还不算已覆盖，判到上一交易日为止。
    store.saved = ((date(2026, 10, 21), today), today - timedelta(days=120), today)
    assert latest_completed_session(store, now=before) == date(2026, 10, 21)
    assert latest_completed_session(store, now=after) == today

    # 国庆假期：日历覆盖到今天而今天不是交易日，判据就用最后一个真实交易日。
    holiday = date(2026, 10, 3)
    store.saved = (
        (date(2026, 9, 30), holiday - timedelta(days=1)),
        holiday - timedelta(days=120),
        holiday,
    )
    assert latest_completed_session(store, now=datetime(2026, 10, 3, 19, 0, tzinfo=SHANGHAI)) == (
        holiday - timedelta(days=1)
    )

    # 覆盖没做到今天：判不出今天是不是交易日，必须返回 None（不许省下任何一次下载）。
    store.saved = ((date(2026, 10, 16),), date(2026, 6, 1), date(2026, 10, 16))
    assert latest_completed_session(store, now=after) is None

    class NoCalendar:
        def read_authoritative_trading_dates(self, *_args):
            raise RuntimeError("行情库读不动")

    assert latest_completed_session(NoCalendar(), now=after) is None  # type: ignore[arg-type]


def test_range_display_bound_only_hides_days_that_cannot_have_bars() -> None:
    """区间右端只在「今天一定不出日线」时才收成最近交易日，收盘前不许藏掉当天。

    请求窗口按自然日写到今天是有意为之（盘中那一轮要的就是当天那根），但进度文案跟着
    把周六/节假日写成一个要下的日子就会误导人。判据收不出来的时候必须原样保留。
    """

    store = Store()

    # 周六早上：日历覆盖做到 10-24，最新交易日是 10-23 → 右端收到 10-23。
    store.saved = (
        (date(2026, 10, 22), date(2026, 10, 23)),
        date(2026, 6, 1),
        date(2026, 10, 24),
    )
    assert range_display_bound(store, now=datetime(2026, 10, 24, 7, 37, tzinfo=SHANGHAI)) == date(
        2026, 10, 23
    )

    # 周四 15:00：日历还没列出 10-22，可那天正是该下的那一天 → 不收。
    store.saved = (
        (date(2026, 10, 21), date(2026, 10, 22)),
        date(2026, 6, 1),
        date(2026, 10, 22),
    )
    assert range_display_bound(store, now=datetime(2026, 10, 22, 15, 0, tzinfo=SHANGHAI)) is None

    # 周四 19:00：日历已经列出 10-22，判据就是今天 → 区间右端本来就是交易日，不收。
    assert range_display_bound(store, now=datetime(2026, 10, 22, 19, 0, tzinfo=SHANGHAI)) is None

    # 周四节假日（国庆）收盘后：覆盖做到今天却没列出今天 → 收到 9-30。
    store.saved = (
        (date(2026, 9, 29), date(2026, 9, 30)),
        date(2026, 6, 1),
        date(2026, 10, 1),
    )
    assert range_display_bound(store, now=datetime(2026, 10, 1, 19, 0, tzinfo=SHANGHAI)) == date(
        2026, 9, 30
    )

    # 覆盖没做到今天：无从判断今天是不是交易日，原样保留区间右端。
    store.saved = ((date(2026, 10, 16),), date(2026, 6, 1), date(2026, 10, 16))
    assert range_display_bound(store, now=datetime(2026, 10, 24, 7, 37, tzinfo=SHANGHAI)) is None

    class NoCalendar:
        def read_authoritative_trading_dates(self, *_args):
            raise RuntimeError("行情库读不动")

    assert range_display_bound(NoCalendar(), now=datetime(2026, 10, 24, tzinfo=SHANGHAI)) is None  # type: ignore[arg-type]


def test_sync_scope_fingerprint_names_the_scope_not_its_order() -> None:
    left = sync_scope_fingerprint(
        universe="all",
        provider="eastmoney",
        instrument_types=["stock"],
        boards=["chinext", "main"],
    )
    right = sync_scope_fingerprint(
        universe="ALL",
        provider="EASTMONEY",
        instrument_types=["stock"],
        boards=["main", "chinext"],
    )
    assert left == right
    assert left != sync_scope_fingerprint(universe="sh", provider="eastmoney")


def test_synced_session_stamp_round_trips_and_refuses_other_scopes() -> None:
    store = Store()
    finished = datetime(2026, 10, 22, 19, 4, tzinfo=SHANGHAI)
    scope = sync_scope_fingerprint(universe="all", provider="eastmoney")

    assert read_synced_session(store, scope) is None
    record_synced_session(
        store,
        session=date(2026, 10, 22),
        scope=scope,
        instruments=5_224,
        failed=0,
        provider="eastmoney",
        finished_at=finished,
    )
    stamp = read_synced_session(store, scope)
    assert stamp is not None
    assert stamp.instruments == 5_224
    assert stamp.usable_for(session=date(2026, 10, 22), scope=scope) is True
    # 交易日还没走到记号那一天：不算覆盖。
    assert stamp.usable_for(session=date(2026, 10, 23), scope=scope) is False

    # 换了范围就不能借全市场那份记号省下来；反过来窄范围的同步也不许冲掉全市场那份。
    narrow = sync_scope_fingerprint(universe="sh", provider="eastmoney")
    assert read_synced_session(store, narrow) is None
    record_synced_session(
        store,
        session=date(2026, 10, 21),
        scope=narrow,
        instruments=1_800,
        failed=0,
        provider="eastmoney",
        finished_at=datetime(2026, 10, 22, 19, 30, tzinfo=SHANGHAI),
    )
    assert read_synced_session(store, scope).session == date(2026, 10, 22)  # type: ignore[union-attr]
    assert read_synced_session(store, narrow).session == date(2026, 10, 21)  # type: ignore[union-attr]

    # 带失败标的的那次不算「下完了」：漏掉的那几只正是用户想补的。
    record_synced_session(
        store,
        session=date(2026, 10, 22),
        scope=scope,
        instruments=5_224,
        failed=3,
        provider="eastmoney",
        finished_at=finished,
    )
    assert (
        read_synced_session(store, scope).usable_for(  # type: ignore[union-attr]
            session=date(2026, 10, 22), scope=scope
        )
        is False
    )


def test_synced_session_records_prune_the_oldest_scopes() -> None:
    """范围种类有限，记号按完成时间淘汰最旧的，不让 data_meta 无限膨胀。"""

    store = Store()
    base = datetime(2026, 10, 22, 8, 0, tzinfo=SHANGHAI)
    for index in range(SYNCED_SESSION_SCOPE_LIMIT + 4):
        record_synced_session(
            store,
            session=date(2026, 10, 22),
            scope=f"scope-{index}",
            instruments=10,
            failed=0,
            provider="eastmoney",
            finished_at=base + timedelta(minutes=index),
        )
    entries = json.loads(store.metadata[SYNCED_SESSION_META_KEY])["scopes"]
    assert len(entries) == SYNCED_SESSION_SCOPE_LIMIT
    assert "scope-0" not in entries  # 最旧的那几条被淘汰
    assert read_synced_session(store, f"scope-{SYNCED_SESSION_SCOPE_LIMIT + 3}") is not None


def test_broken_synced_session_record_is_treated_as_no_record() -> None:
    store = Store()
    scope = sync_scope_fingerprint(universe="all", provider="eastmoney")
    store.metadata[SYNCED_SESSION_META_KEY] = "不是一段 JSON"
    assert read_synced_session(store, scope) is None
    store.metadata[SYNCED_SESSION_META_KEY] = '{"scopes": {"x": {"session": "2026-13-45"}}}'
    assert read_synced_session(store, "x") is None
    store.metadata[SYNCED_SESSION_META_KEY] = '{"session": "2026-10-22"}'
    assert read_synced_session(store, scope) is None
