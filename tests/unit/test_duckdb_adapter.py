from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from threading import Barrier, Lock

import pandas as pd
import pytest

from selector_app.market_data.adapter import DuckDbMarketDataAdapter
from selector_app.market_data.store import DuckDbMarketDataStore


def _frame(instrument_type: str = "stock") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": [pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03")],
            "open": [10.0, 11.0],
            "high": [11.0, 12.0],
            "low": [9.0, 10.0],
            "close": [10.5, 11.5],
            "volume": [1000.0, 1100.0],
            "amount": [10500.0, 12650.0],
            "bar_status": ["completed", "completed"],
            "instrument_type": [instrument_type, instrument_type],
        }
    )


def test_duckdb_adapter_lists_and_reads_instruments_without_vipdoc(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _frame())
    store.replace_local_bars("SH", "601398", "stock", _frame())
    adapter = DuckDbMarketDataAdapter(store)

    refs = adapter.list_stock_refs("/unused", "all")
    ref = adapter.stock_ref("/unused", "SH", "601398")

    assert [(item.market, item.code, item.instrument_type) for item in refs] == [
        ("SH", "600000", "stock"),
        ("SH", "601398", "stock"),
    ]
    assert ref.path == Path("/unused")
    assert ref.instrument_type == "stock"
    assert list(adapter.read_stock(ref)["close"]) == [10.5, 11.5]


def test_worker_adapter_reuses_one_immutable_stock_snapshot_per_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DuckDbMarketDataStore(tmp_path / "cached-market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _frame())
    original_read = store.read_bars
    original_many_read = store.read_many_bars
    read_count = 0

    def read_bars(market: str, code: str) -> pd.DataFrame:
        nonlocal read_count
        read_count += 1
        return original_read(market, code)

    def read_many_bars(*args, **kwargs) -> pd.DataFrame:
        nonlocal read_count
        read_count += 1
        return original_many_read(*args, **kwargs)

    monkeypatch.setattr(store, "read_bars", read_bars)
    monkeypatch.setattr(store, "read_many_bars", read_many_bars)
    adapter = DuckDbMarketDataAdapter(store, cache_stock_reads=True)
    ref = adapter.stock_ref("", "SH", "600000")

    first = adapter.read_stock(ref)
    first.loc[0, "close"] = -1
    second = adapter.read_stock(ref)

    assert read_count == 1
    assert second.loc[0, "close"] == 10.5

    window = adapter.read_many_stocks(
        [ref],
        start_date=date(2024, 1, 3),
        end_date=date(2024, 1, 3),
        warmup_bars=1,
    )
    assert window["date"].dt.strftime("%Y-%m-%d").tolist() == [
        "2024-01-02",
        "2024-01-03",
    ]
    assert read_count == 1

    adapter.clear_thread_stock_cache()
    adapter.read_stock(ref)
    assert read_count == 2


def test_worker_stock_snapshots_are_thread_local_and_concurrent_reads_succeed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DuckDbMarketDataStore(tmp_path / "thread-local-market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _frame())
    original_read = store.read_bars
    read_count = 0
    count_lock = Lock()

    def read_bars(market: str, code: str) -> pd.DataFrame:
        nonlocal read_count
        with count_lock:
            read_count += 1
        return original_read(market, code)

    monkeypatch.setattr(store, "read_bars", read_bars)
    adapter = DuckDbMarketDataAdapter(store, cache_stock_reads=True)
    ref = adapter.stock_ref("", "SH", "600000")
    barrier = Barrier(2, timeout=3)

    def read_twice() -> tuple[pd.DataFrame, pd.DataFrame]:
        first = adapter.read_stock(ref)
        barrier.wait()
        second = adapter.read_stock(ref)
        return first, second

    with ThreadPoolExecutor(max_workers=2) as executor:
        frames = list(executor.map(lambda _index: read_twice(), range(2)))

    assert read_count == 2
    assert all(first.equals(second) for first, second in frames)


def _register_without_bars(store: DuckDbMarketDataStore, market: str, code: str) -> None:
    """Insert an instrument row that has no ``daily_bars`` behind it.

    This is what a catalog-only import leaves behind: the directory was read, so
    the instrument exists, but no price series ever arrived.
    """

    with store._connect() as connection:
        connection.execute(
            """
            INSERT INTO instruments (
                market, code, name, instrument_type, board, bar_count, source_state, updated_at
            ) VALUES (?, ?, ?, ?, ?, 0, 'active', now())
            """,
            [market, code, "空标的", "fund", "main"],
        )


def test_duckdb_adapter_skips_instruments_without_bars(tmp_path: Path) -> None:
    """A catalog-only row cannot be backtested, so it must not reach the pool."""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _frame())
    _register_without_bars(store, "SH", "510050")
    adapter = DuckDbMarketDataAdapter(store)

    assert [(item.market, item.code) for item in adapter.list_stock_refs("/unused", "all")] == [
        ("SH", "600000")
    ]


def test_duckdb_adapter_custom_universe_uses_codes_from_file(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _frame())
    store.replace_local_bars("SH", "601398", "stock", _frame())
    universe = tmp_path / "universe.txt"
    universe.write_text("SH 600000\nSH 601398\n", encoding="utf-8")
    adapter = DuckDbMarketDataAdapter(store)

    refs = adapter.list_stock_refs("/unused", "custom", universe)

    assert [(item.market, item.code) for item in refs] == [("SH", "600000"), ("SH", "601398")]
