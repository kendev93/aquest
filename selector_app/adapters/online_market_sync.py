"""Online-backed stock universe discovery and daily-bar synchronization."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from threading import Lock
from typing import Any, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from selector_app.market_data.day_format import classify_board, classify_instrument
from selector_app.market_data.models import (
    InstrumentBoard,
    InstrumentRef,
    InstrumentType,
    MarketCode,
)
from selector_app.market_data.scope import InstrumentScope
from selector_app.market_data.staging import (
    ProviderStagePlan,
    StageTarget,
    StageTargetResult,
    is_throttling_error,
    latest_frame_from_rows,
    serialize_latest_rows,
)
from selector_app.market_data.store import DuckDbMarketDataStore

from .market_sync import (
    AbortCheck,
    DetailCallback,
    InstrumentDiscoveryReport,
    MarketDataCancelledError,
    MarketSyncConfig,
    MarketSyncReport,
    ProgressCallback,
    _instrument_detail,
    _report_detail,
)

_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_MARKET_CLOSE = (15, 5)
_FULL_HISTORY_START = date(1990, 1, 1)
_INCREMENTAL_OVERLAP_DAYS = 10
_MAX_RETRIES = 3
_RETRY_BASE_SECONDS = 0.25
# 证券列表：服务端每页只给 100 条，全量目录要几十个请求，任一页重试耗尽就会
# 让整次同步失败，所以单页按"时间预算 + 上限次数"重试，而不是固定 2~3 次。
_CATALOG_RETRY_SECONDS = 45.0
_CATALOG_MAX_ATTEMPTS = 24
_CATALOG_RETRY_BASE_SECONDS = 1.0
_CATALOG_RETRY_MAX_SECONDS = 4.0
_REQUEST_INTERVAL_SECONDS = 0.15
_STOCK_BOARDS = frozenset({"main", "star", "chinext"})
_EASTMONEY_LIST_URL = "https://82.push2.eastmoney.com/api/qt/clist/get"
_EASTMONEY_FUND_LIST_URL = "https://88.push2.eastmoney.com/api/qt/clist/get"
# East Money 的 clist 节点各自独立限流：同一时刻有的正常返回、有的直接断开连接。
# 目录请求失败时轮换到下一个节点，避免整次同步因为恰好选中一个被限流的节点而中止。
_EASTMONEY_CATALOG_HOSTS = (
    "82.push2.eastmoney.com",
    "7.push2.eastmoney.com",
    "2.push2.eastmoney.com",
    "88.push2.eastmoney.com",
)
_EASTMONEY_HISTORY_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
_TENCENT_HISTORY_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
_EASTMONEY_PAGE_SIZE = 5_000
# East Money silently caps ``pz`` at 100 rows per page no matter what we ask for,
# so a full A-share catalog needs hundreds of pages.  The old 100-page ceiling
# truncated the list to whatever the first pages happened to contain.
_MAX_CATALOG_PAGES = 1_000
_EASTMONEY_UT = "bd1d9ddb04089700cf9c27f6f7426281"
_EASTMONEY_HISTORY_UT = "7eea3edcaed734bea9cbfc24409ed989"
_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
_EMPTY_COLUMNS = [
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "bar_status",
]

# SH main (t:2) + SH STAR (t:23) + SZ main (t:6) + SZ ChiNext (t:80).  Matches
# ``_STOCK_BOARDS`` (main/star/chinext) server-side; B shares and the Beijing
# exchange - which this project's SH/SZ market model does not cover - stay out.
_EASTMONEY_STOCK_FILTER = "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23"


class OnlineProviderError(RuntimeError):
    """Base error for actionable online provider failures."""


class OnlineProviderUnavailable(OnlineProviderError):
    """Raised when the online provider cannot be initialized."""


class OnlineDataError(OnlineProviderError):
    """Raised when an upstream online response cannot be trusted."""


HttpTransport = Callable[[str, Mapping[str, object], float], object]


class OnlineModule(Protocol):
    __version__: str

    def fetch_instruments(self, *, timeout: float) -> pd.DataFrame: ...

    def fetch_fund_instruments(self, *, timeout: float) -> pd.DataFrame: ...

    def fetch_daily_history(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        *,
        timeout: float,
    ) -> pd.DataFrame: ...

    def fetch_tencent_daily_history(
        self,
        market: MarketCode,
        code: str,
        start_date: str,
        end_date: str,
        *,
        timeout: float,
    ) -> pd.DataFrame: ...


OnlineModuleFactory = Callable[[], OnlineModule]
SleepFunction = Callable[[float], None]
OnlineTarget = tuple[MarketCode, str, InstrumentType, InstrumentBoard, str | None]


@dataclass(frozen=True)
class _TargetSelection:
    targets: list[OnlineTarget]
    listed_instruments: int
    filtered_instruments: int
    target_source: str
    fingerprint: str | None = None


@dataclass(frozen=True)
class _BatchReport:
    processed: int
    updated_files: int
    unchanged_files: int
    written_bars: int
    errors: int
    failure_reasons: Mapping[str, int]
    history_sources: Mapping[str, int]


@dataclass(frozen=True)
class _SyncResult:
    written_bars: int = 0
    bar_count: int = 0
    range_start: str | None = None
    range_end: str | None = None
    retries: int = 0
    history_source: str = "eastmoney"


@dataclass(frozen=True)
class _ExistingBars:
    """Bounded latest rows plus persisted coverage metadata for one target."""

    latest: pd.DataFrame
    first_date: date | None = None
    last_date: date | None = None
    bar_count: int = 0


@dataclass(frozen=True)
class _FetchedTarget:
    frame: pd.DataFrame
    retries: int
    history_source: str


class _RateLimiter:
    def __init__(self, interval_seconds: float, sleep: SleepFunction) -> None:
        self._interval_seconds = interval_seconds
        self._sleep = sleep
        self._lock = Lock()
        self._next_allowed = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_allowed - now)
            self._next_allowed = max(now, self._next_allowed) + self._interval_seconds
        if delay > 0:
            self._sleep(delay)


def _http_transport(url: str, params: Mapping[str, object], timeout: float) -> object:
    query = urlencode({key: str(value) for key, value in params.items()})
    request = Request(
        f"{url}?{query}",
        headers={
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://quote.eastmoney.com/",
            "User-Agent": _USER_AGENT,
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.read()
    except HTTPError as exc:
        raise OnlineProviderError(f"在线行情请求失败: HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise OnlineProviderError(f"在线行情网络请求失败: {exc}") from exc


def _reject_failed_payload(payload: Mapping[str, object]) -> Mapping[str, object]:
    """Reject East Money's soft failures (HTTP 200 with ``rc != 0``).

    Throttling and provider-side errors come back as ``{"rc": 102, "data": null}``
    rather than a failing status code.  Treating that as "no rows" makes a broken
    security list look like an empty market: the sync then finishes "successfully"
    with zero targets, zero bars and zero errors, which is indistinguishable from
    a genuinely up-to-date store.
    """

    raw = payload.get("rc")
    if raw is None or isinstance(raw, bool):
        return payload
    if not isinstance(raw, (int, float, str, bytes, bytearray)):
        return payload
    try:
        code = int(raw)
    except (TypeError, ValueError):
        return payload
    if code != 0:
        raise OnlineProviderError(f"在线行情接口返回错误码 rc={code}")
    return payload


def _mapping_payload(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            value = bytes(value).decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise OnlineDataError("在线行情响应不是有效 UTF-8") from exc
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise OnlineDataError("在线行情响应为空")
        if not text.startswith("{") and "=" in text:
            text = text.split("=", 1)[1].strip().rstrip(";")
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise OnlineDataError("在线行情响应不是有效 JSON") from exc
        if isinstance(decoded, Mapping):
            return decoded
    raise OnlineDataError("在线行情响应格式无效")


def _catalog_url_candidates(url: str) -> list[str]:
    """Return the given catalog URL followed by the same path on peer hosts."""

    split = urlsplit(url)
    hosts: list[str] = [split.netloc]
    hosts.extend(host for host in _EASTMONEY_CATALOG_HOSTS if host != split.netloc)
    return [
        urlunsplit((split.scheme, host, split.path, split.query, split.fragment)) for host in hosts
    ]


def _should_fetch_next_page(total: int, fetched: int, page_rows: int, page_size: int) -> bool:
    """Whether the catalog has more pages after the one just fetched.

    The server caps a page at 100 rows even when a larger ``pz`` was requested,
    so "page shorter than requested" no longer means "last page".  Trust the
    reported ``total`` whenever it is available and fall back to the page-size
    heuristic only when the provider omits it.
    """

    if total > 0:
        return fetched < total
    return page_rows >= page_size


def _mapping_value(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _mapping_rows(value: object) -> list[Mapping[str, object]]:
    if isinstance(value, Mapping):
        return [row for row in value.values() if isinstance(row, Mapping)]
    if isinstance(value, (list, tuple)):
        return [row for row in value if isinstance(row, Mapping)]
    return []


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, bytes, bytearray)):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _parse_compact_date(value: str) -> date:
    try:
        return datetime.strptime(value.replace("-", ""), "%Y%m%d").date()
    except ValueError as exc:
        raise OnlineDataError(f"在线行情日期格式无效: {value}") from exc


def _tencent_reports_shares(market: MarketCode, code: str) -> bool:
    """Whether Tencent already reports volume in shares instead of lots.

    Verified against Sina's share-denominated feed on 2026-09-17: every board
    (main, SME, ChiNext, ETF, index) answers in lots of 100 and needs scaling,
    while the STAR Market (688) comes back as raw shares, so scaling it would
    inflate the volume a hundredfold.
    """

    return market == "SH" and code.startswith("688")


class OnlineMarketClient:
    """Small project-owned HTTP client for the online stock data we require."""

    __version__ = "builtin-http-1"

    def __init__(
        self,
        transport: HttpTransport | None = None,
        *,
        page_size: int = _EASTMONEY_PAGE_SIZE,
    ) -> None:
        if not 1 <= page_size <= _EASTMONEY_PAGE_SIZE:
            raise ValueError("在线证券列表分页大小必须在 1 到 5000 之间")
        self._transport = transport or _http_transport
        self._page_size = page_size

    def fetch_instruments(self, *, timeout: float) -> pd.DataFrame:
        """Fetch the SH/SZ A-share list from the public quote list endpoint.

        The filter is narrowed server-side to the boards this project serves
        (SH main/star, SZ main/ChiNext) instead of pulling every SH/SZ security
        (``m:0,m:1`` is ~59k rows including bonds, funds and indices that the
        board filter discards afterwards).  East Money hard-caps a page at 100
        rows regardless of ``pz``, so the unfiltered form costs ~590 requests;
        the narrowed one is ~56.
        """

        page = 1
        rows: list[Mapping[str, object]] = []
        total = 0
        while page <= _MAX_CATALOG_PAGES:
            payload = self._request_catalog(
                _EASTMONEY_LIST_URL,
                {
                    "pn": str(page),
                    "pz": str(self._page_size),
                    "po": "1",
                    "np": "1",
                    "ut": _EASTMONEY_UT,
                    "fltt": "2",
                    "invt": "2",
                    "fid": "f12",
                    "fs": _EASTMONEY_STOCK_FILTER,
                    "fields": "f12,f14",
                },
                timeout,
            )
            data = _mapping_value(payload.get("data"))
            page_rows = _mapping_rows(data.get("diff"))
            if not page_rows:
                break
            rows.extend(page_rows)
            total = _nonnegative_int(data.get("total"))
            if not _should_fetch_next_page(total, len(rows), len(page_rows), self._page_size):
                break
            page += 1
        return pd.DataFrame(
            [{"代码": row.get("f12"), "名称": row.get("f14")} for row in rows],
            columns=["代码", "名称"],
        )

    def fetch_fund_instruments(self, *, timeout: float) -> pd.DataFrame:
        """Fetch ETF instruments from East Money's fund quote lists."""

        return self._fetch_catalog_by_filter(
            "b:MK0021,b:MK0022,b:MK0023,b:MK0024,b:MK0827",
            timeout=timeout,
        )

    def fetch_lof_instruments(self, *, timeout: float) -> pd.DataFrame:
        """Fetch LOF instruments from East Money's dedicated quote lists."""

        return self._fetch_catalog_by_filter(
            "b:MK0404,b:MK0405,b:MK0406,b:MK0407",
            timeout=timeout,
        )

    def fetch_index_instruments(self, *, timeout: float) -> pd.DataFrame:
        """Fetch the main SH/SZ index catalog with market prefixes."""

        return self._fetch_catalog_by_filter(
            "m:1 t:2,m:1 t:23,m:0 t:5,m:0 t:6,m:0 t:7,m:0 t:13",
            timeout=timeout,
            include_market=True,
        )

    def _fetch_catalog_by_filter(
        self,
        market_filter: str,
        *,
        timeout: float,
        include_market: bool = False,
    ) -> pd.DataFrame:
        page = 1
        rows: list[Mapping[str, object]] = []
        while page <= _MAX_CATALOG_PAGES:
            payload = self._request_catalog(
                _EASTMONEY_FUND_LIST_URL,
                {
                    "pn": str(page),
                    "pz": str(self._page_size),
                    "po": "1",
                    "np": "1",
                    "ut": _EASTMONEY_UT,
                    "fltt": "2",
                    "invt": "2",
                    "fid": "f12",
                    "fs": market_filter,
                    "fields": "f12,f13,f14",
                },
                timeout,
            )
            data = _mapping_value(payload.get("data"))
            page_rows = _mapping_rows(data.get("diff"))
            if not page_rows:
                break
            rows.extend(page_rows)
            total = _nonnegative_int(data.get("total"))
            if not _should_fetch_next_page(total, len(rows), len(page_rows), self._page_size):
                break
            page += 1
        result: list[dict[str, object]] = []
        for row in rows:
            code = _clean_code(row.get("f12"))
            if code is None:
                continue
            symbol = code
            if include_market:
                market_value = str(row.get("f13") or "").strip()
                market = "sh" if market_value == "1" or code.startswith(("0", "88", "99")) else "sz"
                symbol = f"{market}{code}"
            result.append({"代码": symbol, "名称": row.get("f14")})
        return pd.DataFrame(result, columns=["代码", "名称"])

    def fetch_daily_history(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        *,
        timeout: float,
    ) -> pd.DataFrame:
        """Fetch unadjusted daily bars from East Money."""

        normalized_symbol = _clean_code(symbol)
        if normalized_symbol is None:
            raise OnlineDataError(f"股票代码无效: {symbol}")
        # 沪市 ETF/LOF 以 5 开头（50/51/52/53/55/56/58），只按股票前缀判会发成
        # 深市 secid 而取不到数据；深市 ETF 以 15/16/17/18 开头。
        market = _market_for_stock_code(normalized_symbol) or _market_for_fund_code(
            normalized_symbol
        )
        market_code = "1" if (market or "SZ") == "SH" else "0"
        payload = self._request(
            _EASTMONEY_HISTORY_URL,
            {
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
                "ut": _EASTMONEY_HISTORY_UT,
                "klt": "101",
                "fqt": "0",
                "secid": f"{market_code}.{normalized_symbol}",
                "beg": start_date,
                "end": end_date,
            },
            timeout,
        )
        data = _mapping_value(payload.get("data"))
        raw_klines = data.get("klines")
        if raw_klines is None:
            return pd.DataFrame(
                columns=[
                    "日期",
                    "开盘",
                    "收盘",
                    "最高",
                    "最低",
                    "成交量",
                    "成交额",
                    "换手率",
                ]
            )
        if not isinstance(raw_klines, (list, tuple)):
            raise OnlineDataError("East Money 日线响应缺少有效 klines 列表")
        rows = [
            str(item).split(",") for item in raw_klines if isinstance(item, str) and item.strip()
        ]
        if any(len(row) < 11 for row in rows):
            raise OnlineDataError("East Money 日线响应字段数量不足")
        frame = pd.DataFrame(
            [row[:11] for row in rows],
            columns=[
                "日期",
                "开盘",
                "收盘",
                "最高",
                "最低",
                "成交量",
                "成交额",
                "振幅",
                "涨跌幅",
                "涨跌额",
                "换手率",
            ],
        )
        frame["日期"] = pd.to_datetime(frame["日期"], errors="coerce")
        for column in ("开盘", "收盘", "最高", "最低", "成交量", "成交额", "换手率"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        return frame[["日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额", "换手率"]]

    def fetch_daily_history_for_asset(
        self,
        symbol: str,
        asset_type: str,
        start_date: str,
        end_date: str,
        *,
        timeout: float,
    ) -> pd.DataFrame:
        """Fetch daily bars for stock, fund, LOF, or index symbols."""

        raw_symbol = str(symbol).strip().lower()
        if raw_symbol[:2] in {"sh", "sz"}:
            market = cast(MarketCode, raw_symbol[:2].upper())
            code = _clean_code(raw_symbol[2:])
        else:
            code = _clean_code(raw_symbol)
            market = _market_for_stock_code(code or "") or _market_for_fund_code(code or "") or "SZ"
        if code is None:
            raise OnlineDataError(f"证券代码无效: {symbol}")
        payload = self._request(
            _EASTMONEY_HISTORY_URL,
            {
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
                "ut": _EASTMONEY_HISTORY_UT,
                "klt": "101",
                "fqt": "0" if str(asset_type).strip().lower() in {"index", "指数"} else "1",
                "secid": f"{'1' if market == 'SH' else '0'}.{code}",
                "beg": start_date.replace("-", ""),
                "end": end_date.replace("-", ""),
            },
            timeout,
        )
        data = _mapping_value(payload.get("data"))
        raw_klines = data.get("klines")
        if not isinstance(raw_klines, (list, tuple)):
            return pd.DataFrame(columns=_EMPTY_COLUMNS)
        rows = [str(item).split(",") for item in raw_klines if isinstance(item, str)]
        if not rows or any(len(row) < 7 for row in rows):
            raise OnlineDataError("在线行情日线响应字段数量不足")
        result = pd.DataFrame(
            [row[:7] for row in rows],
            columns=["date", "open", "close", "high", "low", "volume", "amount"],
        )
        result["date"] = pd.to_datetime(result["date"], errors="coerce")
        for column in ("open", "close", "high", "low", "volume", "amount"):
            result[column] = pd.to_numeric(result[column], errors="coerce")
        return result

    def fetch_tencent_daily_history(
        self,
        market: MarketCode,
        code: str,
        start_date: str,
        end_date: str,
        *,
        timeout: float,
    ) -> pd.DataFrame:
        """Fetch raw daily bars from Tencent's year-bounded JSONP endpoint."""

        start = _parse_compact_date(start_date)
        end = _parse_compact_date(end_date)
        symbol = f"{market.lower()}{code}"
        parts: list[pd.DataFrame] = []
        for year in range(start.year, end.year + 1):
            payload = self._request(
                _TENCENT_HISTORY_URL,
                {
                    "_var": f"kline_day{year}",
                    "param": f"{symbol},day,{year}-01-01,{year + 1}-12-31,640,",
                    "r": "0.8205512681390605",
                },
                timeout,
            )
            data = _mapping_value(payload.get("data"))
            symbol_data = _mapping_value(data.get(symbol))
            raw_rows = symbol_data.get("day")
            if not isinstance(raw_rows, (list, tuple)):
                continue
            rows = [row for row in raw_rows if isinstance(row, (list, tuple)) and len(row) >= 9]
            if not rows:
                continue
            parts.append(
                pd.DataFrame(
                    [
                        [
                            row[0],
                            row[1],
                            row[2],
                            row[3],
                            row[4],
                            row[5],
                            row[7],
                            row[8],
                        ]
                        for row in rows
                    ],
                    columns=[
                        "date",
                        "open",
                        "close",
                        "high",
                        "low",
                        "volume",
                        "turnover",
                        "amount",
                    ],
                )
            )
        if not parts:
            return pd.DataFrame(
                columns=["date", "open", "close", "high", "low", "volume", "turnover", "amount"]
            )
        frame = pd.concat(parts, ignore_index=True)
        dates = pd.to_datetime(frame["date"], errors="coerce")
        for column in ("open", "close", "high", "low", "volume", "turnover", "amount"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        # 腾讯对科创板直接给"股"，其余品种（含 ETF 和指数）给的都是"手"。
        # 成交额一律是万元、换手率一律是百分数，这两项跟板块无关。旧实现把三者
        # 绑在同一个前缀判断里，于是被 "sz000" 误伤的深市主板成交量少算 100
        # 倍，科创板与指数的成交额少算 1 万倍。
        if not _tencent_reports_shares(market, code):
            frame["volume"] = frame["volume"] * 100
        frame["turnover"] = frame["turnover"] / 100
        frame["amount"] = frame["amount"] * 10_000
        frame["date"] = dates.dt.date
        frame = frame.loc[(dates.dt.date >= start) & (dates.dt.date <= end)]
        return frame.reset_index(drop=True)

    def _request(
        self,
        url: str,
        params: Mapping[str, object],
        timeout: float,
    ) -> Mapping[str, object]:
        if timeout <= 0:
            raise ValueError("在线行情请求超时时间必须大于 0")
        return _reject_failed_payload(_mapping_payload(self._transport(url, params, timeout)))

    def _request_catalog(
        self,
        url: str,
        params: Mapping[str, object],
        timeout: float,
    ) -> Mapping[str, object]:
        """Fetch one catalog page, retrying provider-side failures across hosts.

        East Money answers throttling two different ways: HTTP 200 with
        ``rc != 0`` / ``data: null`` (now rejected by ``_reject_failed_payload``)
        and a plain dropped connection.  Either one used to surface as an empty
        security list, which made the sync report a clean success with zero
        targets.

        Throttling is tracked per host, and one ``push2`` node being closed does
        not mean the others are, so each retry rotates to the next candidate
        host.

        A full catalog is dozens of pages and every page has to succeed, so a
        fixed small attempt count is not enough: at a 50% per-request failure
        rate five attempts leave roughly a one-in-five chance that *some* page
        gives up and aborts the whole run.  Each page therefore retries until a
        time budget is spent instead of a fixed number of tries.
        """

        candidates = _catalog_url_candidates(url)
        deadline = time.monotonic() + _CATALOG_RETRY_SECONDS
        last_error: OnlineProviderError | None = None
        attempt = 0
        while True:
            candidate = candidates[attempt % len(candidates)]
            try:
                return self._request(candidate, params, timeout)
            except OnlineProviderError as exc:
                last_error = exc
                attempt += 1
                if attempt >= _CATALOG_MAX_ATTEMPTS or time.monotonic() >= deadline:
                    break
                self._sleep_catalog_retry(attempt)
        assert last_error is not None
        raise last_error

    def _sleep_catalog_retry(self, attempt: int) -> None:
        delay = min(
            _CATALOG_RETRY_BASE_SECONDS * (2 ** (attempt - 1)),
            _CATALOG_RETRY_MAX_SECONDS,
        )
        time.sleep(delay)


def _default_module() -> OnlineModule:
    return OnlineMarketClient()


class OnlineMarketSync:
    """Synchronize SH/SZ stock data through the project-owned store contract."""

    def __init__(
        self,
        store: DuckDbMarketDataStore | None = None,
        module_factory: OnlineModuleFactory | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: SleepFunction = time.sleep,
        request_interval_seconds: float = _REQUEST_INTERVAL_SECONDS,
    ) -> None:
        if request_interval_seconds < 0:
            raise ValueError("在线行情请求间隔不能小于 0")
        self._store = store or DuckDbMarketDataStore()
        self._module_factory = module_factory or _default_module
        self._clock = clock or (lambda: datetime.now(_SHANGHAI_TZ))
        self._sleep = sleep
        self._rate_limiter = _RateLimiter(request_interval_seconds, sleep)

    def supports_process_staging(self) -> bool:
        """Return whether the provider can be reconstructed safely in a child process."""

        return self._module_factory is _default_module

    def prepare_stage_plan(self, config: MarketSyncConfig) -> ProviderStagePlan:
        """Refresh the universe and build an immutable download plan in the parent."""

        as_of = self._clock()
        selection, new_instruments, existing_instruments = self._prepare_targets(config)
        existing_by_key = self._latest_existing_bars(selection.targets)
        stage_targets: list[StageTarget] = []
        for market, code, instrument_type, board, name in selection.targets:
            snapshot = existing_by_key.get((market, code), _ExistingBars(pd.DataFrame()))
            stage_targets.append(
                StageTarget(
                    provider="eastmoney",
                    market=market,
                    code=code,
                    instrument_type=instrument_type,
                    board=board,
                    name=name,
                    requested_ranges=tuple(
                        _requested_ranges(snapshot, config, _as_shanghai(as_of).date())
                    ),
                    existing_first_date=snapshot.first_date,
                    existing_last_date=snapshot.last_date,
                    existing_bar_count=snapshot.bar_count,
                    existing_latest_rows=serialize_latest_rows(snapshot.latest),
                )
            )
        return ProviderStagePlan(
            provider="eastmoney",
            targets=tuple(stage_targets),
            listed_instruments=selection.listed_instruments,
            filtered_instruments=selection.filtered_instruments,
            new_instruments=new_instruments,
            existing_instruments=existing_instruments,
            target_source=selection.target_source,
            provider_version=self.provider_version(),
            instrument_list_fingerprint=selection.fingerprint,
            as_of=as_of,
        )

    def fetch_stage_target(
        self,
        target: StageTarget,
        config: MarketSyncConfig,
        *,
        abort_check: AbortCheck | None = None,
        rate_gate: Any | None = None,
    ) -> StageTargetResult:
        """Fetch one planned target without opening or modifying the main store."""

        existing = _ExistingBars(
            latest=latest_frame_from_rows(target.existing_latest_rows),
            first_date=target.existing_first_date,
            last_date=target.existing_last_date,
            bar_count=target.existing_bar_count,
        )
        fetched = self._fetch_target(
            self._module_factory(),
            cast(MarketCode, target.market),
            target.code,
            cast(InstrumentType, target.instrument_type),
            cast(InstrumentBoard, target.board),
            target.name,
            config,
            existing=existing,
            requested_ranges=target.requested_ranges,
            abort_check=abort_check,
            rate_gate=rate_gate,
        )
        if fetched.frame.empty:
            if existing.latest.empty:
                raise OnlineDataError(f"在线行情 {target.code} 没有可用日线")
            return StageTargetResult(
                status="unchanged",
                frame=pd.DataFrame(),
                range_start=(
                    target.existing_first_date.isoformat()
                    if target.existing_first_date is not None
                    else None
                ),
                range_end=(
                    target.existing_last_date.isoformat()
                    if target.existing_last_date is not None
                    else None
                ),
                retries=fetched.retries,
                history_source=(
                    "already_complete" if config.full_history else fetched.history_source
                ),
            )
        if (
            not config.full_history
            and not existing.latest.empty
            and not _has_new_data(existing.latest, fetched.frame)
        ):
            unchanged = _unchanged_result(
                existing,
                retries=fetched.retries,
                history_source=fetched.history_source,
            )
            return StageTargetResult(
                status="unchanged",
                frame=pd.DataFrame(),
                written_bars=0,
                range_start=unchanged.range_start,
                range_end=unchanged.range_end,
                retries=unchanged.retries,
                history_source=unchanged.history_source,
            )
        range_start, range_end = _frame_date_range(fetched.frame)
        return StageTargetResult(
            status="updated",
            frame=fetched.frame,
            written_bars=len(fetched.frame),
            range_start=range_start,
            range_end=range_end,
            retries=fetched.retries,
            history_source=fetched.history_source,
        )

    def provider_version(self) -> str | None:
        try:
            module = self._module_factory()
        except OnlineProviderUnavailable:
            return None
        version = getattr(module, "__version__", None)
        if isinstance(version, str) and version.strip():
            return version.strip()
        return None

    def provider_capabilities(self) -> dict[str, object]:
        try:
            version = self.provider_version()
            self._module_factory()
        except OnlineProviderUnavailable as exc:
            return {
                "provider": "eastmoney",
                "available": False,
                "version": None,
                "error": str(exc),
                "instrument_types": [],
                "boards": [],
            }
        return {
            "provider": "eastmoney",
            "available": True,
            "version": version,
            "instrument_types": ["stock", "fund"],
            "boards": sorted(_STOCK_BOARDS | _ONLINE_FUND_BOARDS),
            "history_sources": [
                "eastmoney",
                "tencent",
            ],
            "fallback_policy": "East Money → Tencent",
            "adjustments": ["raw"],
            "history": "full_or_incremental",
        }

    def discover_instruments(
        self,
        config: MarketSyncConfig,
        *,
        detail_callback: DetailCallback | None = None,
    ) -> InstrumentDiscoveryReport:
        _report_detail(
            detail_callback,
            {
                "phase": "instrument_discovery",
                "provider": "eastmoney",
                "current_status": "fetching",
                "range_label": "正在刷新在线行情证券列表…",
            },
        )
        selection, new_instruments, existing_instruments = self._prepare_targets(config)
        report = InstrumentDiscoveryReport(
            total_candidates=len(selection.targets),
            listed_instruments=selection.listed_instruments,
            filtered_instruments=selection.filtered_instruments,
            new_instruments=new_instruments,
            existing_instruments=existing_instruments,
            target_source=selection.target_source,
            provider="eastmoney",
            provider_version=self.provider_version(),
            instrument_list_fingerprint=selection.fingerprint,
        )
        _report_detail(
            detail_callback,
            {
                "phase": "instrument_discovery",
                "provider": "eastmoney",
                "current_status": "completed",
                "discovery_summary": report.to_dict(),
            },
        )
        return report

    def sync(
        self,
        config: MarketSyncConfig,
        progress_callback: ProgressCallback | None = None,
        *,
        detail_callback: DetailCallback | None = None,
        abort_check: AbortCheck | None = None,
    ) -> MarketSyncReport:
        if abort_check is not None and abort_check():
            raise MarketDataCancelledError("行情同步已取消")
        selection, new_instruments, existing_instruments = self._prepare_targets(config)
        if abort_check is not None and abort_check():
            raise MarketDataCancelledError("行情同步已取消")
        targets = selection.targets
        total = len(targets)
        failure_reasons: Counter[str] = Counter()
        processed = 0
        updated_files = 0
        unchanged_files = 0
        written_bars = 0
        history_sources: Counter[str] = Counter()
        _report_detail(
            detail_callback,
            {
                "phase": "instrument_discovery",
                "provider": "eastmoney",
                "current_status": "completed",
                "discovery_summary": {
                    "total_candidates": total,
                    "listed_instruments": selection.listed_instruments,
                    "filtered_instruments": selection.filtered_instruments,
                    "new_instruments": new_instruments,
                    "existing_instruments": existing_instruments,
                    "target_source": selection.target_source,
                    "provider": "eastmoney",
                },
            },
        )
        if progress_callback is not None:
            progress_callback(0, total)
        if targets:
            existing_by_key = self._latest_existing_bars(targets)
            batches = _partition_targets(targets, config.sync_workers)
            progress_lock = Lock()
            progress_count = 0

            def on_target_done() -> None:
                nonlocal progress_count
                with progress_lock:
                    progress_count += 1
                    current = progress_count
                if progress_callback is not None:
                    progress_callback(current, total)

            with ThreadPoolExecutor(
                max_workers=len(batches),
                thread_name_prefix="eastmoney-sync",
            ) as executor:
                futures = {
                    executor.submit(
                        self._sync_batch,
                        batch,
                        config,
                        existing_by_key,
                        on_target_done,
                        detail_callback,
                        abort_check,
                    ): batch
                    for batch in batches
                }
                for future in as_completed(futures):
                    batch_report = future.result()
                    processed += batch_report.processed
                    updated_files += batch_report.updated_files
                    unchanged_files += batch_report.unchanged_files
                    written_bars += batch_report.written_bars
                    failure_reasons.update(batch_report.failure_reasons)
                    history_sources.update(batch_report.history_sources)
        now = self._clock().isoformat()
        self._store.set_meta("last_online_sync_at", now)
        self._store.set_meta("last_online_provider", "eastmoney")
        version = self.provider_version()
        if version is not None:
            self._store.set_meta("last_online_provider_version", version)
        return MarketSyncReport(
            total_candidates=total,
            processed=processed,
            updated_files=updated_files,
            unchanged_files=unchanged_files,
            written_bars=written_bars,
            errors=sum(failure_reasons.values()),
            failure_reasons=dict(failure_reasons),
            listed_instruments=selection.listed_instruments,
            filtered_instruments=selection.filtered_instruments,
            new_instruments=new_instruments,
            existing_instruments=existing_instruments,
            target_source=selection.target_source,
            provider="eastmoney",
            provider_version=version,
            instrument_list_fingerprint=selection.fingerprint,
            history_sources=dict(history_sources),
            execution_mode="thread_pool_direct",
            worker_count=min(config.sync_workers, total) if total else 0,
        )

    def fetch_daily_bars(
        self,
        code: str,
        *,
        market: MarketCode | None = None,
        start_date: date,
        end_date: date,
        timeout: float,
    ) -> pd.DataFrame:
        frame, _retries, _source = self._fetch_with_retries(
            self._module_factory(),
            market=market or _market_for_stock_code(code) or "SZ",
            code=code,
            start_date=start_date,
            end_date=end_date,
            timeout=timeout,
        )
        return frame

    def _prepare_targets(
        self,
        config: MarketSyncConfig,
    ) -> tuple[_TargetSelection, int, int]:
        wanted = _online_supported_types(config)
        if not wanted:
            return _empty_selection("eastmoney_list"), 0, 0
        if config.prefer_store_targets:
            stored = self._store.list_instruments(
                market=None if config.universe == "all" else config.universe.upper(),
                instrument_types=wanted,
                boards=config.boards,
            )
            selection = _selection_from_stored(stored, instrument_types=wanted)
        else:
            selection = self._selection_from_online_catalogs(config, wanted)
        selection = replace(selection, fingerprint=_fingerprint_targets(selection.targets))
        if selection.fingerprint is not None:
            self._store.set_meta("instrument_list_fingerprint", selection.fingerprint)
        names: dict[tuple[str, str], str | None] = {
            (market, code): name
            for market, code, _instrument_type, _board, name in selection.targets
            if name is not None
        }
        if names:
            self._store.update_instrument_names(names)
        refs = [
            InstrumentRef(
                market=market,
                code=code,
                instrument_type=instrument_type,
                board=board,
                name=name,
            )
            for market, code, instrument_type, board, name in selection.targets
        ]
        new_instruments = self._store.ensure_instruments(refs) if refs else 0
        return selection, new_instruments, max(len(refs) - new_instruments, 0)

    def _selection_from_online_catalogs(
        self,
        config: MarketSyncConfig,
        wanted: tuple[InstrumentType, ...],
    ) -> _TargetSelection:
        """Fetch one East Money catalog per requested instrument kind.

        Stocks and funds live in different quote endpoints, so a mixed request
        has to walk both and merge the results by (market, code).
        """

        module = self._module_factory()
        selections: list[_TargetSelection] = []
        if "stock" in wanted:
            selections.append(
                _selection_from_eastmoney_list(
                    module.fetch_instruments(timeout=config.timeout),
                    config,
                    instrument_type="stock",
                )
            )
        if "fund" in wanted:
            selections.append(
                _selection_from_eastmoney_list(
                    module.fetch_fund_instruments(timeout=config.timeout),
                    config,
                    instrument_type="fund",
                )
            )
        return _merge_selections(selections)

    def _latest_existing_bars(
        self,
        targets: list[OnlineTarget],
    ) -> dict[tuple[str, str], _ExistingBars]:
        refs = [
            InstrumentRef(
                market=market,
                code=code,
                instrument_type=instrument_type,
                board=board,
                name=name,
            )
            for market, code, instrument_type, board, name in targets
        ]
        stored_by_key = {
            (ref.market, ref.code): ref
            for ref in self._store.list_instruments(
                include_missing=True,
                # Restrict to the kinds actually targeted, but never assume the
                # targets are all stocks: a fund run must see its stored funds.
                instrument_types=tuple(sorted({target[2] for target in targets})) or None,
            )
        }
        latest = self._store.read_latest_bars(refs, count=2, include_provisional=True)
        latest_by_key = (
            {
                (str(market), str(code)): group.reset_index(drop=True)
                for (market, code), group in latest.groupby(["market", "code"], sort=False)
            }
            if not latest.empty
            else {}
        )
        snapshots: dict[tuple[str, str], _ExistingBars] = {}
        for market, code, _instrument_type, _board, _name in targets:
            key = (market, code)
            stored = stored_by_key.get(key)
            latest_frame = latest_by_key.get(key, pd.DataFrame())
            snapshots[key] = _ExistingBars(
                latest=latest_frame,
                first_date=(stored.first_date if stored is not None else None)
                or _first_date(latest_frame),
                last_date=(stored.last_date if stored is not None else None)
                or _latest_date(latest_frame),
                bar_count=(stored.bar_count if stored is not None else None) or len(latest_frame),
            )
        return snapshots

    def _sync_batch(
        self,
        batch: list[OnlineTarget],
        config: MarketSyncConfig,
        existing_by_key: Mapping[tuple[str, str], _ExistingBars],
        target_done: Callable[[], None],
        detail_callback: DetailCallback | None,
        abort_check: AbortCheck | None,
    ) -> _BatchReport:
        failure_reasons: Counter[str] = Counter()
        processed = 0
        updated_files = 0
        unchanged_files = 0
        written_bars = 0
        history_sources: Counter[str] = Counter()
        module = self._module_factory()
        for market, code, instrument_type, board, name in batch:
            if abort_check is not None and abort_check():
                raise MarketDataCancelledError("行情同步已取消")
            _report_detail(
                detail_callback,
                _instrument_detail(
                    market,
                    code,
                    name,
                    status="fetching",
                    range_label=(
                        "正在补齐缺失历史日线…" if config.full_history else "正在读取最新日线…"
                    ),
                )
                | {"provider": "eastmoney", "retry": 0},
            )
            try:
                sync_result = self._sync_one(
                    module,
                    market,
                    code,
                    instrument_type,
                    board,
                    name,
                    config,
                    existing=existing_by_key.get((market, code), _ExistingBars(pd.DataFrame())),
                )
                written = sync_result.written_bars
                _report_detail(
                    detail_callback,
                    _instrument_detail(
                        market,
                        code,
                        name,
                        status="updated" if written > 0 else "unchanged",
                        bar_count=sync_result.bar_count,
                        range_start=sync_result.range_start,
                        range_end=sync_result.range_end,
                        retry=sync_result.retries,
                    )
                    | {
                        "provider": "eastmoney",
                        "history_source": sync_result.history_source,
                    },
                )
                if written > 0:
                    updated_files += 1
                    written_bars += written
                else:
                    unchanged_files += 1
                history_sources[sync_result.history_source] += 1
            except MarketDataCancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - isolate one instrument
                reason = str(exc).strip()[:200] or type(exc).__name__
                failure_reasons[reason] += 1
                _report_detail(
                    detail_callback,
                    _instrument_detail(
                        market,
                        code,
                        name,
                        status="error",
                        error=reason,
                    )
                    | {"provider": "eastmoney"},
                )
            processed += 1
            target_done()
        return _BatchReport(
            processed=processed,
            updated_files=updated_files,
            unchanged_files=unchanged_files,
            written_bars=written_bars,
            errors=sum(failure_reasons.values()),
            failure_reasons=dict(failure_reasons),
            history_sources=dict(history_sources),
        )

    def _sync_one(
        self,
        module: OnlineModule,
        market: MarketCode,
        code: str,
        instrument_type: InstrumentType,
        board: InstrumentBoard,
        name: str | None,
        config: MarketSyncConfig,
        *,
        existing: _ExistingBars,
    ) -> _SyncResult:
        fetched = self._fetch_target(
            module,
            market,
            code,
            instrument_type,
            board,
            name,
            config,
            existing=existing,
        )
        if fetched.frame.empty:
            if existing.latest.empty:
                raise OnlineDataError(f"在线行情 {code} 没有可用日线")
            return _unchanged_result(
                existing,
                retries=fetched.retries,
                history_source=(
                    "already_complete" if config.full_history else fetched.history_source
                ),
            )
        normalized = fetched.frame
        range_start, range_end = _frame_date_range(normalized)
        if (
            not config.full_history
            and not existing.latest.empty
            and not _has_new_data(existing.latest, normalized)
        ):
            return _unchanged_result(
                existing,
                retries=fetched.retries,
                history_source=fetched.history_source,
            )
        written, _updated = self._store.upsert_online_bars(normalized, provider="eastmoney")
        return _SyncResult(
            written_bars=written,
            bar_count=len(normalized),
            range_start=range_start,
            range_end=range_end,
            retries=fetched.retries,
            history_source=fetched.history_source,
        )

    def _fetch_target(
        self,
        module: OnlineModule,
        market: MarketCode,
        code: str,
        instrument_type: InstrumentType,
        board: InstrumentBoard,
        name: str | None,
        config: MarketSyncConfig,
        *,
        existing: _ExistingBars,
        requested_ranges: Sequence[tuple[date, date]] | None = None,
        abort_check: AbortCheck | None = None,
        rate_gate: Any | None = None,
    ) -> _FetchedTarget:
        ranges = list(
            requested_ranges
            if requested_ranges is not None
            else _requested_ranges(existing, config, _as_shanghai(self._clock()).date())
        )
        if not ranges:
            return _FetchedTarget(pd.DataFrame(), 0, "already_complete")
        normalized_parts: list[pd.DataFrame] = []
        history_sources: list[str] = []
        retries = 0
        for start_date, requested_end in ranges:
            if abort_check is not None and abort_check():
                raise MarketDataCancelledError("行情同步已取消")
            frame, range_retries, history_source = self._fetch_with_retries(
                module,
                market=market,
                code=code,
                start_date=start_date,
                end_date=requested_end,
                timeout=config.timeout,
                abort_check=abort_check,
                rate_gate=rate_gate,
            )
            retries += range_retries
            if frame.empty:
                continue
            normalized = _normalize_online_frame(
                frame,
                market=market,
                code=code,
                instrument_type=instrument_type,
                board=board,
                name=name,
                now=self._clock(),
            )
            normalized = _clip_to_range(normalized, start_date, requested_end)
            if normalized.empty:
                continue
            normalized_parts.append(normalized)
            history_sources.append(history_source)
        if not normalized_parts:
            return _FetchedTarget(pd.DataFrame(), retries, "empty")
        normalized = (
            pd.concat(normalized_parts, ignore_index=True)
            .sort_values("date", kind="stable")
            .drop_duplicates(subset=["date"], keep="last")
            .reset_index(drop=True)
        )
        return _FetchedTarget(
            normalized,
            retries,
            _combine_history_sources(history_sources),
        )

    def _fetch_with_retries(
        self,
        module: OnlineModule,
        *,
        market: MarketCode,
        code: str,
        start_date: date,
        end_date: date,
        timeout: float,
        abort_check: AbortCheck | None = None,
        rate_gate: Any | None = None,
    ) -> tuple[pd.DataFrame, int, str]:
        date_start = start_date.strftime("%Y%m%d")
        date_end = end_date.strftime("%Y%m%d")
        source_requests: tuple[tuple[str, str, dict[str, object]], ...] = (
            (
                "eastmoney",
                "fetch_daily_history",
                {
                    "symbol": code,
                    "start_date": date_start,
                    "end_date": date_end,
                    "timeout": timeout,
                },
            ),
            (
                "tencent",
                "fetch_tencent_daily_history",
                {
                    "market": market,
                    "code": code,
                    "start_date": date_start,
                    "end_date": date_end,
                    "timeout": timeout,
                },
            ),
        )
        errors: list[str] = []
        available = False
        retries = 0
        for source_name, method_name, kwargs in source_requests:
            method = getattr(module, method_name, None)
            if not callable(method):
                continue
            available = True
            for attempt in range(_MAX_RETRIES + 1):
                if abort_check is not None and abort_check():
                    raise MarketDataCancelledError("行情同步已取消")
                if rate_gate is None:
                    self._rate_limiter.wait()
                else:
                    rate_gate.wait("eastmoney")
                try:
                    frame = method(**kwargs)
                    if not isinstance(frame, pd.DataFrame):
                        raise OnlineDataError(f"{source_name} 响应不是 DataFrame")
                    if rate_gate is not None:
                        rate_gate.report(success=True, provider="eastmoney")
                    if frame.empty:
                        break
                    return frame, retries, source_name
                except OnlineDataError as exc:
                    if rate_gate is not None:
                        rate_gate.report(success=False, provider="eastmoney")
                    errors.append(f"{source_name}: {exc}")
                    break
                except Exception as exc:  # noqa: BLE001 - upstream errors are retriable
                    if rate_gate is not None:
                        rate_gate.report(
                            success=False,
                            throttled=is_throttling_error(exc),
                            provider="eastmoney",
                        )
                    errors.append(f"{source_name}: {exc}")
                    if attempt == _MAX_RETRIES:
                        break
                    retries += 1
                    self._sleep(_RETRY_BASE_SECONDS * (2**attempt))
        if not available:
            raise OnlineProviderError("在线行情未提供可用的股票日线接口")
        if errors:
            detail = "; ".join(errors[-3:])
            raise OnlineProviderError(f"在线行情获取 {code} 日线失败: {detail}")
        return pd.DataFrame(), retries, "empty"


def _requested_ranges(
    existing: _ExistingBars,
    config: MarketSyncConfig,
    end_date: date,
) -> list[tuple[date, date]]:
    """Build only the date ranges that are absent from the local store."""

    if not config.full_history:
        latest_date = _latest_date(existing.latest)
        if latest_date is None:
            return [(_FULL_HISTORY_START, end_date)]
        start_date = max(
            _FULL_HISTORY_START,
            latest_date - timedelta(days=_INCREMENTAL_OVERLAP_DAYS),
        )
        return [(start_date, end_date)] if start_date <= end_date else []

    if existing.first_date is None or existing.last_date is None:
        return [(_FULL_HISTORY_START, end_date)]

    ranges: list[tuple[date, date]] = []
    if existing.first_date > _FULL_HISTORY_START:
        ranges.append((_FULL_HISTORY_START, existing.first_date - timedelta(days=1)))
    if existing.last_date < end_date:
        ranges.append((existing.last_date + timedelta(days=1), end_date))
    return ranges


def _clip_to_range(frame: pd.DataFrame, start_date: date, end_date: date) -> pd.DataFrame:
    if frame.empty:
        return frame
    dates = pd.to_datetime(frame["date"], errors="coerce").dt.date
    return frame.loc[(dates >= start_date) & (dates <= end_date)].reset_index(drop=True)


def _unchanged_result(
    existing: _ExistingBars,
    *,
    retries: int = 0,
    history_source: str,
) -> _SyncResult:
    first_date = existing.first_date or _first_date(existing.latest)
    last_date = existing.last_date or _latest_date(existing.latest)
    return _SyncResult(
        bar_count=existing.bar_count or len(existing.latest),
        range_start=first_date.isoformat() if first_date is not None else None,
        range_end=last_date.isoformat() if last_date is not None else None,
        retries=retries,
        history_source=history_source,
    )


def _combine_history_sources(sources: list[str]) -> str:
    unique = list(dict.fromkeys(sources))
    return "+".join(unique) if unique else "empty"


def _scope_can_include(
    config: MarketSyncConfig,
    *,
    instrument_type: InstrumentType,
    boards: frozenset[str],
) -> bool:
    """Whether the requested scope still overlaps one provider capability."""

    if config.instrument_types is not None and instrument_type not in config.instrument_types:
        return False
    return config.boards is None or bool(set(config.boards) & boards)


# The online East Money catalogs carry these instrument kinds. Bond rows exist in
# the quote lists but the local store has no bar source for them, so they stay
# out of the downloadable set on purpose.
_ONLINE_FUND_BOARDS = frozenset({"fund"})
_ONLINE_SUPPORTED_TYPES: tuple[tuple[InstrumentType, frozenset[str]], ...] = (
    ("stock", _STOCK_BOARDS),
    ("fund", _ONLINE_FUND_BOARDS),
)


def _online_supported_types(config: MarketSyncConfig) -> tuple[InstrumentType, ...]:
    if config.instrument_types is None:
        # An unscoped request keeps resolving to stocks only. Funds live behind
        # a second catalog request, so folding them into every unscoped run
        # would silently multiply the download volume for callers that never
        # asked for ETFs.
        return ("stock",)
    return tuple(
        instrument_type
        for instrument_type, boards in _ONLINE_SUPPORTED_TYPES
        if _scope_can_include(config, instrument_type=instrument_type, boards=boards)
    )


def _empty_selection(target_source: str) -> _TargetSelection:
    return _TargetSelection([], 0, 0, target_source)


def _selection_from_stored(
    stored: list[InstrumentRef],
    *,
    instrument_types: tuple[InstrumentType, ...],
) -> _TargetSelection:
    allowed = set(instrument_types)
    targets: list[OnlineTarget] = [
        (ref.market, ref.code, ref.instrument_type, ref.board, ref.name)
        for ref in stored
        if ref.instrument_type in allowed
    ]
    return _TargetSelection(
        targets=targets,
        listed_instruments=len(targets),
        filtered_instruments=0,
        target_source="eastmoney_store",
    )


def _selection_from_eastmoney_list(
    frame: pd.DataFrame,
    config: MarketSyncConfig,
    *,
    instrument_type: InstrumentType,
) -> _TargetSelection:
    """Turn one East Money catalog page into download targets of a single kind.

    ``instrument_type`` is the kind the caller asked this frame for. Fund codes
    live in ranges the stock helper rejects (51xxxx/15xxxx), so the market has
    to be resolved with the matching helper before classification.
    """

    if not isinstance(frame, pd.DataFrame):
        raise OnlineDataError("在线行情证券列表响应不是 DataFrame")
    if frame.empty:
        raise OnlineDataError("在线行情证券列表为空")
    code_column = _find_column(frame, ("代码", "code", "证券代码"))
    name_column = _find_column(frame, ("名称", "name", "证券简称"))
    if code_column is None:
        raise OnlineDataError("在线行情证券列表缺少代码字段")
    scope = InstrumentScope.from_values(
        universe=config.universe,
        instrument_types=(instrument_type,),
        boards=config.boards,
    )
    seen: set[tuple[MarketCode, str]] = set()
    listed: set[tuple[MarketCode, str]] = set()
    targets: list[OnlineTarget] = []
    # Keep the per-kind board gate: the East Money stock catalog also lists
    # B shares, which this channel does not serve.
    allowed_boards = _STOCK_BOARDS if instrument_type == "stock" else _ONLINE_FUND_BOARDS
    for row in frame.to_dict(orient="records"):
        code = _clean_code(row.get(code_column))
        if code is None:
            continue
        market = _market_for_online_code(code, instrument_type)
        if market is None:
            continue
        classified_type = classify_instrument(market, code)
        board = classify_board(market, code)
        if classified_type != instrument_type or board not in allowed_boards:
            continue
        key = (market, code)
        listed.add(key)
        name = _clean_name(row.get(name_column)) if name_column is not None else None
        if key in seen or not scope.matches(market, classified_type, board):
            continue
        seen.add(key)
        targets.append((market, code, classified_type, board, name))
    return _TargetSelection(
        targets=targets,
        listed_instruments=len(listed),
        filtered_instruments=max(len(listed) - len(targets), 0),
        target_source=f"eastmoney_{instrument_type}_list",
    )


def _market_for_online_code(code: str, instrument_type: InstrumentType) -> MarketCode | None:
    if instrument_type == "fund":
        return _market_for_fund_code(code)
    return _market_for_stock_code(code)


def _merge_selections(selections: list[_TargetSelection]) -> _TargetSelection:
    """Merge per-kind catalog selections, de-duplicating by (market, code)."""

    if not selections:
        return _empty_selection("eastmoney_list")
    if len(selections) == 1:
        return selections[0]
    targets: list[OnlineTarget] = []
    seen: set[tuple[MarketCode, str]] = set()
    for selection in selections:
        for target in selection.targets:
            key = (target[0], target[1])
            if key in seen:
                continue
            seen.add(key)
            targets.append(target)
    return _TargetSelection(
        targets=targets,
        listed_instruments=sum(item.listed_instruments for item in selections),
        filtered_instruments=sum(item.filtered_instruments for item in selections),
        target_source="eastmoney_mixed_list",
    )


def _fingerprint_targets(targets: list[OnlineTarget]) -> str:
    canonical = "\n".join(
        f"{market}|{code}|{instrument_type}|{board}|{name or ''}"
        for market, code, instrument_type, board, name in sorted(targets)
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _find_column(frame: pd.DataFrame, candidates: tuple[str, ...]) -> str | None:
    return next((candidate for candidate in candidates if candidate in frame.columns), None)


def _clean_code(value: object) -> str | None:
    if value is None:
        return None
    missing = pd.isna(value)
    if isinstance(missing, (bool, np.bool_)) and bool(missing):
        return None
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    if not re.fullmatch(r"\d{1,6}", text):
        return None
    return text.zfill(6)


def _clean_name(value: object) -> str | None:
    if value is None:
        return None
    missing = pd.isna(value)
    if isinstance(missing, (bool, np.bool_)) and bool(missing):
        return None
    text = str(value).strip()
    return text or None


def _market_for_stock_code(code: str) -> MarketCode | None:
    if code.startswith("6") or code.startswith("9"):
        return "SH"
    if code.startswith(("0", "2", "3")):
        return "SZ"
    # The broad online list can also contain Beijing A shares. The current
    # store and backtest contracts are SH/SZ-only, so leave them out explicitly.
    return None


def _market_for_fund_code(code: str) -> MarketCode | None:
    if code.startswith(("5", "6", "9")):
        return "SH"
    if code.startswith(("15", "16", "17", "18")):
        return "SZ"
    return None


def _normalize_online_frame(
    frame: pd.DataFrame,
    *,
    market: MarketCode,
    code: str,
    instrument_type: InstrumentType,
    board: InstrumentBoard,
    name: str | None,
    now: datetime,
) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise OnlineDataError("在线行情日线响应不是 DataFrame")
    if frame.empty:
        return pd.DataFrame(
            columns=[*_EMPTY_COLUMNS, "market", "code", "name", "instrument_type", "board"]
        )
    aliases = {
        "date": ("日期", "date"),
        "open": ("开盘", "open"),
        "high": ("最高", "high"),
        "low": ("最低", "low"),
        "close": ("收盘", "close"),
        "volume": ("成交量", "成交量(手)", "volume", "vol"),
        "amount": ("成交额", "amount"),
    }
    columns = {key: _find_column(frame, candidates) for key, candidates in aliases.items()}
    missing = [key for key, value in columns.items() if value is None]
    if missing:
        raise OnlineDataError(f"在线行情日线响应缺少字段: {', '.join(missing)}")
    normalized = frame.loc[:, [cast(str, columns[key]) for key in aliases]].copy()
    normalized.columns = list(aliases)
    normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce")
    if normalized["date"].isna().any():
        raise OnlineDataError("在线行情日线响应包含无效日期")
    for column in ("open", "high", "low", "close", "volume", "amount"):
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    values = normalized[["open", "high", "low", "close", "volume", "amount"]].to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise OnlineDataError("在线行情日线响应包含无效数值")
    if (normalized[["open", "high", "low", "close"]] <= 0).any().any():
        raise OnlineDataError("在线行情日线响应包含非正价格")
    if (normalized[["volume", "amount"]] < 0).any().any():
        raise OnlineDataError("在线行情日线响应包含负成交数据")
    if (
        (normalized["low"] > normalized[["open", "close"]].min(axis=1))
        | (normalized["high"] < normalized[["open", "close"]].max(axis=1))
    ).any():
        raise OnlineDataError("在线行情日线响应的高低价无法覆盖开收盘价")
    current = _as_shanghai(now)
    if (normalized["date"].dt.date > current.date()).any():
        raise OnlineDataError("在线行情日线响应包含未来日期")
    normalized["bar_status"] = normalized["date"].map(
        lambda value: (
            "provisional"
            if value.date() == current.date() and (current.hour, current.minute) < _MARKET_CLOSE
            else "completed"
        )
    )
    normalized = normalized.sort_values("date", kind="stable").drop_duplicates(
        subset=["date"], keep="last"
    )
    normalized["market"] = market
    normalized["code"] = code
    normalized["name"] = name
    normalized["instrument_type"] = instrument_type
    normalized["board"] = board
    return normalized.reset_index(drop=True)


def _latest_date(frame: pd.DataFrame) -> date | None:
    if frame.empty or "date" not in frame.columns:
        return None
    dates = pd.to_datetime(frame["date"], errors="coerce").dropna()
    return pd.Timestamp(dates.max()).date() if not dates.empty else None


def _first_date(frame: pd.DataFrame) -> date | None:
    if frame.empty or "date" not in frame.columns:
        return None
    dates = pd.to_datetime(frame["date"], errors="coerce").dropna()
    return pd.Timestamp(dates.min()).date() if not dates.empty else None


def _has_new_data(existing: pd.DataFrame, latest: pd.DataFrame) -> bool:
    if latest.empty:
        return False
    if existing.empty:
        return True
    existing_by_date = {
        pd.Timestamp(row.date).date(): row for row in existing.itertuples(index=False)
    }
    for row in latest.itertuples(index=False):
        current_date = pd.Timestamp(row.date).date()
        previous = existing_by_date.get(current_date)
        if previous is None:
            return True
        if row.bar_status != previous.bar_status:
            return True
        for column in ("open", "high", "low", "close", "volume", "amount"):
            if float(getattr(row, column)) != float(getattr(previous, column)):
                return True
    return False


def _frame_date_range(frame: pd.DataFrame) -> tuple[str | None, str | None]:
    if frame.empty or "date" not in frame.columns:
        return None, None
    dates = pd.to_datetime(frame["date"], errors="coerce").dropna()
    if dates.empty:
        return None, None
    return (
        pd.Timestamp(dates.iloc[0]).strftime("%Y-%m-%d"),
        pd.Timestamp(dates.iloc[-1]).strftime("%Y-%m-%d"),
    )


def _as_shanghai(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=_SHANGHAI_TZ)
    return value.astimezone(_SHANGHAI_TZ)


def _partition_targets(targets: list[OnlineTarget], worker_count: int) -> list[list[OnlineTarget]]:
    batch_count = min(max(worker_count, 1), len(targets))
    return [targets[index::batch_count] for index in range(batch_count)]


__all__ = [
    "OnlineDataError",
    "OnlineMarketClient",
    "OnlineMarketSync",
    "OnlineModule",
    "OnlineProviderError",
    "OnlineProviderUnavailable",
]
