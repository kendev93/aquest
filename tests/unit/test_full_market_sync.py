from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import pandas as pd

from selector_app.market_data.etfquest_writer import ETFQuestMarketDataWriter
from selector_app.market_data.full_sync import (
    FullMarketSync,
    MarketSyncTarget,
    resolve_market_sync_workers,
    resolve_sync_range,
    target_ranges,
)


class _Storage:
    def __init__(self) -> None:
        self.coverage: dict[str, dict[str, object]] = {}
        self.dates: dict[str, list[date]] = {}
        self.saved: list[pd.DataFrame] = []
        self.catalog: pd.DataFrame | None = None
        self.settings: dict[str, str] = {}

    def get_trade_calendar(self) -> pd.DataFrame:
        return pd.DataFrame({"trade_date": [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]})

    def get_symbol_date_coverage(self, symbol: str, data_type: str = "daily") -> dict[str, object]:
        del data_type
        return self.coverage.get(
            symbol,
            {"earliest_date": None, "latest_date": None, "bar_count": 0},
        )

    def get_symbol_dates(
        self,
        symbol: str,
        start_date: date | None = None,
        end_date: date | None = None,
        data_type: str = "daily",
    ) -> list[date]:
        del data_type
        return [
            value
            for value in self.dates.get(symbol, [])
            if (start_date is None or value >= start_date)
            and (end_date is None or value <= end_date)
        ]

    def upsert_asset_info(self, frame: pd.DataFrame) -> None:
        self.catalog = frame.copy()

    def write_daily_bars(self, frame: pd.DataFrame) -> int:
        self.saved.append(frame.copy())
        return len(frame)

    def update_last_update_time(self, symbol: str, data_type: str = "daily") -> None:
        del symbol, data_type

    def save_system_setting(self, key: str, value: str) -> None:
        self.settings[key] = value


class _Source:
    def get_stock_list(self) -> pd.DataFrame:
        return pd.DataFrame([{"symbol": "600000", "name": "浦发银行", "type": "Stock"}])

    def get_etf_list(self) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {"symbol": "510300", "name": "沪深300ETF", "type": "ETF"},
                {"symbol": "163406", "name": "兴全合润", "type": "LOF"},
            ]
        )

    def get_index_list(self) -> pd.DataFrame:
        return pd.DataFrame([{"symbol": "sh000300", "name": "沪深300", "type": "Index"}])

    def get_daily_data_for_asset(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        asset_type: str,
        adjust_type: str = "qfq",
    ) -> pd.DataFrame:
        del asset_type, adjust_type, end_date
        return pd.DataFrame(
            [
                {
                    "symbol": symbol,
                    "date": start_date,
                    "open": 1,
                    "close": 1,
                    "high": 1,
                    "low": 1,
                    "volume": 10,
                }
            ]
        )


def test_target_ranges_redownloads_selected_interval_for_internal_gap() -> None:
    storage = _Storage()
    storage.coverage["600000"] = {
        "earliest_date": date(2024, 1, 2),
        "latest_date": date(2024, 1, 4),
        "bar_count": 2,
    }
    storage.dates["600000"] = [date(2024, 1, 2), date(2024, 1, 4)]

    assert target_ranges(
        storage,
        MarketSyncTarget("600000", "浦发银行", "Stock"),
        date(2024, 1, 2),
        date(2024, 1, 4),
    ) == [(date(2024, 1, 2), date(2024, 1, 4))]


def test_target_ranges_repairs_a_missing_prefix_without_skipping_it() -> None:
    storage = _Storage()
    storage.coverage["600000"] = {
        "earliest_date": date(2024, 1, 3),
        "latest_date": date(2024, 1, 4),
        "bar_count": 2,
    }
    storage.dates["600000"] = [date(2024, 1, 3), date(2024, 1, 4)]

    assert target_ranges(
        storage,
        MarketSyncTarget("600000", "浦发银行", "Stock"),
        date(2024, 1, 2),
        date(2024, 1, 4),
    ) == [(date(2024, 1, 2), date(2024, 1, 2))]


def test_full_sync_discovers_all_assets_and_merges_single_writer(tmp_path: Path) -> None:
    storage = _Storage()
    report = FullMarketSync(storage, _Source(), temp_root=tmp_path).run(
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 4),
        workers=1,
    )

    assert report["success"] is True
    assert report["catalog_counts"] == {"Stock": 1, "ETF": 1, "LOF": 1, "Index": 1}
    assert report["completed"] == 4
    assert report["written_bars"] == 4
    assert report["errors"] == 0
    assert len(storage.saved) == 4
    assert storage.settings["market_sync_last_summary"]


def test_full_sync_writes_the_existing_etfquest_schema(tmp_path: Path) -> None:
    database = tmp_path / "market_data.db"
    with duckdb.connect(str(database)) as connection:
        connection.execute(
            "CREATE TABLE asset_info ("
            "symbol VARCHAR PRIMARY KEY, name VARCHAR, type VARCHAR, update_time TIMESTAMP)"
        )
        connection.execute(
            "CREATE TABLE market_data ("
            "symbol VARCHAR, date DATE, open DOUBLE, close DOUBLE, high DOUBLE, "
            "low DOUBLE, volume BIGINT, data_type VARCHAR, update_time TIMESTAMP, "
            "PRIMARY KEY (symbol, date, data_type))"
        )
        connection.execute(
            "CREATE TABLE trade_calendar (trade_date DATE PRIMARY KEY, update_time TIMESTAMP)"
        )
        connection.execute(
            "CREATE TABLE system_settings ("
            "key VARCHAR PRIMARY KEY, value VARCHAR, updated_at TIMESTAMP)"
        )

    writer = ETFQuestMarketDataWriter(database)
    report = FullMarketSync(writer, _Source(), temp_root=tmp_path).run(
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 4),
        workers=1,
    )

    assert report["success"] is True
    assert writer.status().bar_count == 4
    assert len(writer.get_trade_calendar()) == 1


def test_full_sync_worker_count_respects_memory_budget(monkeypatch) -> None:
    monkeypatch.setenv("ETFQUEST_MARKET_SYNC_WORKERS", "auto")
    monkeypatch.setenv("ETFQUEST_MARKET_SYNC_MAX_WORKERS", "8")
    monkeypatch.setenv("ETFQUEST_MARKET_SYNC_MEMORY_BUDGET_MB", "512")

    assert (
        resolve_market_sync_workers(
            cpu_count=16,
            available_memory_mb=512,
            per_worker_mb=256,
        )
        == 1
    )


def test_sync_range_presets_are_resolved_against_the_selected_day() -> None:
    today = date(2026, 9, 14)

    assert resolve_sync_range("all", today=today) == (None, None)
    assert resolve_sync_range("5y", today=today) == (date(2021, 9, 14), today)
    assert resolve_sync_range("ytd", today=today) == (date(2026, 1, 1), today)
