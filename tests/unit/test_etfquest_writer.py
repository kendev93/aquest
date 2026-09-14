from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import pandas as pd

from selector_app.market_data.etfquest_writer import ETFQuestMarketDataWriter


def _create_database(path: Path) -> None:
    with duckdb.connect(str(path)) as connection:
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


def test_writer_validates_etfquest_schema_and_writes_only_market_tables(tmp_path: Path) -> None:
    database = tmp_path / "market_data.db"
    _create_database(database)
    writer = ETFQuestMarketDataWriter(database)

    writer.validate_schema()
    writer.upsert_asset_info(
        pd.DataFrame([{"symbol": "510300", "name": "沪深300ETF", "type": "ETF"}])
    )
    written = writer.write_daily_bars(
        pd.DataFrame(
            [
                {
                    "symbol": "510300",
                    "date": date(2024, 1, 2),
                    "open": 3.5,
                    "close": 3.6,
                    "high": 3.7,
                    "low": 3.4,
                    "volume": 2000,
                    "data_type": "daily",
                }
            ]
        )
    )

    assert written == 1
    assert writer.get_symbol_date_coverage("510300") == {
        "earliest_date": date(2024, 1, 2),
        "latest_date": date(2024, 1, 2),
        "bar_count": 1,
    }
    with duckdb.connect(str(database), read_only=True) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
            ).fetchall()
        }
        assert "daily_bars" not in tables
        assert connection.execute("SELECT COUNT(*) FROM market_data").fetchone() == (1,)


def test_writer_rejects_missing_schema_without_creating_or_migrating(tmp_path: Path) -> None:
    database = tmp_path / "market_data.db"
    with duckdb.connect(str(database)) as connection:
        connection.execute("CREATE TABLE market_data (symbol VARCHAR)")

    writer = ETFQuestMarketDataWriter(database)

    try:
        writer.validate_schema()
    except RuntimeError as exc:
        assert "schema" in str(exc)
    else:
        raise AssertionError("缺少 ETFQuest 表结构时必须拒绝写入")

    with duckdb.connect(str(database), read_only=True) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
            ).fetchall()
        }
    assert tables == {"market_data"}
