"""Repository operations for instruments and daily bars."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import cast

import pandas as pd

from .day_format import classify_board
from .models import InstrumentBoard, InstrumentRef, InstrumentType, MarketCode, StockRef
from .staging import StageMergeReport
from .store_base import (
    _BAR_COLUMNS,
    _FRAME_COLUMNS,
    _WRITE_LOCK,
    _append_filter,
    _clean_name,
    _DuckDbStoreBase,
    _now,
)


def _sql_path_literal(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


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
                      AND instruments.name IS DISTINCT FROM names.name
                    """,
                    [_now()],
                )
                connection.unregister("_instrument_names")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return int(matched[0] if matched else 0)

    def ensure_instruments(self, instruments: Iterable[InstrumentRef]) -> int:
        """Register discovered online instruments without discarding existing bars."""

        self._store._require_writer()
        rows = [
            {
                "market": ref.market,
                "code": ref.code,
                "name": _clean_name(ref.name),
                "instrument_type": ref.instrument_type,
                "board": ref.board,
            }
            for ref in instruments
        ]
        if not rows:
            return 0
        frame = pd.DataFrame(rows).drop_duplicates(subset=["market", "code"], keep="last")
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
            try:
                connection.register("_discovered_instruments", frame)
                matched = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM instruments AS instruments
                    INNER JOIN _discovered_instruments AS discovered
                      ON instruments.market = discovered.market
                     AND instruments.code = discovered.code
                    """
                ).fetchone()
                connection.execute(
                    """
                    INSERT INTO instruments (
                        market, code, name, instrument_type, board, source_path,
                        first_date, last_date, bar_count, source_state, updated_at
                    )
                    SELECT market, code, name, instrument_type, board, NULL,
                           NULL, NULL, 0, 'active', ?
                    FROM _discovered_instruments
                    ON CONFLICT (market, code) DO UPDATE SET
                        name = COALESCE(excluded.name, instruments.name),
                        instrument_type = excluded.instrument_type,
                        board = excluded.board,
                        source_state = 'active',
                        updated_at = CASE
                            WHEN instruments.name IS DISTINCT FROM
                                     COALESCE(excluded.name, instruments.name)
                              OR instruments.instrument_type IS DISTINCT FROM
                                     excluded.instrument_type
                              OR instruments.board IS DISTINCT FROM excluded.board
                              OR instruments.source_state IS DISTINCT FROM 'active'
                            THEN excluded.updated_at
                            ELSE instruments.updated_at
                        END
                    """,
                    [_now()],
                )
                connection.unregister("_discovered_instruments")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        matched_count = int(matched[0] if matched else 0)
        return max(len(frame) - matched_count, 0)

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
                    provider="local",
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return len(incoming)

    def upsert_local_bars(self, frame: pd.DataFrame) -> tuple[int, int]:
        """Write local rows, overwriting any row on the same date.

        Local data wins over online data: every incoming ``(market, code,
        trade_date, price_basis)`` key replaces whatever already sits there,
        regardless of whether the previous row came from an online sync or an
        earlier local import. Dates absent from ``frame`` are left untouched so
        a partial import cannot silently drop history.

        Returns ``(written_rows, replaced_rows)``.
        """

        self._store._require_writer()
        if frame.empty:
            return (0, 0)
        incoming_frame = frame.copy(deep=True)
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
            raise ValueError(f"本地行情数据缺少字段: {', '.join(missing)}")
        if incoming_frame[["market", "code", "instrument_type", "board"]].isna().any().any():
            raise ValueError("本地行情数据缺少标的身份字段")
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
                    source="local",
                )
            )
        if not normalized_parts:
            return (0, 0)
        incoming = pd.concat(normalized_parts, ignore_index=True)
        incoming = incoming.drop_duplicates(
            subset=["market", "code", "trade_date", "price_basis"], keep="last"
        )
        incoming = incoming.sort_values(
            ["market", "code", "trade_date"], kind="stable"
        ).reset_index(drop=True)
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
            try:
                connection.register("_incoming_local_upsert", incoming)
                replaced_row = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM _incoming_local_upsert AS incoming
                    INNER JOIN daily_bars AS existing
                      ON existing.market = incoming.market
                     AND existing.code = incoming.code
                     AND existing.trade_date = incoming.trade_date
                     AND existing.price_basis = incoming.price_basis
                    """
                ).fetchone()
                connection.execute(
                    """
                    DELETE FROM daily_bars AS existing
                    USING _incoming_local_upsert AS incoming
                    WHERE existing.market = incoming.market
                      AND existing.code = incoming.code
                      AND existing.trade_date = incoming.trade_date
                      AND existing.price_basis = incoming.price_basis
                    """
                )
                connection.execute(
                    f"""
                    INSERT INTO daily_bars ({", ".join(_BAR_COLUMNS)})
                    SELECT {", ".join(f"incoming.{column}" for column in _BAR_COLUMNS)}
                    FROM _incoming_local_upsert AS incoming
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
                        provider="local",
                    )
                connection.unregister("_incoming_local_upsert")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return (len(incoming), int(replaced_row[0] if replaced_row else 0))

    def delete_bars(
        self,
        *,
        source: str | None = None,
        markets: Sequence[str] | None = None,
        codes: Sequence[str] | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> int:
        """Delete stored daily bars, returning how many rows were removed.

        This is the recovery path for a bad import. Rows written by
        ``upsert_local_bars`` outrank online rows forever, so without a way to
        drop them a single wrong import can only be undone by rebuilding the
        database. Pass ``source="local"`` to undo an import while keeping
        everything an online sync produced; omit every filter to clear the
        table outright.
        """

        self._store._require_writer()
        conditions: list[str] = []
        parameters: list[object] = []
        normalized_source = source.strip().lower() if source else ""
        if normalized_source:
            conditions.append("source = ?")
            parameters.append(normalized_source)
        _append_filter(
            conditions,
            parameters,
            "market",
            (item.strip().upper() for item in markets or ()),
        )
        _append_filter(conditions, parameters, "code", (item.strip() for item in codes or ()))
        if start_date is not None:
            conditions.append("trade_date >= ?")
            parameters.append(start_date)
        if end_date is not None:
            conditions.append("trade_date <= ?")
            parameters.append(end_date)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
            try:
                row = connection.execute(
                    f"SELECT COUNT(*) FROM daily_bars{where}", parameters
                ).fetchone()
                removed = int(row[0] if row else 0)
                if removed:
                    connection.execute(f"DELETE FROM daily_bars{where}", parameters)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return removed

    def upsert_online_bars(
        self,
        frame: pd.DataFrame,
        *,
        provider: str = "eastmoney",
    ) -> tuple[int, int]:
        """Upsert online rows without overwriting any local row."""

        self._store._require_writer()
        # "tdx" stays accepted for compatibility with callers replaying rows that
        # were downloaded by the removed legacy provider.
        if provider not in {"tdx", "eastmoney"}:
            raise ValueError(f"在线行情数据源无效: {provider}")
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
                    provider=provider,
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
                        provider=provider,
                    )
                connection.unregister("_incoming_online_bars")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return (eligible_count, existing_online_count)

    def merge_online_stage(self, stage_path: str | Path) -> StageMergeReport:
        """Merge one completed process-local staging database into the main store."""

        self._store._require_writer()
        path = Path(stage_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"行情分片数据库不存在: {path}")
        attached = False
        with _WRITE_LOCK, self._store._connect() as connection:
            try:
                connection.execute(f"ATTACH {_sql_path_literal(path)} AS sync_stage (READ_ONLY)")
                attached = True
                connection.execute(
                    """
                    CREATE TEMP TABLE _staged_online AS
                    SELECT market, code, instrument_type, board, trade_date,
                           open, high, low, close, volume, amount, source,
                           provider, price_basis, bar_status, imported_at
                    FROM sync_stage.staged_bars
                    """
                )
                staged_row = connection.execute("SELECT COUNT(*) FROM _staged_online").fetchone()
                existing_online_row = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM _staged_online AS incoming
                    INNER JOIN daily_bars AS existing
                      ON existing.market = incoming.market
                     AND existing.code = incoming.code
                     AND existing.trade_date = incoming.trade_date
                     AND existing.price_basis = incoming.price_basis
                     AND existing.source = 'online'
                    """
                ).fetchone()
                keys = connection.execute(
                    """
                    SELECT DISTINCT market, code, instrument_type, board, provider
                    FROM _staged_online
                    ORDER BY market, code
                    """
                ).fetchall()
                merged_row = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM _staged_online AS incoming
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM daily_bars AS local
                        WHERE local.market = incoming.market
                          AND local.code = incoming.code
                          AND local.trade_date = incoming.trade_date
                          AND local.price_basis = incoming.price_basis
                          AND local.source = 'local'
                    )
                    """
                ).fetchone()
                connection.execute("BEGIN")
                try:
                    connection.execute(
                        """
                        DELETE FROM daily_bars AS existing
                        USING _staged_online AS incoming
                        WHERE existing.market = incoming.market
                          AND existing.code = incoming.code
                          AND existing.trade_date = incoming.trade_date
                          AND existing.price_basis = incoming.price_basis
                          AND existing.source = 'online'
                        """
                    )
                    connection.execute(
                        """
                        INSERT INTO daily_bars (
                            market, code, instrument_type, board, trade_date,
                            open, high, low, close, volume, amount, source,
                            provider, price_basis, bar_status, imported_at
                        )
                        SELECT incoming.market, incoming.code, incoming.instrument_type,
                               incoming.board, incoming.trade_date, incoming.open,
                               incoming.high, incoming.low, incoming.close, incoming.volume,
                               incoming.amount, incoming.source, incoming.provider,
                               incoming.price_basis, incoming.bar_status, incoming.imported_at
                        FROM _staged_online AS incoming
                        WHERE NOT EXISTS (
                            SELECT 1
                            FROM daily_bars AS local
                            WHERE local.market = incoming.market
                              AND local.code = incoming.code
                              AND local.trade_date = incoming.trade_date
                              AND local.price_basis = incoming.price_basis
                              AND local.source = 'local'
                        )
                        """
                    )
                    for market, code, instrument_type, board, provider in keys:
                        self._store._refresh_instrument(
                            connection,
                            str(market),
                            str(code),
                            cast(InstrumentType, str(instrument_type)),
                            cast(InstrumentBoard, str(board)),
                            provider=str(provider),
                        )
                    connection.execute("COMMIT")
                except Exception:
                    connection.execute("ROLLBACK")
                    raise
            finally:
                connection.execute("DROP TABLE IF EXISTS _staged_online")
                if attached:
                    connection.execute("DETACH sync_stage")
        return StageMergeReport(
            staged_rows=int(staged_row[0] if staged_row else 0),
            merged_rows=int(merged_row[0] if merged_row else 0),
            existing_online_rows=int(existing_online_row[0] if existing_online_row else 0),
            affected_instruments=len(keys),
        )

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
            "volume, amount, source, provider, price_basis, bar_status, imported_at "
            f"FROM daily_bars WHERE {' AND '.join(conditions)} ORDER BY trade_date"
        )
        with self._store.read_session() as connection:
            return self._store._frame_from_query(connection.execute(query, parameters).df())

    def read_close_matrix(
        self,
        instruments: Iterable[InstrumentRef | StockRef] | None = None,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        """Read only close prices, shaped for basket-level curves.

        一篮子的等权曲线只用得到 close。全市场对照要读 5000+ 只票，把
        open/high/low/volume 一起拖出来会让传输量翻好几倍，而这些列一个都用不上，
        所以单独开一个口子。
        """

        conditions = ["bars.bar_status = 'completed'"]
        parameters: list[object] = []
        requested = None if instruments is None else list(instruments)
        symbols = requested or []
        if requested is not None and not symbols:
            return self._store._frame_from_query(pd.DataFrame())
        if start_date is not None:
            conditions.append("bars.trade_date >= ?")
            parameters.append(start_date)
        if end_date is not None:
            conditions.append("bars.trade_date <= ?")
            parameters.append(end_date)
        join_clause = ""
        if symbols:
            symbol_frame = pd.DataFrame(
                [{"market": item.market, "code": item.code} for item in symbols]
            ).drop_duplicates()
            join_clause = (
                " INNER JOIN _requested_symbols AS requested"
                " ON bars.market = requested.market AND bars.code = requested.code"
            )
        query = (
            "SELECT bars.market, bars.code, bars.trade_date, bars.close "
            f"FROM daily_bars AS bars{join_clause} WHERE {' AND '.join(conditions)} "
            "ORDER BY bars.trade_date"
        )
        with self._store._connect(read_only=True) as connection:
            if symbols:
                connection.register("_requested_symbols", symbol_frame)
            return self._store._frame_from_query(connection.execute(query, parameters).df())

    def latest_trade_date(self) -> date | None:
        """最新一根已完成日线的日期；库里没有任何 bar 时给 ``None``。

        行情是手动同步的，所以「最新交易日」几乎从不等于今天。任何要按"从某天到
        现在"取窗口的调用方都得先问它，否则会把一个空的区间当成"库里没数据"，
        再把原因错怪到标的上。
        """

        with self._store._connect(read_only=True) as connection:
            row = connection.execute(
                "SELECT MAX(trade_date) FROM daily_bars WHERE bar_status = 'completed'"
            ).fetchone()
        if row is None or row[0] is None:
            return None
        value = row[0]
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value)[:10])

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
        warmup_bars: int = 0,
        include_provisional: bool = False,
    ) -> pd.DataFrame:
        if isinstance(warmup_bars, bool) or warmup_bars < 0:
            raise ValueError("行情预热 K 线数量必须是非负整数")
        if warmup_bars > 0 and start_date is None:
            raise ValueError("行情预热 K 线需要设置开始日期")
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
        selected_columns = (
            "bars.market, bars.code, bars.instrument_type, bars.board, bars.trade_date, "
            "bars.open, bars.high, bars.low, bars.close, bars.volume, bars.amount, bars.source, "
            "bars.provider, bars.price_basis, bars.bar_status, bars.imported_at"
        )
        output_columns = selected_columns.replace("bars.", "")
        qualified_output_columns = ", ".join(
            f"selected.{column.strip()}" for column in output_columns.split(",")
        )
        join_clause = ""
        if symbols:
            join_clause = (
                " INNER JOIN _requested_symbols AS requested"
                " ON bars.market = requested.market AND bars.code = requested.code"
            )
        with self._store._connect(read_only=True) as connection:
            if symbols:
                connection.register("_requested_symbols", symbol_frame)
            try:
                if warmup_bars > 0:
                    assert start_date is not None
                    window_query = (
                        f"SELECT {selected_columns} FROM daily_bars AS bars{join_clause}{where}"
                    )
                    prior_conditions = ["bars.trade_date < ?"]
                    if not include_provisional:
                        prior_conditions.append("bars.bar_status = 'completed'")
                    prior_where = " WHERE " + " AND ".join(prior_conditions)
                    query = (
                        "WITH window_rows AS ("
                        f"{window_query}), prior_rows AS ("
                        f"SELECT {selected_columns}, ROW_NUMBER() OVER ("
                        "PARTITION BY bars.market, bars.code ORDER BY bars.trade_date DESC"
                        f") AS warmup_rank FROM daily_bars AS bars{join_clause}{prior_where}) "
                        f"SELECT {qualified_output_columns} FROM ("
                        f"SELECT {output_columns} FROM window_rows UNION ALL "
                        f"SELECT {output_columns} FROM prior_rows WHERE warmup_rank <= ?"
                        ") AS selected ORDER BY selected.market, selected.code, selected.trade_date"
                    )
                    prior_parameters = [start_date, warmup_bars]
                    result = connection.execute(
                        query,
                        [*parameters, *prior_parameters],
                    ).df()
                else:
                    query = (
                        f"SELECT {selected_columns} FROM daily_bars AS bars{join_clause}"
                        f"{where} ORDER BY bars.market, bars.code, bars.trade_date"
                    )
                    result = connection.execute(query, parameters).df()
            finally:
                if symbols:
                    connection.unregister("_requested_symbols")
            return self._store._frame_from_query(result)

    def count_many_bars(
        self,
        instruments: Iterable[InstrumentRef | StockRef] | None = None,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        include_provisional: bool = False,
    ) -> int:
        """Count completed bars for a bounded set of instruments."""

        requested = None if instruments is None else list(instruments)
        symbols = requested or []
        if requested is not None and not symbols:
            return 0
        if not symbols:
            raise ValueError("批量行情计数必须提供标的范围")
        symbol_frame = pd.DataFrame(
            [{"market": item.market, "code": item.code} for item in symbols]
        ).drop_duplicates()
        conditions: list[str] = []
        parameters: list[object] = []
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
            "SELECT COUNT(*) FROM daily_bars AS bars"
            " INNER JOIN _requested_symbols AS requested"
            " ON bars.market = requested.market AND bars.code = requested.code" + where
        )
        with self._store._connect(read_only=True) as connection:
            connection.register("_requested_symbols", symbol_frame)
            try:
                row = connection.execute(query, parameters).fetchone()
            finally:
                connection.unregister("_requested_symbols")
        return int(row[0]) if row and row[0] is not None else 0

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
                    bars.provider,
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
                   volume, amount, source, provider, price_basis, bar_status, imported_at
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
            conditions.append("instruments.market = ?")
            parameters.append(market)
        normalized_keyword = keyword.strip().upper()
        if normalized_keyword:
            conditions.append(
                "(instruments.code LIKE ? OR instruments.market LIKE ? "
                "OR instruments.instrument_type LIKE ?)"
            )
            like = f"%{normalized_keyword}%"
            parameters.extend([like, like, like])
        if not include_missing:
            conditions.append("instruments.source_state != 'missing'")
        _append_filter(conditions, parameters, "instruments.instrument_type", instrument_types)
        _append_filter(conditions, parameters, "instruments.board", boards)
        query = (
            "WITH latest AS ("
            "SELECT market, code, arg_max(close, trade_date) AS last_close "
            "FROM daily_bars GROUP BY market, code) "
            "SELECT instruments.market, instruments.code, instruments.name, "
            "instruments.instrument_type, instruments.board, instruments.source_path, "
            "instruments.last_provider, instruments.bar_count, instruments.first_date, "
            "instruments.last_date, latest.last_close "
            "FROM instruments LEFT JOIN latest "
            "ON latest.market = instruments.market AND latest.code = instruments.code"
            + (f" WHERE {' AND '.join(conditions)}" if conditions else "")
            + " ORDER BY instruments.market, instruments.code"
        )
        with _WRITE_LOCK, self._store._connect(read_only=True) as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            InstrumentRef(
                market=cast(MarketCode, market_value),
                code=cast(str, code),
                name=cast(str | None, name),
                instrument_type=cast(InstrumentType, instrument_type),
                board=cast(InstrumentBoard, board),
                source_path=Path(source_path) if source_path else None,
                provider=cast(str | None, provider),
                bar_count=int(bar_count) if bar_count is not None else None,
                first_date=first_date,
                last_date=last_date,
                last_close=float(last_close) if last_close is not None else None,
            )
            for (
                market_value,
                code,
                name,
                instrument_type,
                board,
                source_path,
                provider,
                bar_count,
                first_date,
                last_date,
                last_close,
            ) in rows
        ]
