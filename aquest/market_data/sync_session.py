"""「本交易日是否已经同步过」的判据与记号。

点「同步行情」时要决定这次要不要发起下载，每日调度时要决定要不要刷新交易日历，两处
必须给同一个答案。答案只能落在交易日历上：库里最后一根已完成日线是 10-21，说明不了
10-22 到底是不是交易日；反过来日历说 10-22 是交易日、而同步记号说这个范围已经把它
下完了，那点按钮就无事可做。

日历原先只在收盘后的自动同步路径里取（见 :mod:`aquest.market_data.auto_sync`），而任何
一次手动同步都会把当天那轮调度挡在 ``auto_sync_due`` 外面，于是日历能连着几天不刷新
（2026-10-22 实测 ``market_calendar_latest_date`` 还停在 10-16）。取日历、存日历、判
交易日都收在这里，由调度器和请求闸门共用，两边都不再自己抄一份规则。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from aquest.market_data.store import DuckDbMarketDataStore

logger = logging.getLogger(__name__)

_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

#: 收盘之后还要等数据源把当天日线发布出来；沿用自动同步一直在用的同一个时刻。
MARKET_SESSION_PUBLISHED_TIME = time(18, 30)
#: 一次取多长的日历。120 天足够跨过最长连休，也让"今天是不是交易日"有权威答案。
TRADING_CALENDAR_LOOKBACK_DAYS = 120
TRADING_CALENDAR_PROVIDER = "eastmoney_sse_composite"
#: 最近一次成功刷新日历的时刻（``data_meta``，上海时区 ISO）；按发布时点分段判断该不该再取。
CALENDAR_REFRESH_META_KEY = "market_calendar_refresh_date"
#: 某个研究范围在某个交易日已经完整同步过的记号（``data_meta``，值是按范围分格的 JSON）。
SYNCED_SESSION_META_KEY = "market_sync_completed_session"
#: 记号里最多留几个范围；超出的按完成时间淘汰最旧的，不留垃圾也不要求用户清理。
SYNCED_SESSION_SCOPE_LIMIT = 8
#: 单次日历请求的超时；日历只服务一个判据，不该把一次点击拖成十秒等待。
CALENDAR_FETCH_TIMEOUT_SECONDS = 10.0


class CalendarFetcher(Protocol):
    """能按日期区间取交易日历的对象（``OnlineMarketSync.fetch_trading_calendar``）。"""

    def __call__(
        self,
        start_date: date,
        end_date: date,
        *,
        timeout: float = CALENDAR_FETCH_TIMEOUT_SECONDS,
    ) -> Sequence[date]: ...


class CalendarRefreshError(RuntimeError):
    """日历取不到或存不下。调用方决定是退回自然日兜底还是什么都不做。"""


@dataclass(frozen=True)
class SyncedSession:
    """某一次成功同步留下的记号：哪个范围、下到了哪个交易日。"""

    session: date
    scope: str
    instruments: int
    failed: int
    finished_at: datetime
    provider: str

    def usable_for(self, *, session: date, scope: str) -> bool:
        """这次点击能不能靠这个记号省下来：范围一致、交易日一致、且没有失败标的。

        带失败标的的记号不算"下完了"——漏掉的那几只正是用户重点一次想要补的东西。
        """

        return self.session >= session and self.scope == scope and self.failed == 0

    def to_entry(self) -> dict[str, object]:
        return {
            "session": self.session.isoformat(),
            "instruments": self.instruments,
            "failed": self.failed,
            "finished_at": self.finished_at.isoformat(),
            "provider": self.provider,
        }

    @staticmethod
    def from_entry(scope: str, entry: Mapping[str, object]) -> SyncedSession | None:
        try:
            return SyncedSession(
                session=date.fromisoformat(str(entry["session"])),
                scope=scope,
                instruments=int(str(entry["instruments"])),
                failed=int(str(entry["failed"])),
                finished_at=datetime.fromisoformat(str(entry["finished_at"])),
                provider=str(entry["provider"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


def shanghai_now(now: datetime | None = None) -> datetime:
    current = now or datetime.now(_SHANGHAI_TZ)
    if current.tzinfo is None:
        return current.replace(tzinfo=_SHANGHAI_TZ)
    return current.astimezone(_SHANGHAI_TZ)


def refresh_trading_calendar(
    store: DuckDbMarketDataStore,
    fetcher: CalendarFetcher,
    *,
    today: date,
    timeout: float = CALENDAR_FETCH_TIMEOUT_SECONDS,
) -> tuple[date, ...]:
    """取最近 ``TRADING_CALENDAR_LOOKBACK_DAYS`` 天的日历并落库（含覆盖声明）。"""

    coverage_start = today - timedelta(days=TRADING_CALENDAR_LOOKBACK_DAYS)
    sessions = [
        item
        for item in fetcher(coverage_start, today, timeout=timeout)
        if isinstance(item, date) and coverage_start <= item <= today
    ]
    if not sessions:
        raise CalendarRefreshError("行情源没有返回有效交易日历")
    unique = tuple(sorted(set(sessions)))
    saved = store.save_market_trading_calendar(
        unique,
        source=TRADING_CALENDAR_PROVIDER,
        coverage_start=coverage_start,
        coverage_end=today,
    )
    if not saved:
        raise CalendarRefreshError("交易日历写入行情库失败")
    return unique


def calendar_refresh_is_due(stored: str | None, current: datetime) -> bool:
    """今天还需不需要再取一次日历：发布时点之前一次、之后再一次。

    上游把「今天」列进交易日历要等收盘发布之后（实测 10-23 09:16 问它，最新交易日还
    是 10-22），所以凌晨那次刷新拿到的列表只到昨天，却会把覆盖声明做到今天。按"一天
    一次"记的话，当晚发布之后就再也不会刷新，判据整个晚上都停在昨天——等于当天的行情
    要到第二天才被认出来。分两段各允许一次：发布时点之前那一次负责把覆盖声明做到今天
    （周末和节假日就靠它），之后的那一次才真正看见新交易日。
    """

    published = current.replace(
        hour=MARKET_SESSION_PUBLISHED_TIME.hour,
        minute=MARKET_SESSION_PUBLISHED_TIME.minute,
        second=0,
        microsecond=0,
    )
    boundary = (
        published
        if current >= published
        else current.replace(hour=0, minute=0, second=0, microsecond=0)
    )
    if not stored:
        return True
    try:
        stamped = datetime.fromisoformat(stored)
    except ValueError:
        # 老格式（只记日期）或写坏了的值：按"没刷过"处理，多取一次日历没有代价。
        return True
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=_SHANGHAI_TZ)
    return stamped.astimezone(_SHANGHAI_TZ) < boundary


def refresh_calendar_when_stale(
    store: DuckDbMarketDataStore,
    fetcher: CalendarFetcher | None,
    *,
    now: datetime | None = None,
) -> bool:
    """日历该刷就刷一次；成功返回 True，取不到或本轮还不该取时返回 False。

    刻意不跟"今天是否已经同步过行情"绑定：日历是判据，同步是动作，绑在一起的话用户
    手点一次同步，当天就再也不会刷新日历，判据从此停在旧日期上。
    """

    if fetcher is None or not callable(fetcher):
        return False
    current = shanghai_now(now)
    today = current.date()
    try:
        stored = store.get_meta(CALENDAR_REFRESH_META_KEY)
    except Exception:  # noqa: BLE001 - 读不到记号按没取过处理，宁可多取一次日历
        logger.exception("读取交易日历刷新记号失败，按未刷新处理")
        stored = None
    if not calendar_refresh_is_due(stored, current):
        return False
    try:
        refresh_trading_calendar(store, fetcher, today=today)
    except Exception as exc:  # noqa: BLE001 - 日历失败不能拖垮调度本身
        logger.warning("交易日历刷新失败，下一次调度再试：%s", str(exc)[:200])
        return False
    try:
        store.set_meta(CALENDAR_REFRESH_META_KEY, current.isoformat())
    except Exception:  # noqa: BLE001 - 已落库，记号写失败只是这轮会多取一次
        logger.exception("写入交易日历刷新记号失败")
    return True


def latest_completed_session(
    store: DuckDbMarketDataStore,
    *,
    now: datetime | None = None,
) -> date | None:
    """已经收盘、且数据源应当已经发布出来的最近一个交易日。

    判据要求日历把覆盖声明做到今天：``read_authoritative_trading_dates`` 在覆盖不完整
    时抛错，这里一律当作"判不出来"返回 None——判不出来就不许省下任何一次下载。
    """

    current = shanghai_now(now)
    today = current.date()
    start = today - timedelta(days=TRADING_CALENDAR_LOOKBACK_DAYS)
    try:
        sessions = list(store.read_authoritative_trading_dates(start, today))
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        # 认不出来的行情库（没有权威日历读法）与覆盖不足一样按"判不出来"处理：
        # 判不出来就不许省下任何一次下载。
        logger.debug("交易日历判不出最新交易日（覆盖不足或读失败）：%s", str(exc)[:200])
        return None
    if not sessions:
        return None
    last_session = sessions[-1]
    if last_session == today and (current.hour, current.minute) < (
        MARKET_SESSION_PUBLISHED_TIME.hour,
        MARKET_SESSION_PUBLISHED_TIME.minute,
    ):
        # 今天还没到发布时点：当天日线要么还没出、要么只是盘中的临时行，不能算已覆盖。
        return sessions[-2] if len(sessions) > 1 else None
    return last_session


def range_display_bound(
    store: DuckDbMarketDataStore,
    *,
    now: datetime | None = None,
) -> date | None:
    """同步区间在界面上可以放心写到的右端；返回 None 表示别收，照旧写到今天。

    区间右端一直是「下载到今天为止」的自然日语义：数据源在周末只会把最近几根交易日
    给回来，今天那一段本来就取不到东西。把今天写进区间里会让人以为要下一根不存在的
    日线（2026-10-24 周六实测如此）。只在能确定今天不出日线时才收成最近一个交易日：
    周末，或者收盘后日历已覆盖到今天却仍没列出今天。收盘前不收——那天可能正好是要
    下的那一天，藏掉它就等于说错这一轮在下什么。
    """

    current = shanghai_now(now)
    today = current.date()
    if today.weekday() < 5 and (current.hour, current.minute) < (
        MARKET_SESSION_PUBLISHED_TIME.hour,
        MARKET_SESSION_PUBLISHED_TIME.minute,
    ):
        return None
    last_session = latest_completed_session(store, now=now)
    if last_session is None or last_session >= today:
        return None
    return last_session


def sync_scope_fingerprint(
    *,
    universe: str,
    provider: str,
    instrument_types: Sequence[str] | None = None,
    boards: Sequence[str] | None = None,
) -> str:
    """同一个下载范围的名字：记号只对同样的范围有效。"""

    return "|".join(
        (
            universe.strip().lower() or "all",
            provider.strip().lower(),
            ",".join(sorted({item for item in instrument_types or () if item})),
            ",".join(sorted({item for item in boards or () if item})),
        )
    )


def record_synced_session(
    store: DuckDbMarketDataStore,
    *,
    session: date,
    scope: str,
    instruments: int,
    failed: int,
    provider: str,
    finished_at: datetime | None = None,
) -> SyncedSession:
    """把「这个范围已经下到了这个交易日」写进行情库，按范围各留一份。

    分开留是因为一次同步只代表它自己那个范围：全市场下完了不该让「只下沪市」也自称下完，
    反过来窄范围的同步也不该把全市场的记号冲掉。范围种类有限，超出上限的按完成时间淘汰
    最旧的一条，不需要人工清理。
    """

    stamp = SyncedSession(
        session=session,
        scope=scope,
        instruments=instruments,
        failed=failed,
        finished_at=finished_at or shanghai_now(),
        provider=provider,
    )
    entries = _read_entries(store)
    entries[scope] = stamp.to_entry()
    if len(entries) > SYNCED_SESSION_SCOPE_LIMIT:
        by_age = sorted(entries.items(), key=lambda item: str(item[1].get("finished_at") or ""))
        entries = dict(by_age[-SYNCED_SESSION_SCOPE_LIMIT:])
    store.set_meta(SYNCED_SESSION_META_KEY, json.dumps({"scopes": entries}, sort_keys=True))
    return stamp


def read_synced_session(store: DuckDbMarketDataStore, scope: str) -> SyncedSession | None:
    """读回某个范围的记号；读不到或不认识都按「没有记号」处理，也就是照旧允许下载。"""

    entry = _read_entries(store).get(scope)
    if entry is None:
        return None
    stamp = SyncedSession.from_entry(scope, entry)
    if stamp is None:
        logger.warning("行情同步记号格式不认识，按无记号处理：%s", str(entry)[:200])
    return stamp


def _read_entries(store: DuckDbMarketDataStore) -> dict[str, dict[str, object]]:
    try:
        raw = store.get_meta(SYNCED_SESSION_META_KEY)
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return {}
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    scopes = value.get("scopes") if isinstance(value, Mapping) else None
    if not isinstance(scopes, Mapping):
        return {}
    return {str(key): dict(item) for key, item in scopes.items() if isinstance(item, Mapping)}
