"""Repository operations for instruments and daily bars."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from pathlib import Path
from typing import cast

import pandas as pd

from .day_format import classify_board
from .models import InstrumentBoard, InstrumentRef, InstrumentType, MarketCode, StockRef
from .store_base import (
    _BAR_COLUMNS,
    _FRAME_COLUMNS,
    _WRITE_LOCK,
    _append_filter,
    _clean_name,
    _DuckDbStoreBase,
    _now,
)


class DuckDbBarsStore:
    """Read and write instrument metadata and daily bars."""

    def __init__(self, store: _DuckDbStoreBase) -> None:
        self._store = store

    def update_instrument_names(self, names: Mapping[tuple[str, str], str | None]) -> int:
        """Update names for instruments already present in the repository."""

        self._store._require_writer()
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
        with _WRITE_LOCK, self._store._connect() as connection:
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

        self._store._require_writer()
        market = market.strip().upper()
        code = code.strip()
        resolved_board = board or classify_board(market, code)
        if resolved_board is None:
            raise ValueError(f"证券板块无效: {market} {code}")
        incoming = self._store._normalize_frame(
            frame,
            market=market,
            code=code,
            instrument_type=instrument_type,
            board=resolved_board,
            source="local",
        )
        with _WRITE_LOCK, self._store._connect() as connection:
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
                self._store._refresh_instrument(
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

        self._store._require_writer()
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
                self._store._normalize_frame(
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
        with _WRITE_LOCK, self._store._connect() as connection:
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
                    self._store._refresh_instrument(
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
        with self._store._connect(read_only=True) as connection:
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
        with self._store._connect(read_only=True) as connection:
            return self._store._frame_from_query(connection.execute(query, parameters).df())

    def has_provisional_bars(self, market: str, code: str) -> bool:
        """Return whether a symbol still needs its intraday bar finalized."""

        with self._store._connect(read_only=True) as connection:
            row = connection.execute(
                "SELECT 1 FROM daily_bars "
                "WHERE market = ? AND code = ? AND bar_status = 'provisional' LIMIT 1",
                [market.strip().upper(), code.strip()],
            ).fetchone()
        return row is not None

    def list_provisional_symbols(self) -> set[tuple[str, str]]:
        """Read all symbols with provisional bars in one bounded query."""

        with self._store._connect(read_only=True) as connection:
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
            return self._store._frame_from_query(pd.DataFrame())
        if symbols:
            symbol_frame = pd.DataFrame(
                [{"market": item.market, "code": item.code} for item in symbols]
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
        with self._store._connect(read_only=True) as connection:
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
            return self._store._frame_from_query(result)

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
            return self._store._frame_from_query(pd.DataFrame())
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
        with self._store._connect(read_only=True) as connection:
            connection.register("_requested_symbols", symbol_frame)
            try:
                result = connection.execute(query, [count]).df()
            finally:
                connection.unregister("_requested_symbols")
        return self._store._frame_from_query(result)

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
        with self._store._connect(read_only=True) as connection:
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
