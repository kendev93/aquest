from __future__ import annotations

from datetime import date

import pytest

from aquest.market_data.benchmark import (
    BENCHMARK_SYMBOL,
    BenchmarkSyncError,
    refresh_benchmark_bars,
)
from aquest.market_data.store import DuckDbMarketDataStore


def _fetcher(rows: list[tuple[date, float]]):
    def fetch(start_date: date, end_date: date, *, timeout: float) -> list[tuple[date, float]]:
        del start_date, end_date, timeout
        return rows

    return fetch


def test_refresh_replaces_full_series_and_filters_invalid_rows(tmp_path):
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    first = [(date(2015, 1, 5), 3.1), (date(2015, 1, 6), 3.2), (date(2015, 1, 7), 3.3)]
    assert refresh_benchmark_bars(store, _fetcher(first), today=date(2026, 11, 27)) == 3
    assert store.read_benchmark_bars(BENCHMARK_SYMBOL) == first

    # 前复权基准按最新除权基准整表重算：第二次同步只留第二次的行。
    second = [
        (date(2016, 1, 4), 3.0),
        (date(2016, 1, 5), float("nan")),
        (date(2014, 12, 31), 99.0),
        (date(2030, 1, 1), 3.5),
    ]
    assert refresh_benchmark_bars(store, _fetcher(second), today=date(2026, 11, 27)) == 1
    assert store.read_benchmark_bars(BENCHMARK_SYMBOL) == [(date(2016, 1, 4), 3.0)]
    assert store.read_benchmark_bars(
        BENCHMARK_SYMBOL,
        start_date=date(2016, 1, 1),
        end_date=date(2016, 1, 4),
    ) == [(date(2016, 1, 4), 3.0)]


def test_refresh_rejects_empty_series(tmp_path):
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    with pytest.raises(BenchmarkSyncError):
        refresh_benchmark_bars(store, _fetcher([]), today=date(2026, 11, 27))
