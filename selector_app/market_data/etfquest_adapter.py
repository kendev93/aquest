"""Read-only adapter for the market database owned by ETFQuest.

ETFQuest is the only process allowed to update its market database.  Indicator
Lab deliberately keeps this adapter independent from its own writable store so
that application history, indicator caches, and job metadata remain local.
"""

from __future__ import annotations

import os
import platform
import time
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Iterator, cast

import duckdb
import pandas as pd

from .day_format import classify_board
from .models import (
    DataStoreStatus,
    InstrumentBoard,
    InstrumentType,
    MarketCode,
    StockRef,
)

_CONNECT_RETRY_DELAYS = (0.05, 0.1, 0.25, 0.5)
_ETFQUEST_TABLES = {"asset_info", "market_data", "trade_calendar", "system_settings"}
_BAR_COLUMNS = [
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


class ETFQuestMarketDataError(RuntimeError):
    """Safe error raised when ETFQuest's shared database cannot be read."""


def default_etfquest_database_path() -> Path:
    """Resolve ETFQuest's packaged database path without creating it."""

    configured = str(os.getenv("SELECTOR_ETFQUEST_DATABASE_PATH") or "").strip()
    if configured:
        return Path(configured).expanduser()

    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "ETFQuest" / "database" / "market_data.db"
    if system == "Windows":
        program_data = os.getenv("ProgramData", r"C:\ProgramData")
        return Path(program_data) / "ETFQuest" / "database" / "market_data.db"
    return Path.home() / ".local" / "share" / "ETFQuest" / "database" / "market_data.db"


def _empty_bars() -> pd.DataFrame:
    return pd.DataFrame(columns=pd.Index(_BAR_COLUMNS))


def _normalise_asset_type(raw_type: object, symbol: str) -> InstrumentType:
    value = str(raw_type or "").strip().lower()
    if value in {"stock", "股票"}:
        return "stock"
    if value in {"index", "指数"}:
        return "index"
    if value in {"bond", "债券"}:
        return "bond"
    if value in {"fund", "etf", "lof", "基金"}:
        return "fund"
    clean = symbol.lower().removeprefix("sh").removeprefix("sz")
    if symbol.lower().startswith(("sh000", "sz399")):
        return "index"
    if clean.startswith(("51", "52", "56", "58", "15", "16", "17", "18")):
        return "fund"
    return "stock"


def _split_symbol(symbol: object, instrument_type: InstrumentType) -> tuple[MarketCode, str] | None:
    raw = str(symbol or "").strip()
    if not raw:
        return None
    lower = raw.lower()
    if lower.startswith("sh") or lower.startswith("sz"):
        market = cast(MarketCode, lower[:2].upper())
        code = lower[2:]
    else:
        market = "SH" if raw.startswith(("5", "6")) else "SZ"
        code = raw
        if instrument_type == "index":
            market = "SZ" if raw.startswith("399") else "SH"
    if len(code) != 6 or not code.isdigit():
        return None
    return market, code


def _board_for(market: MarketCode, code: str, instrument_type: InstrumentType) -> InstrumentBoard:
    if instrument_type == "fund":
        return "fund"
    if instrument_type == "index":
        return "index"
    if instrument_type == "bond":
        return "bond"
    return classify_board(market, code) or "main"


class ETFQuestMarketDataAdapter:
    """Expose ETFQuest daily data through the Indicator Lab adapter contract."""

    def __init__(self, database_path: str | Path | None = None) -> None:
        self.database_path = Path(database_path or default_etfquest_database_path()).expanduser()

    @contextmanager
    def _connect(self) -> Iterator[duckdb.DuckDBPyConnection]:
        if not self.database_path.is_file():
            raise ETFQuestMarketDataError(f"ETFQuest 行情数据库不存在: {self.database_path}")
        last_error: Exception | None = None
        for delay in (0.0, *_CONNECT_RETRY_DELAYS):
            if delay:
                time.sleep(delay)
            try:
                connection = duckdb.connect(str(self.database_path), read_only=True)
                try:
                    self._validate_schema(connection)
                    yield connection
                finally:
                    connection.close()
                return
            except duckdb.Error as exc:
                last_error = exc
                message = str(exc).lower()
                if "lock" not in message and "conflict" not in message and "busy" not in message:
                    raise ETFQuestMarketDataError("ETFQuest 行情数据库无法读取，请检查数据库版本") from exc
        raise ETFQuestMarketDataError("ETFQuest 行情数据库正在同步，请稍后重试") from last_error

    @staticmethod
    def _validate_schema(connection: duckdb.DuckDBPyConnection) -> None:
        rows = connection.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
        tables = {str(row[0]) for row in rows}
        missing = sorted(_ETFQUEST_TABLES - tables)
        if missing:
            raise ETFQuestMarketDataError(
                f"ETFQuest 行情数据库 schema 不兼容，缺少表: {', '.join(missing)}"
            )

    def list_stock_refs(
        self,
        vipdoc_path: str | Path,
        universe: str,
        universe_file: str | Path | None = None,
        instrument_types: tuple[InstrumentType, ...] | None = None,
        boards: tuple[InstrumentBoard, ...] | None = None,
    ) -> list[StockRef]:
        normalized_universe = str(universe or "all").strip().lower()
        if normalized_universe not in {"all", "sh", "sz"}:
            raise ValueError(f"不支持的扫描范围: {universe}")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    bars.symbol,
                    assets.name,
                    assets.type,
                    COUNT(*) AS bar_count,
                    MIN(bars.date) AS first_date,
                    MAX(bars.date) AS last_date,
                    MAX(bars.close ORDER BY bars.date) AS last_close,
                    MAX(bars.update_time) AS updated_at
                FROM market_data AS bars
                LEFT JOIN asset_info AS assets ON assets.symbol = bars.symbol
                WHERE bars.data_type = 'daily'
                GROUP BY bars.symbol, assets.name, assets.type
                ORDER BY bars.symbol
                """
            ).fetchall()

        refs: list[StockRef] = []
        for symbol, name, raw_type, count, first_date, last_date, last_close, _updated_at in rows:
            text_symbol = str(symbol or "").strip()
            instrument_type = _normalise_asset_type(raw_type, text_symbol)
            parsed = _split_symbol(text_symbol, instrument_type)
            if parsed is None:
                continue
            market, code = parsed
            board = _board_for(market, code, instrument_type)
            if normalized_universe != "all" and market.lower() != normalized_universe:
                continue
            if instrument_types and instrument_type not in instrument_types:
                continue
            if boards and board not in boards:
                continue
            refs.append(
                StockRef(
                    market=market,
                    code=code,
                    path=self.database_path,
                    instrument_type=instrument_type,
                    board=board,
                    name=str(name).strip() if name is not None else None,
                    provider="etfquest",
                    bar_count=int(count or 0),
                    first_date=first_date,
                    last_date=last_date,
                    last_close=float(last_close) if last_close is not None else None,
                )
            )
        return refs

    def stock_ref(self, vipdoc_path: str | Path, market: MarketCode, code: str) -> StockRef:
        normalized_market = str(market).strip().upper()
        normalized_code = str(code).strip()
        for ref in self.list_stock_refs(vipdoc_path, normalized_market.lower()):
            if ref.market == normalized_market and ref.code == normalized_code:
                return ref
        raise ValueError(f"找不到 ETFQuest 行情: {normalized_market} {normalized_code}")

    @staticmethod
    def _requested_frame(refs: Iterable[StockRef]) -> pd.DataFrame:
        rows = [{"symbol": f"{ref.market.lower()}{ref.code}"} for ref in refs]
        return pd.DataFrame(rows).drop_duplicates() if rows else pd.DataFrame(columns=["symbol"])

    def read_stock(self, ref: StockRef) -> pd.DataFrame:
        return self._read_many((ref,), start_date=None, end_date=None, warmup_bars=0)

    def read_many_stocks(
        self,
        refs: list[StockRef],
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        warmup_bars: int = 0,
    ) -> pd.DataFrame:
        if isinstance(warmup_bars, bool) or warmup_bars < 0:
            raise ValueError("行情预热 K 线数量必须是非负整数")
        if warmup_bars and start_date is None:
            raise ValueError("行情预热 K 线需要设置开始日期")
        return self._read_many(refs, start_date=start_date, end_date=end_date, warmup_bars=warmup_bars)

    def _read_many(
        self,
        refs: Iterable[StockRef],
        *,
        start_date: date | None,
        end_date: date | None,
        warmup_bars: int,
    ) -> pd.DataFrame:
        requested = list(refs)
        if not requested:
            return _empty_bars()
        symbols = self._requested_frame(requested)
        conditions = ["bars.data_type = 'daily'", "bars.symbol = requested.symbol"]
        parameters: list[object] = []
        if end_date is not None:
            conditions.append("bars.date <= ?")
            parameters.append(end_date)
        if start_date is not None:
            conditions.append("bars.date >= ?")
            parameters.append(start_date)
        columns = (
            "bars.symbol, bars.date, bars.open, bars.high, bars.low, bars.close, bars.volume, "
            "bars.update_time"
        )
        with self._connect() as connection:
            connection.register("_requested_symbols", symbols)
            try:
                if warmup_bars:
                    assert start_date is not None
                    query = f"""
                        WITH selected AS (
                            SELECT {columns}, ROW_NUMBER() OVER (
                                PARTITION BY bars.symbol ORDER BY bars.date DESC
                            ) AS warmup_rank
                            FROM market_data AS bars
                            INNER JOIN _requested_symbols AS requested
                                ON bars.symbol = requested.symbol
                            WHERE bars.data_type = 'daily' AND bars.date < ?
                        ), visible AS (
                            SELECT {columns}
                            FROM market_data AS bars
                            INNER JOIN _requested_symbols AS requested
                                ON bars.symbol = requested.symbol
                            WHERE {' AND '.join(conditions)}
                        )
                        SELECT symbol, date, open, high, low, close, volume, update_time
                        FROM visible
                        UNION ALL
                        SELECT symbol, date, open, high, low, close, volume, update_time
                        FROM selected WHERE warmup_rank <= ?
                        ORDER BY symbol, date
                    """
                    result = connection.execute(query, [start_date, *parameters, warmup_bars]).df()
                else:
                    query = f"""
                        SELECT {columns}
                        FROM market_data AS bars
                        INNER JOIN _requested_symbols AS requested
                            ON bars.symbol = requested.symbol
                        WHERE {' AND '.join(conditions)}
                        ORDER BY bars.symbol, bars.date
                    """
                    result = connection.execute(query, parameters).df()
            finally:
                connection.unregister("_requested_symbols")

        if result.empty:
            return _empty_bars()
        rows: list[pd.DataFrame] = []
        ref_map = {(ref.market.lower() + ref.code): ref for ref in requested}
        for symbol, group in result.groupby("symbol", sort=False):
            ref = ref_map.get(str(symbol).lower())
            if ref is None:
                continue
            frame = group.rename(columns={"symbol": "_source_symbol"}).copy()
            frame.insert(0, "market", ref.market)
            frame.insert(1, "code", ref.code)
            frame.insert(2, "instrument_type", ref.instrument_type)
            frame.insert(3, "board", ref.board)
            frame["amount"] = 0.0
            frame["source"] = "etfquest"
            frame["provider"] = "etfquest"
            frame["price_basis"] = "raw"
            frame["bar_status"] = "completed"
            frame["imported_at"] = frame["update_time"]
            frame = frame.rename(columns={"_source_symbol": "_ignored_symbol", "update_time": "_ignored_update_time"})
            rows.append(frame[_BAR_COLUMNS])
        return pd.concat(rows, ignore_index=True) if rows else _empty_bars()

    def count_many_stocks(
        self,
        refs: list[StockRef],
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> int:
        requested = list(refs)
        if not requested:
            return 0
        symbols = self._requested_frame(requested)
        conditions = ["bars.data_type = 'daily'", "bars.symbol = requested.symbol"]
        parameters: list[object] = []
        if start_date is not None:
            conditions.append("bars.date >= ?")
            parameters.append(start_date)
        if end_date is not None:
            conditions.append("bars.date <= ?")
            parameters.append(end_date)
        with self._connect() as connection:
            connection.register("_requested_symbols", symbols)
            try:
                row = connection.execute(
                    "SELECT COUNT(*) FROM market_data AS bars "
                    "INNER JOIN _requested_symbols AS requested ON bars.symbol = requested.symbol "
                    f"WHERE {' AND '.join(conditions)}",
                    parameters,
                ).fetchone()
            finally:
                connection.unregister("_requested_symbols")
        return int(row[0] if row and row[0] is not None else 0)

    def status(self) -> DataStoreStatus:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(DISTINCT symbol), COUNT(*), MIN(date), MAX(date), MAX(update_time)
                FROM market_data WHERE data_type = 'daily'
                """
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

    def read_trade_calendar(self) -> pd.DataFrame:
        with self._connect() as connection:
            return connection.execute(
                "SELECT trade_date FROM trade_calendar ORDER BY trade_date"
            ).df()


__all__ = [
    "ETFQuestMarketDataAdapter",
    "ETFQuestMarketDataError",
    "default_etfquest_database_path",
]
