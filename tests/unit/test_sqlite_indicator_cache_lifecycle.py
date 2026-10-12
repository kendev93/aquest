from __future__ import annotations

import gc
import sqlite3
import sys
import threading
import warnings
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import pytest

from aquest.market_data.store import DuckDbMarketDataStore
from aquest.market_data.store_indicator_cache_sqlite import SqliteIndicatorCacheStore


@contextmanager
def _capture_cleanup_warnings() -> Iterator[tuple[list[warnings.WarningMessage], list[object]]]:
    unraisable: list[object] = []
    previous_hook = sys.unraisablehook
    sys.unraisablehook = unraisable.append
    try:
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always", ResourceWarning)
            yield captured, unraisable
    finally:
        sys.unraisablehook = previous_hook


def _assert_no_cleanup_warnings(
    captured: list[warnings.WarningMessage], unraisable: list[object]
) -> None:
    assert not [item for item in captured if issubclass(item.category, ResourceWarning)]
    assert not unraisable


def _store(path: Path, *, read_only: bool = False) -> DuckDbMarketDataStore:
    return DuckDbMarketDataStore(
        path,
        read_only=read_only,
        indicator_cache_backend="sqlite",
    )


def _write_and_read_cache(store: DuckDbMarketDataStore) -> list[dict[str, object]]:
    store.write_indicator_cache(
        market="SH",
        code="600000",
        timeframe="daily",
        price_basis="raw",
        formula_id="technical",
        formula_version="1",
        data_fingerprint="bars-v1",
        data_start=date(2024, 1, 2),
        data_end=date(2024, 1, 2),
        data_length=1,
        values_blob=b"values",
        signals_blob=b"signals",
    )
    return store.read_indicator_cache(
        market="SH",
        code="600000",
        timeframe="daily",
        price_basis="raw",
        formula_id="technical",
        formula_version="1",
        data_fingerprint="bars-v1",
    )


def test_store_gc_on_another_thread_closes_sqlite_cache_and_allows_reopen(tmp_path: Path) -> None:
    database_path = tmp_path / "market.duckdb"
    holder = [_store(database_path)]
    assert _write_and_read_cache(holder[0])[0]["values_blob"] == b"values"
    cached_connection = holder[0]._indicator_cache._connection()
    creator_thread = threading.get_ident()

    def discard_store() -> int:
        store = holder.pop()
        del store
        gc.collect()
        return threading.get_ident()

    with _capture_cleanup_warnings() as (captured, unraisable):
        with ThreadPoolExecutor(max_workers=1) as executor:
            collector_thread = executor.submit(discard_store).result()
        gc.collect()
        _assert_no_cleanup_warnings(captured, unraisable)

    assert collector_thread != creator_thread
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        cached_connection.execute("SELECT 1")

    reopened = _store(database_path)
    assert _write_and_read_cache(reopened)[0]["signals_blob"] == b"signals"
    reopened_connection = reopened._indicator_cache._connection()
    reopened_holder = [reopened]
    del reopened
    with _capture_cleanup_warnings() as (captured, unraisable):
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(discard_store_from, reopened_holder).result()
        gc.collect()
        _assert_no_cleanup_warnings(captured, unraisable)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        reopened_connection.execute("SELECT 1")


def discard_store_from(holder: list[DuckDbMarketDataStore]) -> None:
    store = holder.pop()
    del store
    gc.collect()


def test_cached_connections_are_reused_per_thread_and_closed_at_thread_exit(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path / "market.duckdb")
    store._indicator_cache._ensure_schema()
    main_connection = store._indicator_cache._connection()
    barrier = threading.Barrier(2)

    def observe_connection() -> tuple[int, sqlite3.Connection, int, int]:
        cache = store._indicator_cache
        first = cache._connection()
        first_id = id(first)
        barrier.wait(timeout=5)
        second = cache._connection()
        second.execute("SELECT 1").fetchone()
        return threading.get_ident(), first, first_id, id(second)

    with _capture_cleanup_warnings() as (captured, unraisable):
        with ThreadPoolExecutor(max_workers=2) as executor:
            observations = list(executor.map(lambda _index: observe_connection(), range(2)))
        gc.collect()
        _assert_no_cleanup_warnings(captured, unraisable)

    assert observations[0][0] != observations[1][0]
    assert observations[0][2] == observations[0][3]
    assert observations[1][2] == observations[1][3]
    assert observations[0][2] != observations[1][2]
    for _thread_id, connection, _first_id, _second_id in observations:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
    assert main_connection.execute("SELECT 1").fetchone() == (1,)
    assert store.indicator_cache_status()["total_entries"] == 0


def test_failed_connection_setup_closes_unpublished_sqlite_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path / "market.duckdb")
    cache = store._indicator_cache
    assert isinstance(cache, SqliteIndicatorCacheStore)
    cache._ensure_schema()
    real_connect = sqlite3.connect

    class FailingConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection
            self.closed = False

        def execute(self, sql: str, *parameters: object):
            if sql == "PRAGMA busy_timeout=30000":
                raise sqlite3.OperationalError("injected setup failure")
            return self.connection.execute(sql, parameters)

        def close(self) -> None:
            self.closed = True
            self.connection.close()

    failing: list[FailingConnection] = []

    def connect(*args: object, **kwargs: object) -> FailingConnection:
        connection = FailingConnection(real_connect(*args, **kwargs))
        failing.append(connection)
        return connection

    with monkeypatch.context() as patcher:
        patcher.setattr("aquest.market_data.store_indicator_cache_sqlite.sqlite3.connect", connect)
        with pytest.raises(sqlite3.OperationalError, match="injected setup failure"):
            cache._connection()

    assert failing and failing[0].closed
    assert not hasattr(cache._local, "owner")
    assert cache._connection().execute("SELECT 1").fetchone() == (1,)


def test_read_only_cache_lookup_does_not_create_missing_sqlite_file(tmp_path: Path) -> None:
    store = _store(tmp_path / "market.duckdb", read_only=True)
    cache = store._indicator_cache
    assert isinstance(cache, SqliteIndicatorCacheStore)

    rows = store.read_indicator_cache(
        market="SH",
        code="600000",
        timeframe="daily",
        price_basis="raw",
        formula_id="technical",
        formula_version="1",
        data_fingerprint="bars-v1",
    )

    assert rows == []
    assert not cache.database_path.exists()
