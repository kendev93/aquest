"""信号扫描日的判据：日历给上界、日线给实际，两者不一致时不许静默换日。"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from aquest.market_data.store import DuckDbMarketDataStore
from aquest.strategy_pool.signal_session import resolve_signal_session

SHANGHAI = ZoneInfo("Asia/Shanghai")
WEEK = (date(2026, 10, 19),) + tuple(
    date(2026, 10, 20) + timedelta(days=offset) for offset in range(4)
)


def _bars(dates: list[date]) -> pd.DataFrame:
    close = np.linspace(10.0, 11.0, len(dates))
    return pd.DataFrame(
        {
            "date": pd.to_datetime(dates),
            "open": close,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": np.full(len(dates), 1_000.0),
            "amount": close * 1_000,
            "bar_status": "completed",
        }
    )


def _store(
    tmp_path,  # noqa: ANN001, ANN202
    *,
    bar_dates: list[date],
    sessions: tuple[date, ...] | None = WEEK,
    coverage_end: date = date(2026, 10, 24),
) -> DuckDbMarketDataStore:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars(bar_dates), name="浦发银行")
    if sessions is not None:
        store.save_market_trading_calendar(
            sessions,
            source="eastmoney_sse_composite",
            coverage_start=coverage_end - timedelta(days=200),
            coverage_end=coverage_end,
        )
    return store


def test_weekend_scan_uses_the_previous_trading_day(tmp_path) -> None:
    """周六重跑：库里最后一根完整日线是周五，扫描日就该是周五，不需要缺口说明。"""

    store = _store(tmp_path, bar_dates=list(WEEK))
    session = resolve_signal_session(store, now=datetime(2026, 10, 24, 12, 0, tzinfo=SHANGHAI))

    assert session.scanned == date(2026, 10, 23)
    assert session.expected == date(2026, 10, 23)
    assert session.lag is None


def test_missing_latest_session_reports_the_gap(tmp_path) -> None:
    """日历说周五已收盘、库里只有到周四：扫周四，但必须把缺口带出去。"""

    store = _store(tmp_path, bar_dates=list(WEEK[:-1]))
    session = resolve_signal_session(store, now=datetime(2026, 10, 24, 12, 0, tzinfo=SHANGHAI))

    assert session.scanned == date(2026, 10, 22)
    assert session.lag == {
        "expected_session": "2026-10-23",
        "scanned_session": "2026-10-22",
    }


def test_future_completed_bar_cannot_push_the_signal_day_forward(tmp_path) -> None:
    """错年份导入的未来日期已完成行不是扫描日：未来不能决定今天看哪天的信号。"""

    store = _store(tmp_path, bar_dates=[*WEEK, date(2026, 10, 26)])
    session = resolve_signal_session(store, now=datetime(2026, 10, 24, 12, 0, tzinfo=SHANGHAI))

    assert session.scanned == date(2026, 10, 23)
    assert session.lag is None


def test_today_bar_before_publish_time_is_still_scanned(tmp_path) -> None:
    """交易日 18:30 之前：日历判据停在上一交易日，但今天的完整日线已经在库里，不能白丢一天。"""

    store = _store(tmp_path, bar_dates=list(WEEK))
    session = resolve_signal_session(store, now=datetime(2026, 10, 23, 16, 0, tzinfo=SHANGHAI))

    assert session.scanned == date(2026, 10, 23)
    assert session.expected == date(2026, 10, 22)
    assert session.lag is None


def test_without_calendar_coverage_falls_back_to_completed_bars(tmp_path) -> None:
    """日历读不到时行为与改动前一致：按完整日线选，不报缺口也不乱猜交易日。"""

    store = _store(tmp_path, bar_dates=list(WEEK[:-1]), sessions=None)
    session = resolve_signal_session(store, now=datetime(2026, 10, 24, 12, 0, tzinfo=SHANGHAI))

    assert session.scanned == date(2026, 10, 22)
    assert session.expected is None
    assert session.lag is None
