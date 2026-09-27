"""History-store façade and core screening/backtest history operations."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping

import duckdb

from .store_base import (
    _WRITE_LOCK,
    _DuckDbStoreBase,
)
from .store_history_candidates import CandidateHistoryMixin
from .store_history_common import (
    ASSET_TABLES,
    CANDIDATE_LIBRARY_TABLE,
    CANDIDATE_NOTE_MAX_LENGTH,
    CANDIDATE_STATUS_ADOPTED,
    CANDIDATE_STATUS_PENDING,
    CANDIDATE_STATUS_PUBLISHED,
    CANDIDATE_STATUS_REJECTED,
    PRESET_NAME_MAX_LENGTH,
    PRESET_NOTE_MAX_LENGTH,
    RESEARCH_PRESET_TABLE,
    SAVED_STRATEGY_TABLE,
    SCREEN_PRESET_TABLE,
    STOCK_GROUP_CRITERIA_PAYLOAD_MAX_LENGTH,
    STOCK_GROUP_CRITERIA_SUMMARY_MAX_LENGTH,
    STOCK_GROUP_CRITERIA_TABLE,
    STOCK_GROUP_MAX_ITEMS,
    STOCK_GROUP_NAME_MAX_LENGTH,
    STOCK_GROUP_NOTE_MAX_LENGTH,
    STOCK_GROUP_SOURCE_MANUAL,
    STOCK_GROUP_SOURCE_SCREEN,
    STOCK_GROUP_TABLE,
    STRATEGY_NAME_MAX_LENGTH,
    STRATEGY_NOTE_MAX_LENGTH,
    STRATEGY_TRACKING_ACTIVE,
    STRATEGY_TRACKING_OFF,
    STRATEGY_TRACKING_PLAN_TABLE,
    STRATEGY_TRACKING_TABLE,
    STRATEGY_TRACKING_TRADES_TABLE,
    TRACKING_PHASE_POST,
    TRACKING_PHASE_PRE,
    _require_asset_table,
)
from .store_history_presets import PresetHistoryMixin
from .store_history_records import HistoryRecordsMixin
from .store_history_stock_groups import StockGroupHistoryMixin
from .store_history_tracking import StrategyTrackingHistoryMixin

__all__ = [
    "ASSET_TABLES",
    "CANDIDATE_LIBRARY_TABLE",
    "CANDIDATE_NOTE_MAX_LENGTH",
    "CANDIDATE_STATUS_ADOPTED",
    "CANDIDATE_STATUS_PENDING",
    "CANDIDATE_STATUS_PUBLISHED",
    "CANDIDATE_STATUS_REJECTED",
    "DuckDbHistoryStore",
    "PRESET_NAME_MAX_LENGTH",
    "PRESET_NOTE_MAX_LENGTH",
    "RESEARCH_PRESET_TABLE",
    "SAVED_STRATEGY_TABLE",
    "SCREEN_PRESET_TABLE",
    "STOCK_GROUP_CRITERIA_PAYLOAD_MAX_LENGTH",
    "STOCK_GROUP_CRITERIA_SUMMARY_MAX_LENGTH",
    "STOCK_GROUP_CRITERIA_TABLE",
    "STOCK_GROUP_MAX_ITEMS",
    "STOCK_GROUP_NAME_MAX_LENGTH",
    "STOCK_GROUP_NOTE_MAX_LENGTH",
    "STOCK_GROUP_SOURCE_MANUAL",
    "STOCK_GROUP_SOURCE_SCREEN",
    "STOCK_GROUP_TABLE",
    "STRATEGY_NAME_MAX_LENGTH",
    "STRATEGY_NOTE_MAX_LENGTH",
    "STRATEGY_TRACKING_ACTIVE",
    "STRATEGY_TRACKING_OFF",
    "STRATEGY_TRACKING_PLAN_TABLE",
    "STRATEGY_TRACKING_TABLE",
    "STRATEGY_TRACKING_TRADES_TABLE",
    "TRACKING_PHASE_POST",
    "TRACKING_PHASE_PRE",
]


class DuckDbHistoryStore(
    HistoryRecordsMixin,
    StrategyTrackingHistoryMixin,
    PresetHistoryMixin,
    StockGroupHistoryMixin,
    CandidateHistoryMixin,
):
    """Persist and retrieve user-facing screening and backtest history."""

    def __init__(self, store: _DuckDbStoreBase) -> None:
        self._store = store

    def history_table_columns(self, table: str) -> tuple[str, ...]:
        """Column names of one user-asset table, in physical order."""

        _require_asset_table(table)
        with self._store._connect(read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = ?
                ORDER BY ordinal_position
                """,
                [table],
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def iter_history_rows(
        self,
        table: str,
        *,
        batch_size: int = 200,
    ) -> Iterator[dict[str, object]]:
        """Stream every row of one user-asset table as plain dictionaries.

        Rows are fetched in batches so exporting a multi-megabyte research
        report never materialises the whole table in memory.
        """

        _require_asset_table(table)
        if batch_size < 1:
            raise ValueError("批量读取行数必须大于 0")
        columns = self.history_table_columns(table)
        if not columns:
            return
        projection = ", ".join(f'"{column}"' for column in columns)
        with self._store._connect(read_only=True) as connection:
            cursor = connection.execute(f'SELECT {projection} FROM "{table}"')
            while True:
                batch = cursor.fetchmany(batch_size)
                if not batch:
                    return
                for row in batch:
                    yield dict(zip(columns, row, strict=True))

    def insert_history_rows(
        self,
        table: str,
        rows: Iterable[Mapping[str, object]],
        *,
        batch_size: int = 200,
    ) -> tuple[int, int]:
        """Insert user-asset rows, skipping any that already exist.

        Returns ``(inserted, attempted)``.  Conflicts are matched by primary key
        *and* by any unique constraint, so importing the same backup twice is a
        no-op instead of a duplicate or an error.
        """

        self._store._require_writer()
        _require_asset_table(table)
        if batch_size < 1:
            raise ValueError("批量写入行数必须大于 0")
        columns = self.history_table_columns(table)
        if not columns:
            return (0, 0)
        projection = ", ".join(f'"{column}"' for column in columns)
        placeholders = ", ".join("?" for _ in columns)
        statement = (
            f'INSERT INTO "{table}" ({projection}) VALUES ({placeholders}) ON CONFLICT DO NOTHING'
        )
        count_statement = f'SELECT COUNT(*) FROM "{table}"'

        inserted = 0
        attempted = 0
        batch: list[tuple[object, ...]] = []
        with _WRITE_LOCK, self._store._connect() as connection:
            for row in rows:
                batch.append(tuple(row.get(column) for column in columns))
                if len(batch) < batch_size:
                    continue
                before = _count_rows(connection, count_statement)
                connection.executemany(statement, batch)
                inserted += _count_rows(connection, count_statement) - before
                attempted += len(batch)
                batch.clear()
            if batch:
                before = _count_rows(connection, count_statement)
                connection.executemany(statement, batch)
                inserted += _count_rows(connection, count_statement) - before
                attempted += len(batch)
        return inserted, attempted


def _count_rows(connection: duckdb.DuckDBPyConnection, statement: str) -> int:
    row = connection.execute(statement).fetchone()
    return int(row[0]) if row is not None else 0
