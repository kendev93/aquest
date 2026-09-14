"""Write-only operations for ETFQuest's existing market-data schema.

The writer intentionally does not inherit :class:`DuckDbMarketDataStore`.
Indicator Lab owns a separate business database and must never migrate,
compact, back up, or create tables in ETFQuest's database.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

from .models import DataStoreStatus

_REQUIRED_TABLES = frozenset({"asset_info", "market_data", "trade_calendar", "system_settings"})
_REQUIRED_BAR_COLUMNS = (
    "symbol",
    "date",
    "open",
    "close",
    "high",
    "low",
    "volume",
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _date_value(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return date.fromisoformat(str(parsed)[:10])


class ETFQuestMarketDataWriter:
    """Validate and update only the four tables owned by ETFQuest."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path).expanduser().resolve()

    def _connect(self) -> duckdb.DuckDBPyConnection:
        if not self.database_path.is_file():
            raise FileNotFoundError(f"ETFQuest 行情数据库不存在: {self.database_path}")
        return duckdb.connect(str(self.database_path), read_only=False)

    def validate_schema(self) -> None:
        with self._connect() as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
                ).fetchall()
            }
            missing = sorted(_REQUIRED_TABLES - tables)
            if missing:
                raise RuntimeError(
                    f"ETFQuest 行情数据库 schema 不兼容，缺少表: {', '.join(missing)}"
                )
            required_columns = {
                "asset_info": {"symbol", "name", "type", "update_time"},
                "market_data": set(_REQUIRED_BAR_COLUMNS) | {"data_type", "update_time"},
                "trade_calendar": {"trade_date", "update_time"},
                "system_settings": {"key", "value", "updated_at"},
            }
            for table, required in required_columns.items():
                actual = {str(row[0]) for row in connection.execute(f"DESCRIBE {table}").fetchall()}
                missing_columns = sorted(required - actual)
                if missing_columns:
                    raise RuntimeError(
                        f"ETFQuest 行情数据库 schema 不兼容，{table} 缺少字段: "
                        + ", ".join(missing_columns)
                    )

    def status(self) -> DataStoreStatus:
        self.validate_schema()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(DISTINCT symbol), COUNT(*), MIN(date), MAX(date), "
                "MAX(update_time) FROM market_data WHERE data_type = 'daily'"
            ).fetchone()
        return DataStoreStatus(
            database_path=str(self.database_path),
            schema_version=0,
            instrument_count=int(row[0] or 0) if row else 0,
            bar_count=int(row[1] or 0) if row else 0,
            data_start=str(row[2])[:10] if row and row[2] else None,
            data_end=str(row[3])[:10] if row and row[3] else None,
            last_online_sync_at=str(row[4]) if row and row[4] else None,
            last_online_provider="etfquest",
            last_online_provider_version=None,
        )

    def upsert_asset_info(self, frame: pd.DataFrame) -> int:
        if frame.empty:
            return 0
        required = {"symbol", "name", "type"}
        missing = sorted(required - set(frame.columns))
        if missing:
            raise ValueError(f"资产目录缺少字段: {', '.join(missing)}")
        incoming = frame.loc[:, ["symbol", "name", "type"]].copy()
        incoming["symbol"] = incoming["symbol"].astype(str).str.strip()
        incoming = incoming.loc[incoming["symbol"] != ""].drop_duplicates("symbol", keep="last")
        if incoming.empty:
            return 0
        incoming["update_time"] = _now()
        with self._connect() as connection:
            connection.execute("BEGIN")
            try:
                connection.register("_incoming_asset_info", incoming)
                try:
                    connection.execute(
                        """
                        INSERT INTO asset_info (symbol, name, type, update_time)
                        SELECT symbol, name, type, update_time FROM _incoming_asset_info
                        ON CONFLICT (symbol) DO UPDATE SET
                            name = excluded.name,
                            type = excluded.type,
                            update_time = excluded.update_time
                        """
                    )
                finally:
                    connection.unregister("_incoming_asset_info")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return len(incoming)

    def get_trade_calendar(self) -> pd.DataFrame:
        with self._connect() as connection:
            return connection.execute(
                "SELECT trade_date FROM trade_calendar ORDER BY trade_date"
            ).df()

    def get_symbol_date_coverage(self, symbol: str, data_type: str = "daily") -> dict[str, object]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT MIN(date), MAX(date), COUNT(*) FROM market_data "
                "WHERE symbol = ? AND data_type = ?",
                [symbol, data_type],
            ).fetchone()
        return {
            "earliest_date": _date_value(row[0]) if row else None,
            "latest_date": _date_value(row[1]) if row else None,
            "bar_count": int(row[2] or 0) if row else 0,
        }

    def get_symbol_dates(
        self,
        symbol: str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        data_type: str = "daily",
    ) -> list[date]:
        conditions = ["symbol = ?", "data_type = ?"]
        parameters: list[object] = [symbol, data_type]
        if start_date is not None:
            conditions.append("date >= ?")
            parameters.append(start_date)
        if end_date is not None:
            conditions.append("date <= ?")
            parameters.append(end_date)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT date FROM market_data WHERE " + " AND ".join(conditions) + " ORDER BY date",
                parameters,
            ).fetchall()
        return [item for row in rows if (item := _date_value(row[0])) is not None]

    def write_daily_bars(self, frame: pd.DataFrame) -> int:
        if frame.empty:
            return 0
        missing = sorted(set(_REQUIRED_BAR_COLUMNS) - set(frame.columns))
        if missing:
            raise ValueError(f"ETFQuest 日线缺少字段: {', '.join(missing)}")
        incoming = frame.copy(deep=True)
        incoming["symbol"] = incoming["symbol"].astype(str).str.strip()
        incoming["date"] = pd.to_datetime(incoming["date"], errors="coerce").dt.date
        for column in ("open", "close", "high", "low", "volume"):
            incoming[column] = pd.to_numeric(incoming[column], errors="coerce")
        incoming["data_type"] = incoming.get("data_type", "daily")
        incoming["update_time"] = incoming.get("update_time", _now())
        incoming = incoming.dropna(subset=["symbol", "date", "open", "close", "high", "low"])
        incoming = incoming.loc[incoming["symbol"] != ""]
        if incoming.empty:
            return 0
        incoming["volume"] = incoming["volume"].fillna(0).astype("int64")
        incoming = incoming.loc[
            :,
            [
                "symbol",
                "date",
                "open",
                "close",
                "high",
                "low",
                "volume",
                "data_type",
                "update_time",
            ],
        ].drop_duplicates(["symbol", "date", "data_type"], keep="last")
        with self._connect() as connection:
            connection.execute("BEGIN")
            try:
                connection.register("_incoming_market_data", incoming)
                try:
                    connection.execute(
                        """
                        INSERT INTO market_data (
                            symbol, date, open, close, high, low, volume, data_type, update_time
                        )
                        SELECT symbol, date, open, close, high, low, volume, data_type, update_time
                        FROM _incoming_market_data
                        ON CONFLICT (symbol, date, data_type) DO UPDATE SET
                            open = excluded.open,
                            close = excluded.close,
                            high = excluded.high,
                            low = excluded.low,
                            volume = excluded.volume,
                            update_time = excluded.update_time
                        """
                    )
                finally:
                    connection.unregister("_incoming_market_data")
                calendar = incoming[["date"]].drop_duplicates().copy()
                calendar["update_time"] = _now()
                connection.register("_incoming_trade_calendar", calendar)
                try:
                    connection.execute(
                        """
                        INSERT INTO trade_calendar (trade_date, update_time)
                        SELECT date, update_time FROM _incoming_trade_calendar
                        ON CONFLICT (trade_date) DO UPDATE SET
                            update_time = excluded.update_time
                        """
                    )
                finally:
                    connection.unregister("_incoming_trade_calendar")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return len(incoming)

    def update_last_update_time(self, symbol: str, data_type: str = "daily") -> None:
        timestamp = _now()
        with self._connect() as connection:
            connection.execute(
                "UPDATE market_data SET update_time = ? WHERE symbol = ? AND data_type = ?",
                [timestamp, symbol, data_type],
            )

    def save_system_setting(self, key: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO system_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT (key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                [key, value, _now()],
            )

    def get_system_setting(self, key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM system_settings WHERE key = ?", [key]
            ).fetchone()
        return str(row[0]) if row and row[0] is not None else None


__all__ = ["ETFQuestMarketDataWriter"]
