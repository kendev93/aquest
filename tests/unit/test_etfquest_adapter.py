from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import pytest

from selector_app.market_data.etfquest_adapter import (
    ETFQuestMarketDataAdapter,
    ETFQuestMarketDataError,
    default_etfquest_database_path,
)


def _create_etfquest_database(path: Path) -> None:
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            "CREATE TABLE asset_info (symbol VARCHAR PRIMARY KEY, name VARCHAR, type VARCHAR, update_time TIMESTAMP)"
        )
        connection.execute(
            "CREATE TABLE market_data (symbol VARCHAR, date DATE, open DOUBLE, close DOUBLE, high DOUBLE, low DOUBLE, volume BIGINT, data_type VARCHAR, update_time TIMESTAMP)"
        )
        connection.execute(
            "CREATE TABLE trade_calendar (trade_date DATE PRIMARY KEY, update_time TIMESTAMP)"
        )
        connection.execute(
            "CREATE TABLE system_settings (key VARCHAR PRIMARY KEY, value VARCHAR, updated_at TIMESTAMP)"
        )
        connection.executemany(
            "INSERT INTO asset_info VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
            [
                ("600000", "浦发银行", "Stock"),
                ("510300", "沪深300ETF", "ETF"),
                ("163406", "兴全合润", "LOF"),
                ("sh000300", "沪深300", "Index"),
            ],
        )
        connection.executemany(
            "INSERT INTO market_data VALUES (?, ?, ?, ?, ?, ?, ?, 'daily', CURRENT_TIMESTAMP)",
            [
                ("600000", date(2024, 1, 2), 10.0, 10.5, 10.6, 9.9, 1000),
                ("600000", date(2024, 1, 3), 10.5, 10.7, 10.8, 10.4, 1200),
                ("510300", date(2024, 1, 2), 3.5, 3.6, 3.7, 3.4, 2000),
                ("163406", date(2024, 1, 2), 1.2, 1.3, 1.35, 1.1, 3000),
                ("sh000300", date(2024, 1, 2), 3500.0, 3510.0, 3520.0, 3490.0, 0),
            ],
        )


def test_default_path_can_be_overridden(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    configured = tmp_path / "market_data.db"
    monkeypatch.setenv("SELECTOR_ETFQUEST_DATABASE_PATH", str(configured))

    assert default_etfquest_database_path() == configured


def test_adapter_maps_etfquest_assets_and_reads_bars_read_only(tmp_path: Path) -> None:
    database = tmp_path / "market_data.db"
    _create_etfquest_database(database)
    adapter = ETFQuestMarketDataAdapter(database)

    refs = adapter.list_stock_refs(".", "all")

    assert {(ref.market, ref.code, ref.instrument_type) for ref in refs} == {
        ("SH", "600000", "stock"),
        ("SH", "510300", "fund"),
        ("SZ", "163406", "fund"),
        ("SH", "000300", "index"),
    }
    ref = next(item for item in refs if item.code == "510300")
    frame = adapter.read_stock(ref)

    assert list(frame.columns) == [
        "market",
        "code",
        "instrument_type",
        "board",
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "source",
        "provider",
        "price_basis",
        "bar_status",
        "imported_at",
    ]
    assert frame.iloc[0]["date"].date() == date(2024, 1, 2)
    assert float(frame.iloc[0]["amount"]) == 0.0

    with pytest.raises(duckdb.Error):
        with duckdb.connect(str(database), read_only=True) as connection:
            connection.execute("INSERT INTO market_data VALUES ('000001', CURRENT_DATE, 1, 1, 1, 1, 0, 'daily', CURRENT_TIMESTAMP)")


def test_adapter_requires_existing_etfquest_database(tmp_path: Path) -> None:
    with pytest.raises(ETFQuestMarketDataError, match="ETFQuest 行情数据库不存在"):
        ETFQuestMarketDataAdapter(tmp_path / "missing.db").list_stock_refs(".", "all")


def test_adapter_filters_instrument_types_and_market(tmp_path: Path) -> None:
    database = tmp_path / "market_data.db"
    _create_etfquest_database(database)
    adapter = ETFQuestMarketDataAdapter(database)

    refs = adapter.list_stock_refs(
        ".",
        "sh",
        instrument_types=("fund", "index"),
    )

    assert {(ref.market, ref.code, ref.instrument_type) for ref in refs} == {
        ("SH", "510300", "fund"),
        ("SH", "000300", "index"),
    }
