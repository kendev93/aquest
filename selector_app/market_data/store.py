"""Embedded DuckDB market-data store."""

from __future__ import annotations

import os
import re
import shutil
import time
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timezone
from pathlib import Path
from threading import RLock
from typing import cast

import duckdb
import numpy as np
import pandas as pd

from .day_format import classify_board
from .models import (
    DataStoreStatus,
    InstrumentBoard,
    InstrumentRef,
    InstrumentType,
    MarketCode,
    StockRef,
)

SCHEMA_VERSION = 5
_APP_DATA_DIR_NAME = ".indicator-lab"
_BAR_COLUMNS = [
    "market",
    "code",
    "instrument_type",
    "board",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "source",
    "price_basis",
    "bar_status",
    "imported_at",
]
_FRAME_COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount", "bar_status"]
_WRITE_LOCK = RLock()
_CONNECT_RETRY_DELAYS = (0.05, 0.1, 0.25, 0.5, 1.0)
_INSTRUMENT_TYPES = {"stock", "fund", "index", "bond"}
_INSTRUMENT_BOARDS = {"main", "star", "chinext", "b_share", "fund", "index", "bond"}


class MarketDataStoreError(RuntimeError):
    """Safe, user-facing storage failure without exposing DuckDB internals."""


def default_data_dir() -> Path:
    configured = os.getenv("SELECTOR_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / _APP_DATA_DIR_NAME


def default_database_path() -> Path:
    return default_data_dir() / "market.duckdb"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _date_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value)
    return None if text in {"NaT", "nan", "None"} else text[:10]


def _timestamp_text(value: object) -> str | None:
    return None if value is None else str(value)


def _clean_name(value: object) -> str | None:
    if value is None:
        return None
    missing = pd.isna(value)
    if isinstance(missing, (bool, np.bool_)) and bool(missing):
        return None
    text = str(value).strip()
    return text or None


def _append_filter(
    conditions: list[str],
    parameters: list[object],
    column: str,
    values: Iterable[str] | None,
) -> None:
    selected = tuple(str(value) for value in values or ())
    if not selected:
        return
    placeholders = ", ".join("?" for _ in selected)
    conditions.append(f"{column} IN ({placeholders})")
    parameters.extend(selected)


class DuckDbMarketDataStore:
    """Own all DuckDB connections so workers never share connection objects."""

    def __init__(self, database_path: str | Path | None = None, *, read_only: bool = False) -> None:
        self.database_path = Path(database_path or default_database_path()).expanduser()
        self.read_only = read_only
        self._initialized = False

    def _connect(self, *, read_only: bool | None = None) -> duckdb.DuckDBPyConnection:
        mode = self.read_only if read_only is None else read_only
        if mode:
            if not self.read_only:
                self._ensure_initialized()
            elif not self.database_path.exists():
                raise FileNotFoundError(f"行情数据库不存在: {self.database_path}")
        else:
            self._ensure_initialized()
        last_error: duckdb.Error | None = None
        for delay in (0.0, *_CONNECT_RETRY_DELAYS):
            if delay:
                time.sleep(delay)
            try:
                return duckdb.connect(str(self.database_path), read_only=mode)
            except duckdb.Error as exc:
                message = str(exc).lower()
                if "lock" not in message and "conflicting" not in message:
                    raise MarketDataStoreError(
                        "行情数据库无法打开，请检查文件权限或数据库完整性"
                    ) from exc
                last_error = exc
        raise MarketDataStoreError(
            "行情数据库当前被其它进程占用，请关闭其它写入进程后重试"
        ) from last_error

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        if self.read_only:
            if not self.database_path.exists():
                raise FileNotFoundError(f"行情数据库不存在: {self.database_path}")
            self._initialized = True
            return
        with _WRITE_LOCK:
            if self._initialized:
                return
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()
            self._initialized = True

    def _initialize(self) -> None:
        existing_version = self._schema_version_on_disk()
        if existing_version is not None and existing_version > SCHEMA_VERSION:
            raise RuntimeError(f"行情数据库 schema 版本过高: {existing_version} > {SCHEMA_VERSION}")
        if existing_version is not None and existing_version < SCHEMA_VERSION:
            self._backup_file()
        with duckdb.connect(str(self.database_path), read_only=False) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS instruments (
                    market VARCHAR NOT NULL,
                    code VARCHAR NOT NULL,
                    name VARCHAR,
                    instrument_type VARCHAR NOT NULL,
                    board VARCHAR NOT NULL DEFAULT 'main',
                    source_path VARCHAR,
                    first_date DATE,
                    last_date DATE,
                    bar_count BIGINT NOT NULL DEFAULT 0,
                    source_state VARCHAR NOT NULL DEFAULT 'active',
                    updated_at TIMESTAMP NOT NULL,
                    PRIMARY KEY (market, code)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_bars (
                    market VARCHAR NOT NULL,
                    code VARCHAR NOT NULL,
                    instrument_type VARCHAR NOT NULL,
                    board VARCHAR NOT NULL DEFAULT 'main',
                    trade_date DATE NOT NULL,
                    open DOUBLE NOT NULL,
                    high DOUBLE NOT NULL,
                    low DOUBLE NOT NULL,
                    close DOUBLE NOT NULL,
                    volume DOUBLE NOT NULL,
                    amount DOUBLE NOT NULL,
                    source VARCHAR NOT NULL,
                    price_basis VARCHAR NOT NULL,
                    bar_status VARCHAR NOT NULL,
                    imported_at TIMESTAMP NOT NULL,
                    UNIQUE (market, code, trade_date, price_basis)
                )
                """
            )
            self._migrate_schema(connection, existing_version or 0)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS source_files (
                    source_root VARCHAR NOT NULL,
                    relative_path VARCHAR NOT NULL,
                    market VARCHAR NOT NULL,
                    code VARCHAR NOT NULL,
                    instrument_type VARCHAR,
                    size BIGINT NOT NULL,
                    mtime_ns BIGINT NOT NULL,
                    sha256 VARCHAR,
                    status VARCHAR NOT NULL,
                    last_seen_at TIMESTAMP NOT NULL,
                    last_imported_at TIMESTAMP,
                    error VARCHAR,
                    PRIMARY KEY (source_root, relative_path)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS data_meta (
                    key VARCHAR PRIMARY KEY,
                    value VARCHAR NOT NULL,
                    updated_at TIMESTAMP NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS indicator_cache (
                    market VARCHAR NOT NULL,
                    code VARCHAR NOT NULL,
                    timeframe VARCHAR NOT NULL,
                    price_basis VARCHAR NOT NULL,
                    formula_id VARCHAR NOT NULL,
                    formula_version VARCHAR NOT NULL,
                    data_fingerprint VARCHAR NOT NULL,
                    data_start DATE,
                    data_end DATE,
                    data_length BIGINT NOT NULL,
                    values_blob BLOB NOT NULL,
                    signals_blob BLOB NOT NULL,
                    computed_at TIMESTAMP NOT NULL,
                    PRIMARY KEY (
                        market, code, timeframe, price_basis, formula_id,
                        formula_version, data_fingerprint
                    )
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS custom_indicator_cache (
                    market VARCHAR NOT NULL,
                    code VARCHAR NOT NULL,
                    timeframe VARCHAR NOT NULL,
                    price_basis VARCHAR NOT NULL,
                    formula_hash VARCHAR NOT NULL,
                    parameter_hash VARCHAR NOT NULL,
                    engine_version VARCHAR NOT NULL,
                    parameter_json VARCHAR NOT NULL,
                    value_names_json VARCHAR NOT NULL,
                    signal_names_json VARCHAR NOT NULL,
                    data_fingerprint VARCHAR NOT NULL,
                    data_start DATE,
                    data_end DATE,
                    data_length BIGINT NOT NULL,
                    minimum_bars BIGINT NOT NULL,
                    recommended_bars BIGINT NOT NULL,
                    values_blob BLOB NOT NULL,
                    signals_blob BLOB NOT NULL,
                    computed_at TIMESTAMP NOT NULL,
                    PRIMARY KEY (
                        market, code, timeframe, price_basis, formula_hash,
                        parameter_hash, engine_version, data_fingerprint
                    )
                )
                """
            )
            connection.execute(
                """
                INSERT INTO data_meta (key, value, updated_at)
                VALUES ('schema_version', ?, ?)
                ON CONFLICT (key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                [str(SCHEMA_VERSION), _now()],
            )

    @staticmethod
    def _migrate_schema(connection: duckdb.DuckDBPyConnection, version: int) -> None:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
            ).fetchall()
        }
        if version < 2:
            for table in ("instruments", "daily_bars"):
                if table not in tables:
                    continue
                columns = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'main' AND table_name = ?",
                        [table],
                    ).fetchall()
                }
                if "board" in columns:
                    continue
                connection.execute(f"ALTER TABLE {table} ADD COLUMN board VARCHAR DEFAULT 'main'")
                connection.execute(
                    f"UPDATE {table} SET board = CASE "
                    "WHEN market = 'SH' AND substr(code, 1, 2) = '68' THEN 'star' "
                    "WHEN market = 'SZ' AND substr(code, 1, 2) = '30' THEN 'chinext' "
                    "WHEN (market = 'SH' AND substr(code, 1, 2) = '90') "
                    "OR (market = 'SZ' AND substr(code, 1, 2) = '20') THEN 'b_share' "
                    "WHEN (market = 'SH' AND substr(code, 1, 2) IN "
                    "('50','51','52','53','55','56','58')) "
                    "OR (market = 'SZ' AND substr(code, 1, 2) IN "
                    "('15','16','17','18')) THEN 'fund' "
                    "WHEN (market = 'SH' AND substr(code, 1, 2) IN ('00','88','99')) "
                    "OR (market = 'SZ' AND substr(code, 1, 2) = '39') THEN 'index' "
                    "WHEN (market = 'SH' AND substr(code, 1, 2) IN "
                    "('01','10','11','12','13','14','20')) "
                    "OR (market = 'SZ' AND substr(code, 1, 2) IN "
                    "('10','11','12','13','14')) THEN 'bond' "
                    "ELSE 'main' END"
                )
        if version < 3 and "instruments" in tables:
            columns = {
                str(row[0])
                for row in connection.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'main' AND table_name = 'instruments'"
                ).fetchall()
            }
            if "name" not in columns:
                connection.execute("ALTER TABLE instruments ADD COLUMN name VARCHAR")

    def _schema_version_on_disk(self) -> int | None:
        if not self.database_path.exists():
            return None
        try:
            with duckdb.connect(str(self.database_path), read_only=True) as connection:
                row = connection.execute(
                    "SELECT value FROM data_meta WHERE key = 'schema_version'"
                ).fetchone()
        except duckdb.CatalogException:
            return None
        except duckdb.Error as exc:
            raise MarketDataStoreError("行情数据库无法读取，请检查文件完整性") from exc
        if row is None:
            return None
        try:
            return int(str(row[0]))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("行情数据库 schema 版本无效") from exc

    def _backup_file(self) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        target = self.database_path.with_name(
            f"{self.database_path.stem}.bak-{timestamp}{self.database_path.suffix}"
        )
        shutil.copy2(self.database_path, target)
        return target

    def backup_database(self) -> Path:
        """Create a recoverable copy before an operator-managed migration."""

        self._require_writer()
        with _WRITE_LOCK:
            self._ensure_initialized()
            return self._backup_file()

    def _require_writer(self) -> None:
        if self.read_only:
            raise RuntimeError("只读行情连接不能写入")

    @staticmethod
    def _normalize_frame(
        frame: pd.DataFrame,
        *,
        market: str,
        code: str,
        instrument_type: InstrumentType,
        board: InstrumentBoard | None = None,
        source: str,
    ) -> pd.DataFrame:
        normalized_market = market.strip().upper()
        normalized_code = code.strip()
        if normalized_market not in {"SH", "SZ"}:
            raise ValueError(f"行情市场无效: {market}")
        if re.fullmatch(r"\d{6}", normalized_code) is None:
            raise ValueError(f"行情代码无效: {code}")
        if instrument_type not in _INSTRUMENT_TYPES:
            raise ValueError(f"证券类型无效: {instrument_type}")
        resolved_board = board or classify_board(normalized_market, normalized_code)
        if resolved_board is None or resolved_board not in _INSTRUMENT_BOARDS:
            raise ValueError(f"证券板块无效: {normalized_market} {normalized_code}")
        missing = [column for column in _FRAME_COLUMNS if column not in frame.columns]
        if missing:
            raise ValueError(f"行情数据缺少字段: {', '.join(missing)}")
        normalized = frame.loc[:, _FRAME_COLUMNS].copy()
        normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce")
        if normalized["date"].isna().any():
            raise ValueError("行情数据包含无效日期")
        for column in ("open", "high", "low", "close", "volume", "amount"):
            normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
        numeric_columns = ["open", "high", "low", "close", "volume", "amount"]
        numeric_values = normalized[numeric_columns].to_numpy(dtype=float)
        if not np.isfinite(numeric_values).all():
            raise ValueError("行情数据包含无效数值")
        if not normalized["bar_status"].isin(["completed", "provisional"]).all():
            raise ValueError("行情数据包含无效 K 线状态")
        normalized = normalized.sort_values("date", kind="stable").drop_duplicates(
            subset=["date"], keep="last"
        )
        imported_at = _now()
        result = pd.DataFrame(
            {
                "market": normalized_market,
                "code": normalized_code,
                "instrument_type": instrument_type,
                "board": resolved_board,
                "trade_date": normalized["date"].dt.date,
                "open": normalized["open"].astype(float),
                "high": normalized["high"].astype(float),
                "low": normalized["low"].astype(float),
                "close": normalized["close"].astype(float),
                "volume": normalized["volume"].astype(float),
                "amount": normalized["amount"].astype(float),
                "source": source,
                "price_basis": "raw",
                "bar_status": normalized["bar_status"].astype(str),
                "imported_at": imported_at,
            }
        )
        return result[_BAR_COLUMNS].reset_index(drop=True)

    @staticmethod
    def _frame_from_query(result: pd.DataFrame) -> pd.DataFrame:
        if result.empty:
            return pd.DataFrame(
                columns=[
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
                    "price_basis",
                    "bar_status",
                    "imported_at",
                ]
            )
        result = result.rename(columns={"trade_date": "date"})
        result["date"] = pd.to_datetime(result["date"])
        return result

    def _refresh_instrument(
        self,
        connection: duckdb.DuckDBPyConnection,
        market: str,
        code: str,
        instrument_type: InstrumentType,
        board: InstrumentBoard,
        name: str | None = None,
        source_path: str | None = None,
        source_state: str = "active",
    ) -> None:
        connection.execute(
            """
            INSERT INTO instruments (
                market, code, name, instrument_type, board, source_path, first_date, last_date,
                bar_count, source_state, updated_at
            )
            SELECT ?, ?, ?, ?, ?, ?, MIN(trade_date), MAX(trade_date), COUNT(*), ?, ?
            FROM daily_bars
            WHERE market = ? AND code = ?
            ON CONFLICT (market, code) DO UPDATE SET
                name = COALESCE(excluded.name, instruments.name),
                instrument_type = excluded.instrument_type,
                board = excluded.board,
                source_path = COALESCE(excluded.source_path, instruments.source_path),
                first_date = excluded.first_date,
                last_date = excluded.last_date,
                bar_count = excluded.bar_count,
                source_state = excluded.source_state,
                updated_at = excluded.updated_at
            """,
            [
                market,
                code,
                _clean_name(name),
                instrument_type,
                board,
                source_path,
                source_state,
                _now(),
                market,
                code,
            ],
        )

    def update_instrument_names(self, names: Mapping[tuple[str, str], str | None]) -> int:
        """Update names for instruments already present in the repository."""

        self._require_writer()
        rows = [
            {
                "market": market.strip().upper(),
                "code": code.strip(),
                "name": _clean_name(name),
            }
            for (market, code), name in names.items()
            if _clean_name(name) is not None
        ]
        if not rows:
            return 0
        frame = pd.DataFrame(rows, columns=["market", "code", "name"])
        with _WRITE_LOCK, self._connect() as connection:
            connection.execute("BEGIN")
            try:
                connection.register("_instrument_names", frame)
                matched = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM instruments AS instruments
                    INNER JOIN _instrument_names AS names
                      ON instruments.market = names.market
                     AND instruments.code = names.code
                    """
                ).fetchone()
                connection.execute(
                    """
                    UPDATE instruments
                    SET name = names.name, updated_at = ?
                    FROM _instrument_names AS names
                    WHERE instruments.market = names.market
                      AND instruments.code = names.code
                    """,
                    [_now()],
                )
                connection.unregister("_instrument_names")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return int(matched[0] if matched else 0)

    def replace_local_bars(
        self,
        market: str,
        code: str,
        instrument_type: InstrumentType,
        frame: pd.DataFrame,
        *,
        name: str | None = None,
        source_path: str | None = None,
        board: InstrumentBoard | None = None,
    ) -> int:
        """Replace one instrument atomically; local data wins over online data."""

        self._require_writer()
        market = market.strip().upper()
        code = code.strip()
        resolved_board = board or classify_board(market, code)
        if resolved_board is None:
            raise ValueError(f"证券板块无效: {market} {code}")
        incoming = self._normalize_frame(
            frame,
            market=market,
            code=code,
            instrument_type=instrument_type,
            board=resolved_board,
            source="local",
        )
        with _WRITE_LOCK, self._connect() as connection:
            connection.execute("BEGIN")
            try:
                connection.execute(
                    "DELETE FROM daily_bars WHERE market = ? AND code = ?", [market, code]
                )
                if not incoming.empty:
                    connection.register("_incoming_local_bars", incoming)
                    columns = ", ".join(_BAR_COLUMNS)
                    connection.execute(
                        f"INSERT INTO daily_bars ({columns}) "
                        f"SELECT {columns} FROM _incoming_local_bars"
                    )
                    connection.unregister("_incoming_local_bars")
                self._refresh_instrument(
                    connection,
                    market,
                    code,
                    instrument_type,
                    board=resolved_board,
                    name=name,
                    source_path=source_path,
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return len(incoming)

    def upsert_online_bars(self, frame: pd.DataFrame) -> tuple[int, int]:
        """Upsert online rows without overwriting any local row."""

        self._require_writer()
        if frame.empty:
            return (0, 0)
        incoming_frame = frame.copy(deep=True)
        names_by_key: dict[tuple[str, str], str | None] = {}
        if "name" in incoming_frame.columns:
            for market, code, name in (
                incoming_frame[["market", "code", "name"]]
                .drop_duplicates(subset=["market", "code"], keep="last")
                .itertuples(index=False, name=None)
            ):
                names_by_key[(str(market).strip().upper(), str(code).strip())] = _clean_name(name)
        if "board" not in incoming_frame.columns:
            incoming_frame["board"] = [
                classify_board(str(market), str(code))
                for market, code in incoming_frame[["market", "code"]].itertuples(
                    index=False, name=None
                )
            ]
        required = {"market", "code", "instrument_type", "board", *_FRAME_COLUMNS}
        missing = sorted(required - set(incoming_frame.columns))
        if missing:
            raise ValueError(f"在线行情数据缺少字段: {', '.join(missing)}")
        if incoming_frame[["market", "code", "instrument_type", "board"]].isna().any().any():
            raise ValueError("在线行情数据缺少标的身份字段")
        normalized_parts: list[pd.DataFrame] = []
        for (market, code, instrument_type, board), group in incoming_frame.groupby(
            ["market", "code", "instrument_type", "board"], sort=False
        ):
            normalized_parts.append(
                self._normalize_frame(
                    group,
                    market=str(market),
                    code=str(code),
                    instrument_type=cast(InstrumentType, str(instrument_type)),
                    board=cast(InstrumentBoard, str(board)),
                    source="online",
                )
            )
        if not normalized_parts:
            return (0, 0)
        incoming = pd.concat(normalized_parts, ignore_index=True)
        incoming = incoming.sort_values(
            ["market", "code", "trade_date"], kind="stable"
        ).reset_index(drop=True)
        with _WRITE_LOCK, self._connect() as connection:
            connection.execute("BEGIN")
            try:
                connection.register("_incoming_online_bars", incoming)
                eligible_row = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM _incoming_online_bars AS incoming
                    WHERE NOT EXISTS (
                        SELECT 1 FROM daily_bars AS local
                        WHERE local.market = incoming.market
                          AND local.code = incoming.code
                          AND local.trade_date = incoming.trade_date
                          AND local.price_basis = incoming.price_basis
                          AND local.source = 'local'
                    )
                    """
                ).fetchone()
                existing_online_row = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM _incoming_online_bars AS incoming
                    INNER JOIN daily_bars AS existing
                      ON existing.market = incoming.market
                     AND existing.code = incoming.code
                     AND existing.trade_date = incoming.trade_date
                     AND existing.price_basis = incoming.price_basis
                     AND existing.source = 'online'
                    """
                ).fetchone()
                eligible_count = int(eligible_row[0] if eligible_row else 0)
                existing_online_count = int(existing_online_row[0] if existing_online_row else 0)
                connection.execute(
                    """
                    DELETE FROM daily_bars AS existing
                    USING _incoming_online_bars AS incoming
                    WHERE existing.market = incoming.market
                      AND existing.code = incoming.code
                      AND existing.trade_date = incoming.trade_date
                      AND existing.price_basis = incoming.price_basis
                      AND existing.source = 'online'
                    """
                )
                connection.execute(
                    f"""
                    INSERT INTO daily_bars ({", ".join(_BAR_COLUMNS)})
                    SELECT {", ".join(f"incoming.{column}" for column in _BAR_COLUMNS)}
                    FROM _incoming_online_bars AS incoming
                    WHERE NOT EXISTS (
                        SELECT 1 FROM daily_bars AS local
                        WHERE local.market = incoming.market
                          AND local.code = incoming.code
                          AND local.trade_date = incoming.trade_date
                          AND local.price_basis = incoming.price_basis
                          AND local.source = 'local'
                    )
                    """
                )
                for market, code, instrument_type, board in (
                    incoming[["market", "code", "instrument_type", "board"]]
                    .drop_duplicates()
                    .itertuples(index=False, name=None)
                ):
                    self._refresh_instrument(
                        connection,
                        market,
                        code,
                        instrument_type,
                        board,
                        name=names_by_key.get((str(market).upper(), str(code))),
                    )
                connection.unregister("_incoming_online_bars")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return (eligible_count, existing_online_count)

    def _count_matching_keys(self, frame: pd.DataFrame) -> int:
        if frame.empty:
            return 0
        with self._connect(read_only=True) as connection:
            connection.register("_keys", frame[["market", "code", "trade_date", "price_basis"]])
            value = connection.execute(
                """
                SELECT COUNT(*)
                FROM daily_bars AS bars
                INNER JOIN _keys AS keys
                  ON bars.market = keys.market
                 AND bars.code = keys.code
                 AND bars.trade_date = keys.trade_date
                 AND bars.price_basis = keys.price_basis
                """
            ).fetchone()
            connection.unregister("_keys")
        return int(value[0] if value else 0)

    def read_bars(
        self,
        market: str,
        code: str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        include_provisional: bool = False,
    ) -> pd.DataFrame:
        market = market.strip().upper()
        code = code.strip()
        conditions = ["market = ?", "code = ?"]
        parameters: list[object] = [market, code]
        if start_date is not None:
            conditions.append("trade_date >= ?")
            parameters.append(start_date)
        if end_date is not None:
            conditions.append("trade_date <= ?")
            parameters.append(end_date)
        if not include_provisional:
            conditions.append("bar_status = 'completed'")
        query = (
            "SELECT market, code, instrument_type, board, trade_date, open, high, low, close, "
            "volume, amount, source, price_basis, bar_status, imported_at "
            f"FROM daily_bars WHERE {' AND '.join(conditions)} ORDER BY trade_date"
        )
        with self._connect(read_only=True) as connection:
            return self._frame_from_query(connection.execute(query, parameters).df())

    def has_provisional_bars(self, market: str, code: str) -> bool:
        """Return whether a symbol still needs its intraday bar finalized."""

        with self._connect(read_only=True) as connection:
            row = connection.execute(
                "SELECT 1 FROM daily_bars "
                "WHERE market = ? AND code = ? AND bar_status = 'provisional' LIMIT 1",
                [market.strip().upper(), code.strip()],
            ).fetchone()
        return row is not None

    def list_provisional_symbols(self) -> set[tuple[str, str]]:
        """Read all symbols with provisional bars in one bounded query."""

        with self._connect(read_only=True) as connection:
            rows = connection.execute(
                "SELECT DISTINCT market, code FROM daily_bars WHERE bar_status = 'provisional'"
            ).fetchall()
        return {(str(market), str(code)) for market, code in rows}

    def read_many_bars(
        self,
        instruments: Iterable[InstrumentRef | StockRef] | None = None,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        include_provisional: bool = False,
    ) -> pd.DataFrame:
        conditions: list[str] = []
        parameters: list[object] = []
        requested = None if instruments is None else list(instruments)
        symbols = requested or []
        if requested is not None and not symbols:
            return self._frame_from_query(pd.DataFrame())
        if symbols:
            symbol_frame = pd.DataFrame(
                [
                    {
                        "market": item.market,
                        "code": item.code,
                    }
                    for item in symbols
                ]
            ).drop_duplicates()
        else:
            symbol_frame = pd.DataFrame(columns=["market", "code"])
        if start_date is not None:
            conditions.append("bars.trade_date >= ?")
            parameters.append(start_date)
        if end_date is not None:
            conditions.append("bars.trade_date <= ?")
            parameters.append(end_date)
        if not include_provisional:
            conditions.append("bars.bar_status = 'completed'")
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        query = (
            "SELECT bars.market, bars.code, bars.instrument_type, bars.board, bars.trade_date, "
            "bars.open, "
            "bars.high, bars.low, bars.close, bars.volume, bars.amount, bars.source, "
            "bars.price_basis, bars.bar_status, bars.imported_at FROM daily_bars AS bars"
        )
        with self._connect(read_only=True) as connection:
            if symbols:
                connection.register("_requested_symbols", symbol_frame)
                query += (
                    " INNER JOIN _requested_symbols AS requested"
                    " ON bars.market = requested.market AND bars.code = requested.code"
                )
            query += where + " ORDER BY bars.market, bars.code, bars.trade_date"
            result = connection.execute(query, parameters).df()
            if symbols:
                connection.unregister("_requested_symbols")
            return self._frame_from_query(result)

    def read_latest_bars(
        self,
        instruments: Iterable[InstrumentRef | StockRef],
        *,
        count: int = 2,
        include_provisional: bool = False,
    ) -> pd.DataFrame:
        """Read a bounded latest-bar snapshot for many instruments in one query."""

        if isinstance(count, bool) or count < 1:
            raise ValueError("最新行情条数必须大于 0")
        requested = list(instruments)
        if not requested:
            return self._frame_from_query(pd.DataFrame())
        symbol_frame = pd.DataFrame(
            [{"market": item.market, "code": item.code} for item in requested]
        ).drop_duplicates()
        status_filter = "" if include_provisional else "WHERE bars.bar_status = 'completed'"
        query = f"""
            WITH ranked AS (
                SELECT
                    bars.market,
                    bars.code,
                    bars.instrument_type,
                    bars.board,
                    bars.trade_date,
                    bars.open,
                    bars.high,
                    bars.low,
                    bars.close,
                    bars.volume,
                    bars.amount,
                    bars.source,
                    bars.price_basis,
                    bars.bar_status,
                    bars.imported_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY bars.market, bars.code
                        ORDER BY bars.trade_date DESC
                    ) AS row_number
                FROM daily_bars AS bars
                INNER JOIN _requested_symbols AS requested
                    ON bars.market = requested.market AND bars.code = requested.code
                {status_filter}
            )
            SELECT market, code, instrument_type, board, trade_date, open, high, low, close,
                   volume, amount, source, price_basis, bar_status, imported_at
            FROM ranked
            WHERE row_number <= ?
            ORDER BY market, code, trade_date
        """
        with self._connect(read_only=True) as connection:
            connection.register("_requested_symbols", symbol_frame)
            try:
                result = connection.execute(query, [count]).df()
            finally:
                connection.unregister("_requested_symbols")
        return self._frame_from_query(result)

    def list_instruments(
        self,
        *,
        market: str | None = None,
        keyword: str = "",
        include_missing: bool = False,
        instrument_types: Iterable[InstrumentType] | None = None,
        boards: Iterable[InstrumentBoard] | None = None,
    ) -> list[InstrumentRef]:
        conditions: list[str] = []
        parameters: list[object] = []
        if market and market != "all":
            conditions.append("market = ?")
            parameters.append(market)
        normalized_keyword = keyword.strip().upper()
        if normalized_keyword:
            conditions.append("(code LIKE ? OR market LIKE ? OR instrument_type LIKE ?)")
            like = f"%{normalized_keyword}%"
            parameters.extend([like, like, like])
        if not include_missing:
            conditions.append("source_state != 'missing'")
        _append_filter(conditions, parameters, "instrument_type", instrument_types)
        _append_filter(conditions, parameters, "board", boards)
        query = (
            "SELECT market, code, name, instrument_type, board, source_path FROM instruments"
            + (f" WHERE {' AND '.join(conditions)}" if conditions else "")
            + " ORDER BY market, code"
        )
        with self._connect(read_only=True) as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            InstrumentRef(
                market=cast(MarketCode, market_value),
                code=cast(str, code),
                name=cast(str | None, name),
                instrument_type=cast(InstrumentType, instrument_type),
                board=cast(InstrumentBoard, board),
                source_path=Path(source_path) if source_path else None,
            )
            for market_value, code, name, instrument_type, board, source_path in rows
        ]

    def status(self) -> DataStoreStatus:
        with self._connect(read_only=True) as connection:
            instrument_row = connection.execute("SELECT COUNT(*) FROM instruments").fetchone()
            bar_row = connection.execute("SELECT COUNT(*) FROM daily_bars").fetchone()
            date_row = connection.execute(
                "SELECT MIN(trade_date), MAX(trade_date) "
                "FROM daily_bars WHERE bar_status = 'completed'"
            ).fetchone()
            instrument_count = int(instrument_row[0]) if instrument_row else 0
            bar_count = int(bar_row[0]) if bar_row else 0
            first, last = date_row if date_row else (None, None)
            local_import = connection.execute(
                "SELECT value FROM data_meta WHERE key = 'last_local_import_at'"
            ).fetchone()
            online_sync = connection.execute(
                "SELECT value FROM data_meta WHERE key = 'last_online_sync_at'"
            ).fetchone()
        return DataStoreStatus(
            database_path=str(self.database_path),
            schema_version=SCHEMA_VERSION,
            instrument_count=instrument_count,
            bar_count=bar_count,
            data_start=_date_text(first),
            data_end=_date_text(last),
            last_local_import_at=str(local_import[0]) if local_import else None,
            last_online_sync_at=str(online_sync[0]) if online_sync else None,
        )

    def get_store_status(self) -> DataStoreStatus:
        """Protocol-friendly alias for the public store status snapshot."""

        return self.status()

    def set_meta(self, key: str, value: str) -> None:
        self._require_writer()
        with _WRITE_LOCK, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO data_meta (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT (key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                [key, value, _now()],
            )

    def read_indicator_cache(
        self,
        *,
        market: str,
        code: str,
        timeframe: str,
        price_basis: str,
        formula_id: str,
        formula_version: str,
        data_fingerprint: str,
    ) -> dict[str, object] | None:
        """Read one immutable, versioned indicator result from DuckDB."""

        try:
            with self._connect(read_only=True) as connection:
                row = connection.execute(
                    """
                    SELECT market, code, timeframe, price_basis, formula_id,
                           formula_version, data_fingerprint, data_start, data_end,
                           data_length, values_blob, signals_blob, computed_at
                    FROM indicator_cache
                    WHERE market = ?
                      AND code = ?
                      AND timeframe = ?
                      AND price_basis = ?
                      AND formula_id = ?
                      AND formula_version = ?
                      AND data_fingerprint = ?
                    """,
                    [
                        market.strip().upper(),
                        code.strip(),
                        timeframe,
                        price_basis,
                        formula_id,
                        formula_version,
                        data_fingerprint,
                    ],
                ).fetchone()
        except duckdb.CatalogException:
            # A read-only connection opened against a pre-cache database can
            # still serve source bars; treat the absent derived table as miss.
            return None
        if row is None:
            return None
        keys = [
            "market",
            "code",
            "timeframe",
            "price_basis",
            "formula_id",
            "formula_version",
            "data_fingerprint",
            "data_start",
            "data_end",
            "data_length",
            "values_blob",
            "signals_blob",
            "computed_at",
        ]
        return dict(zip(keys, row, strict=True))

    def read_indicator_caches(
        self,
        *,
        market: str,
        code: str,
        timeframe: str,
        price_basis: str,
        data_fingerprint: str,
        formula_versions: Mapping[str, str],
    ) -> dict[str, dict[str, object]]:
        """Read several formula generations for one series in one connection."""

        if not formula_versions:
            return {}
        predicates: list[str] = []
        parameters: list[object] = [
            market.strip().upper(),
            code.strip(),
            timeframe,
            price_basis,
            data_fingerprint,
        ]
        for formula_id, formula_version in formula_versions.items():
            predicates.append("(formula_id = ? AND formula_version = ?)")
            parameters.extend([formula_id, formula_version])
        query = (
            "SELECT market, code, timeframe, price_basis, formula_id, formula_version, "
            "data_fingerprint, data_start, data_end, data_length, values_blob, "
            "signals_blob, computed_at FROM indicator_cache "
            "WHERE market = ? AND code = ? AND timeframe = ? AND price_basis = ? "
            "AND data_fingerprint = ? AND (" + " OR ".join(predicates) + ")"
        )
        try:
            with self._connect(read_only=True) as connection:
                rows = connection.execute(query, parameters).fetchall()
        except duckdb.CatalogException:
            return {}
        keys = [
            "market",
            "code",
            "timeframe",
            "price_basis",
            "formula_id",
            "formula_version",
            "data_fingerprint",
            "data_start",
            "data_end",
            "data_length",
            "values_blob",
            "signals_blob",
            "computed_at",
        ]
        return {str(row[4]): dict(zip(keys, row, strict=True)) for row in rows}

    def read_indicator_caches_batch(
        self,
        keys: Iterable[Mapping[str, str]],
    ) -> list[dict[str, object]]:
        """Read cache rows for many instruments with one DuckDB query."""

        pending = list(keys)
        if not pending:
            return []
        lookup_columns = [
            "market",
            "code",
            "timeframe",
            "price_basis",
            "formula_id",
            "formula_version",
            "data_fingerprint",
        ]
        lookup_frame = pd.DataFrame(pending, columns=lookup_columns).drop_duplicates()
        try:
            with self._connect(read_only=True) as connection:
                connection.register("_indicator_cache_keys", lookup_frame)
                try:
                    rows = connection.execute(
                        """
                        SELECT cache.market, cache.code, cache.timeframe,
                               cache.price_basis, cache.formula_id,
                               cache.formula_version, cache.data_fingerprint,
                               cache.data_start, cache.data_end, cache.data_length,
                               cache.values_blob, cache.signals_blob, cache.computed_at
                        FROM indicator_cache AS cache
                        INNER JOIN _indicator_cache_keys AS keys
                          ON cache.market = keys.market
                         AND cache.code = keys.code
                         AND cache.timeframe = keys.timeframe
                         AND cache.price_basis = keys.price_basis
                         AND cache.formula_id = keys.formula_id
                         AND cache.formula_version = keys.formula_version
                         AND cache.data_fingerprint = keys.data_fingerprint
                        """
                    ).fetchall()
                finally:
                    connection.unregister("_indicator_cache_keys")
        except duckdb.CatalogException:
            return []
        columns = [
            "market",
            "code",
            "timeframe",
            "price_basis",
            "formula_id",
            "formula_version",
            "data_fingerprint",
            "data_start",
            "data_end",
            "data_length",
            "values_blob",
            "signals_blob",
            "computed_at",
        ]
        return [dict(zip(columns, row, strict=True)) for row in rows]

    def read_custom_indicator_cache(
        self,
        *,
        market: str,
        code: str,
        timeframe: str,
        price_basis: str,
        formula_hash: str,
        parameter_hash: str,
        engine_version: str,
        data_fingerprint: str,
    ) -> dict[str, object] | None:
        """Read one parameterized custom-formula result from DuckDB."""

        try:
            with self._connect(read_only=True) as connection:
                row = connection.execute(
                    """
                    SELECT market, code, timeframe, price_basis, formula_hash,
                           parameter_hash, engine_version, parameter_json,
                           value_names_json, signal_names_json, data_fingerprint,
                           data_start, data_end, data_length, minimum_bars,
                           recommended_bars, values_blob, signals_blob, computed_at
                    FROM custom_indicator_cache
                    WHERE market = ?
                      AND code = ?
                      AND timeframe = ?
                      AND price_basis = ?
                      AND formula_hash = ?
                      AND parameter_hash = ?
                      AND engine_version = ?
                      AND data_fingerprint = ?
                    """,
                    [
                        market.strip().upper(),
                        code.strip(),
                        timeframe,
                        price_basis,
                        formula_hash,
                        parameter_hash,
                        engine_version,
                        data_fingerprint,
                    ],
                ).fetchone()
        except duckdb.CatalogException:
            return None
        if row is None:
            return None
        keys = [
            "market",
            "code",
            "timeframe",
            "price_basis",
            "formula_hash",
            "parameter_hash",
            "engine_version",
            "parameter_json",
            "value_names_json",
            "signal_names_json",
            "data_fingerprint",
            "data_start",
            "data_end",
            "data_length",
            "minimum_bars",
            "recommended_bars",
            "values_blob",
            "signals_blob",
            "computed_at",
        ]
        return dict(zip(keys, row, strict=True))

    def write_indicator_cache(
        self,
        *,
        market: str,
        code: str,
        timeframe: str,
        price_basis: str,
        formula_id: str,
        formula_version: str,
        data_fingerprint: str,
        data_start: date | None,
        data_end: date | None,
        data_length: int,
        values_blob: bytes,
        signals_blob: bytes,
    ) -> None:
        self.write_indicator_caches(
            [
                {
                    "market": market,
                    "code": code,
                    "timeframe": timeframe,
                    "price_basis": price_basis,
                    "formula_id": formula_id,
                    "formula_version": formula_version,
                    "data_fingerprint": data_fingerprint,
                    "data_start": data_start,
                    "data_end": data_end,
                    "data_length": data_length,
                    "values_blob": values_blob,
                    "signals_blob": signals_blob,
                }
            ]
        )

    def write_indicator_caches(self, entries: Iterable[Mapping[str, object]]) -> None:
        """Atomically replace several formula cache generations in one transaction."""

        pending = list(entries)
        if not pending:
            return

        self._require_writer()
        with _WRITE_LOCK, self._connect() as connection:
            connection.execute("BEGIN")
            try:
                for entry in pending:
                    normalized_market = str(entry["market"]).strip().upper()
                    normalized_code = str(entry["code"]).strip()
                    timeframe = str(entry["timeframe"])
                    price_basis = str(entry["price_basis"])
                    formula_id = str(entry["formula_id"])
                    formula_version = str(entry["formula_version"])
                    data_fingerprint = str(entry["data_fingerprint"])
                    # Only the current data generation is useful. Keeping stale
                    # generations would make this derived table grow forever.
                    connection.execute(
                        """
                        DELETE FROM indicator_cache
                        WHERE market = ?
                          AND code = ?
                          AND timeframe = ?
                          AND price_basis = ?
                          AND formula_id = ?
                          AND (formula_version <> ? OR data_fingerprint <> ?)
                        """,
                        [
                            normalized_market,
                            normalized_code,
                            timeframe,
                            price_basis,
                            formula_id,
                            formula_version,
                            data_fingerprint,
                        ],
                    )
                    connection.execute(
                        """
                        INSERT INTO indicator_cache (
                            market, code, timeframe, price_basis, formula_id,
                            formula_version, data_fingerprint, data_start, data_end,
                            data_length, values_blob, signals_blob, computed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (
                            market, code, timeframe, price_basis, formula_id,
                            formula_version, data_fingerprint
                        ) DO UPDATE SET
                            data_start = excluded.data_start,
                            data_end = excluded.data_end,
                            data_length = excluded.data_length,
                            values_blob = excluded.values_blob,
                            signals_blob = excluded.signals_blob,
                            computed_at = excluded.computed_at
                        """,
                        [
                            normalized_market,
                            normalized_code,
                            timeframe,
                            price_basis,
                            formula_id,
                            formula_version,
                            data_fingerprint,
                            entry["data_start"],
                            entry["data_end"],
                            int(cast(int, entry["data_length"])),
                            entry["values_blob"],
                            entry["signals_blob"],
                            _now(),
                        ],
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def write_custom_indicator_cache(
        self,
        *,
        market: str,
        code: str,
        timeframe: str,
        price_basis: str,
        formula_hash: str,
        parameter_hash: str,
        engine_version: str,
        parameter_json: str,
        value_names_json: str,
        signal_names_json: str,
        data_fingerprint: str,
        data_start: date | None,
        data_end: date | None,
        data_length: int,
        minimum_bars: int,
        recommended_bars: int,
        values_blob: bytes,
        signals_blob: bytes,
    ) -> None:
        """Atomically replace the current custom-formula cache generation."""

        self._require_writer()
        normalized_market = market.strip().upper()
        normalized_code = code.strip()
        with _WRITE_LOCK, self._connect() as connection:
            connection.execute("BEGIN")
            try:
                connection.execute(
                    """
                    DELETE FROM custom_indicator_cache
                    WHERE market = ?
                      AND code = ?
                      AND timeframe = ?
                      AND price_basis = ?
                      AND formula_hash = ?
                      AND parameter_hash = ?
                      AND (engine_version <> ? OR data_fingerprint <> ?)
                    """,
                    [
                        normalized_market,
                        normalized_code,
                        timeframe,
                        price_basis,
                        formula_hash,
                        parameter_hash,
                        engine_version,
                        data_fingerprint,
                    ],
                )
                connection.execute(
                    """
                    INSERT INTO custom_indicator_cache (
                        market, code, timeframe, price_basis, formula_hash,
                        parameter_hash, engine_version, parameter_json,
                        value_names_json, signal_names_json, data_fingerprint,
                        data_start, data_end, data_length, minimum_bars,
                        recommended_bars, values_blob, signals_blob, computed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (
                        market, code, timeframe, price_basis, formula_hash,
                        parameter_hash, engine_version, data_fingerprint
                    ) DO UPDATE SET
                        parameter_json = excluded.parameter_json,
                        value_names_json = excluded.value_names_json,
                        signal_names_json = excluded.signal_names_json,
                        data_start = excluded.data_start,
                        data_end = excluded.data_end,
                        data_length = excluded.data_length,
                        minimum_bars = excluded.minimum_bars,
                        recommended_bars = excluded.recommended_bars,
                        values_blob = excluded.values_blob,
                        signals_blob = excluded.signals_blob,
                        computed_at = excluded.computed_at
                    """,
                    [
                        normalized_market,
                        normalized_code,
                        timeframe,
                        price_basis,
                        formula_hash,
                        parameter_hash,
                        engine_version,
                        parameter_json,
                        value_names_json,
                        signal_names_json,
                        data_fingerprint,
                        data_start,
                        data_end,
                        int(data_length),
                        int(minimum_bars),
                        int(recommended_bars),
                        values_blob,
                        signals_blob,
                        _now(),
                    ],
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def indicator_cache_status(self) -> dict[str, int | str | None]:
        """Return persistent cache size and freshness metadata."""

        try:
            with self._connect(read_only=True) as connection:
                built_in = connection.execute(
                    """
                    SELECT COUNT(*),
                           COALESCE(
                               SUM(octet_length(values_blob) + octet_length(signals_blob)),
                               0
                           ),
                           MIN(computed_at), MAX(computed_at)
                    FROM indicator_cache
                    """
                ).fetchone()
                try:
                    custom = connection.execute(
                        """
                        SELECT COUNT(*),
                               COALESCE(
                                   SUM(octet_length(values_blob) + octet_length(signals_blob)),
                                   0
                               ),
                               MIN(computed_at), MAX(computed_at)
                        FROM custom_indicator_cache
                        """
                    ).fetchone()
                except duckdb.CatalogException:
                    custom = (0, 0, None, None)
        except duckdb.CatalogException:
            built_in = (0, 0, None, None)
            custom = (0, 0, None, None)
        built_in = built_in or (0, 0, None, None)
        custom = custom or (0, 0, None, None)
        total_entries = int(built_in[0]) + int(custom[0])
        return {
            "builtin_entries": int(built_in[0]),
            "custom_entries": int(custom[0]),
            "total_entries": total_entries,
            "bytes": int(built_in[1]) + int(custom[1]),
            "oldest_computed_at": _timestamp_text(
                min(
                    (value for value in (built_in[2], custom[2]) if value is not None),
                    default=None,
                )
            ),
            "latest_computed_at": _timestamp_text(
                max(
                    (value for value in (built_in[3], custom[3]) if value is not None),
                    default=None,
                )
            ),
        }

    def source_file(self, source_root: str | Path, relative_path: str) -> dict[str, object] | None:
        root = str(Path(source_root).expanduser().resolve())
        with self._connect(read_only=True) as connection:
            row = connection.execute(
                "SELECT source_root, relative_path, market, code, instrument_type, "
                "size, mtime_ns, sha256, status, last_seen_at, last_imported_at, error "
                "FROM source_files WHERE source_root = ? AND relative_path = ?",
                [root, relative_path],
            ).fetchone()
        if row is None:
            return None
        keys = [
            "source_root",
            "relative_path",
            "market",
            "code",
            "instrument_type",
            "size",
            "mtime_ns",
            "sha256",
            "status",
            "last_seen_at",
            "last_imported_at",
            "error",
        ]
        return dict(zip(keys, row, strict=True))

    def source_files(self, source_root: str | Path) -> list[dict[str, object]]:
        root = str(Path(source_root).expanduser().resolve())
        with self._connect(read_only=True) as connection:
            rows = connection.execute(
                "SELECT source_root, relative_path, market, code, instrument_type, "
                "size, mtime_ns, sha256, status, last_seen_at, last_imported_at, error "
                "FROM source_files WHERE source_root = ?",
                [root],
            ).fetchall()
        keys = [
            "source_root",
            "relative_path",
            "market",
            "code",
            "instrument_type",
            "size",
            "mtime_ns",
            "sha256",
            "status",
            "last_seen_at",
            "last_imported_at",
            "error",
        ]
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def record_source_file(
        self,
        *,
        source_root: str | Path,
        relative_path: str,
        market: str,
        code: str,
        instrument_type: InstrumentType | None,
        size: int,
        mtime_ns: int,
        sha256: str | None,
        status: str,
        error: str | None = None,
        imported: bool = False,
    ) -> None:
        self._require_writer()
        root = str(Path(source_root).expanduser().resolve())
        now = _now()
        with _WRITE_LOCK, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO source_files (
                    source_root, relative_path, market, code, instrument_type, size, mtime_ns,
                    sha256, status, last_seen_at, last_imported_at, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (source_root, relative_path) DO UPDATE SET
                    market = excluded.market,
                    code = excluded.code,
                    instrument_type = excluded.instrument_type,
                    size = excluded.size,
                    mtime_ns = excluded.mtime_ns,
                    sha256 = excluded.sha256,
                    status = excluded.status,
                    last_seen_at = excluded.last_seen_at,
                    last_imported_at = CASE
                        WHEN excluded.last_imported_at IS NOT NULL THEN excluded.last_imported_at
                        ELSE source_files.last_imported_at
                    END,
                    error = excluded.error
                """,
                [
                    root,
                    relative_path,
                    market,
                    code,
                    instrument_type,
                    size,
                    mtime_ns,
                    sha256,
                    status,
                    now,
                    now if imported else None,
                    error,
                ],
            )

    def mark_source_missing(self, source_root: str | Path, relative_path: str) -> None:
        self._require_writer()
        root = str(Path(source_root).expanduser().resolve())
        with _WRITE_LOCK, self._connect() as connection:
            connection.execute(
                "UPDATE source_files SET status = 'missing', last_seen_at = ?, error = ? "
                "WHERE source_root = ? AND relative_path = ?",
                [_now(), "源文件不存在", root, relative_path],
            )
