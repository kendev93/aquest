"""Small standard-protocol TDX client for daily-bar synchronization."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TypeVar

import pandas as pd

from .commands import Command, GetSecurityBars, GetSecurityCount, GetSecurityList
from .errors import TdxConnectionError
from .transport import TdxConnection, ping_host
from .types import KlineCategory, Market, SecurityBar, SecurityInfo

_T = TypeVar("_T")
_DEFAULT_HOSTS = (
    "111.229.247.189",
    "150.158.160.2",
    "180.153.18.170",
    "124.71.187.122",
    "180.153.18.171",
    "119.147.212.81",
    "115.238.56.198",
    "218.75.126.9",
)
_RETRY_DELAYS = (0.1, 0.5, 1.0, 2.0)
_SECURITY_LIST_TIMEOUT_SECONDS = 60.0


def _hosts() -> list[str]:
    configured = os.getenv("TDX_KNOWN_HOSTS")
    if configured:
        values = [value.strip() for value in configured.split(",") if value.strip()]
        if values:
            return values
    return list(_DEFAULT_HOSTS)


def _port() -> int:
    return int(os.getenv("TDX_PORT") or "7709")


class TdxClient:
    """Synchronous standard TDX client with bounded reconnect/failover."""

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        timeout: float = 15.0,
        *,
        connection_factory: Callable[[str, int, float], TdxConnection] | None = None,
    ) -> None:
        self._hosts = _hosts()
        configured_host = os.getenv("TDX_HOST")
        self._host = host or configured_host or self._hosts[0]
        self._port = port or _port()
        self._timeout = timeout
        self._connection_factory = connection_factory or TdxConnection
        self._connection = self._connection_factory(self._host, self._port, self._timeout)

    @classmethod
    def from_best_host(cls, timeout: float = 15.0) -> TdxClient:
        hosts = _hosts()
        configured = os.getenv("TDX_HOST")
        if configured:
            return cls(configured, timeout=timeout)
        measured = cls.measure_hosts(timeout=min(timeout, 2.0))
        return cls(measured[0][0] if measured else hosts[0], timeout=timeout)

    @classmethod
    def measure_hosts(cls, timeout: float = 2.0) -> list[tuple[str, float]]:
        """Probe configured servers concurrently and return fastest-first results."""

        hosts = _hosts()
        port = _port()
        bounded_timeout = max(min(timeout, 5.0), 0.1)
        measured: list[tuple[str, float]] = []
        with ThreadPoolExecutor(
            max_workers=min(len(hosts), 8), thread_name_prefix="tdx-probe"
        ) as pool:
            futures = {pool.submit(ping_host, host, port, bounded_timeout): host for host in hosts}
            for future in as_completed(futures):
                try:
                    latency = future.result()
                except Exception:  # noqa: BLE001 - one unavailable host must not stop probing
                    continue
                if latency is not None:
                    measured.append((futures[future], latency))
        return sorted(measured, key=lambda item: item[1])

    def connect(self) -> None:
        self._connection.connect()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> TdxClient:
        self.connect()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _reconnect(self, host: str | None = None, *, timeout: float | None = None) -> None:
        self.close()
        if host is not None:
            self._host = host
        connection_timeout = timeout if timeout is not None else self._timeout
        self._connection = self._connection_factory(
            self._host,
            self._port,
            connection_timeout,
        )
        self.connect()

    def _execute(self, command: Command[_T], *, deadline: float | None = None) -> _T:
        last_error: TdxConnectionError | None = None
        for delay in (0.0, *_RETRY_DELAYS):
            if delay:
                remaining = _remaining(deadline)
                if remaining <= 0:
                    raise _request_timeout() from last_error
                time.sleep(min(delay, remaining))
            remaining = _remaining(deadline)
            if remaining <= 0:
                raise _request_timeout() from last_error
            _set_connection_timeout(self._connection, min(self._timeout, remaining))
            try:
                return self._connection.execute(command)
            except TdxConnectionError as exc:
                last_error = exc
                remaining = _remaining(deadline)
                if remaining <= 0:
                    raise _request_timeout() from exc
                try:
                    self._reconnect(timeout=min(self._timeout, remaining))
                except TdxConnectionError as reconnect_error:
                    last_error = reconnect_error
        current_index = self._hosts.index(self._host) if self._host in self._hosts else -1
        failover_hosts = self._hosts[current_index + 1 :] + self._hosts[:current_index]
        for host in failover_hosts:
            remaining = _remaining(deadline)
            if remaining <= 0:
                raise _request_timeout() from last_error
            try:
                self._reconnect(host, timeout=min(self._timeout, remaining))
                _set_connection_timeout(self._connection, min(self._timeout, remaining))
                return self._connection.execute(command)
            except TdxConnectionError as exc:
                last_error = exc
        if deadline is not None and _remaining(deadline) <= 0:
            raise _request_timeout() from last_error
        raise last_error or TdxConnectionError("TDX 请求失败")

    def get_security_count(self, market: Market, *, deadline: float | None = None) -> int:
        return self._execute(GetSecurityCount(market), deadline=deadline)

    def get_security_list(
        self,
        market: Market,
        start: int,
        *,
        deadline: float | None = None,
    ) -> pd.DataFrame:
        items = self._execute(GetSecurityList(market, start), deadline=deadline)
        return _security_list_frame(items)

    def get_security_list_all(self, pages: int | str = "all") -> pd.DataFrame:
        rows: list[dict[str, object]] = []
        page_limit = None if pages == "all" else max(int(pages), 0)
        deadline = time.monotonic() + _SECURITY_LIST_TIMEOUT_SECONDS
        for market in (Market.SH, Market.SZ):
            count = self.get_security_count(market, deadline=deadline)
            maximum = count if page_limit is None else min(count, page_limit * 1000)
            for start in range(0, maximum, 1000):
                try:
                    frame = self.get_security_list(market, start, deadline=deadline)
                except Exception as exc:  # noqa: BLE001 - a skipped page makes the list incomplete
                    raise TdxConnectionError(
                        f"证券列表分页获取失败: market={market.name}, start={start}"
                    ) from exc
                expected = min(1000, maximum - start)
                if len(frame) < expected:
                    raise TdxConnectionError(
                        "证券列表分页不完整: "
                        f"market={market.name}, start={start}, "
                        f"expected={expected}, received={len(frame)}"
                    )
                rows.extend(frame.to_dict(orient="records"))
        return pd.DataFrame(rows)

    def get_security_bars(
        self,
        market: Market,
        code: str,
        category: KlineCategory,
        start: int,
        count: int = 800,
    ) -> pd.DataFrame:
        if start < 0 or not 1 <= count <= 800:
            raise ValueError("TDX 日线分页参数无效")
        command = GetSecurityBars(
            market,
            code,
            category,
            start,
            count,
            is_index=_is_index_code(market, code),
        )
        bars = self._execute(command)
        if not bars:
            for host in self._hosts:
                if host == self._host:
                    continue
                try:
                    self._reconnect(host)
                    bars = self._connection.execute(command)
                except TdxConnectionError:
                    continue
                if bars:
                    break
        return _bars_frame(bars)


def _remaining(deadline: float | None) -> float:
    return float("inf") if deadline is None else deadline - time.monotonic()


def _request_timeout() -> TdxConnectionError:
    return TdxConnectionError(
        f"TDX 证券列表请求超时（超过 {_SECURITY_LIST_TIMEOUT_SECONDS:.0f} 秒）"
    )


def _set_connection_timeout(connection: TdxConnection, timeout: float) -> None:
    connection.set_timeout(max(timeout, 0.01))


def _security_list_frame(items: list[SecurityInfo]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "market": item.market.name,
                "code": item.code,
                "name": item.name,
                "volume_unit": item.volume_unit,
                "decimal_point": item.decimal_point,
                "pre_close": item.pre_close,
            }
            for item in items
        ]
    )


def _bars_frame(bars: list[SecurityBar]) -> pd.DataFrame:
    rows = [
        {
            "date": pd.Timestamp(year=bar.year, month=bar.month, day=bar.day),
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "amount": bar.amount,
        }
        for bar in bars
    ]
    return (
        pd.DataFrame(
            rows,
            columns=["date", "open", "high", "low", "close", "volume", "amount"],
        )
        .sort_values("date", kind="stable")
        .reset_index(drop=True)
    )


def _is_index_code(market: Market, code: str) -> bool:
    normalized = code.strip()
    if market is Market.SH:
        return normalized.startswith(("00", "88", "99"))
    return normalized.startswith("39")
