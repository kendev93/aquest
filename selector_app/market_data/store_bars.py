"""Repository operations for instruments and daily bars."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import cast

import pandas as pd

from .adjustment import CorporateAction
from .day_format import classify_board
from .models import InstrumentBoard, InstrumentRef, InstrumentType, MarketCode, StockRef
from .staging import StageMergeReport
from .store_base import (
    _BAR_COLUMNS,
    _FRAME_COLUMNS,
    _WRITE_LOCK,
    DEFAULT_PRICE_BASIS,
    _append_filter,
    _clean_name,
    _DuckDbStoreBase,
    _now,
)


def corporate_actions_from_frame(frame: pd.DataFrame) -> list[CorporateAction]:
    """把事件表查询结果转成 :class:`CorporateAction` 列表。

    放在 store 层是因为"行 ↔ 领域对象"的转换是这一层的职责：调用方（回测上下文、
    指标预计算、校验工具）都只该面对领域对象，不该自己拼 dataclass。
    """

    if frame.empty:
        return []
    dates = pd.to_datetime(frame["ex_date"]).dt.date
    return [
        CorporateAction(
            market=str(row.market),
            code=str(row.code),
            ex_date=ex_date,
            share_ratio=float(row.share_ratio),
            cash_per_share=float(row.cash_per_share),
            factor_ratio=float(row.factor_ratio),
            action_type=str(row.action_type),
        )
        for ex_date, row in zip(dates, frame.itertuples(index=False))
    ]


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
        price_basis: str | None = None,
    ) -> int:
        """Replace one instrument atomically; local data wins over online data.

        ``price_basis`` 缺省按 source 推断为 raw（本地 vipdoc 是不复权的）；
        确有后复权本地文件时显式传 ``"hfq"``，否则默认读取路径看不到它。
        """

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
            price_basis=price_basis,
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

    def upsert_local_bars(
        self, frame: pd.DataFrame, *, price_basis: str | None = None
    ) -> tuple[int, int]:
        """Write local rows, overwriting any row on the same date.

        Local data wins over online data: every incoming ``(market, code,
        trade_date, price_basis)`` key replaces whatever already sits there,
        regardless of whether the previous row came from an online sync or an
        earlier local import. Dates absent from ``frame`` are left untouched so
        a partial import cannot silently drop history.

        ``price_basis`` 缺省按 source 推断为 raw；后复权本地文件显式传 ``"hfq"``。

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
                    price_basis=price_basis,
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
                    # 覆盖元数据不跟着删就会变成"数据没了但库以为自己还有"，
                    # 于是先删后下只补首尾两段，中间那段静默消失。
                    self._store._refresh_instrument_coverage(connection)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return removed

    def reset_instrument_coverage(self, refs: Sequence[tuple[str, str]]) -> int:
        """Zero the coverage columns of the given instruments, returning how many moved.

        先删后下的入口用得上：删除可能一行都没命中（上一次重刷删了没补回来，
        或者标的本来就是空的），此时 ``delete_bars`` 不会重算覆盖，留着旧区间
        会让这次重下又只补首尾两段——而那两段对一只早已上市的票都是空的。
        """

        self._store._require_writer()
        symbols = pd.DataFrame(
            [
                {"market": market.strip().upper(), "code": code.strip()}
                for market, code in refs
                if market.strip() and code.strip()
            ]
        ).drop_duplicates()
        if symbols.empty:
            return 0
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
            try:
                connection.register("_coverage_reset", symbols)
                try:
                    row = connection.execute(
                        """
                        SELECT COUNT(*)
                        FROM instruments AS instruments
                        INNER JOIN _coverage_reset AS reset
                            ON instruments.market = reset.market
                           AND instruments.code = reset.code
                        WHERE instruments.bar_count > 0
                           OR instruments.first_date IS NOT NULL
                           OR instruments.last_date IS NOT NULL
                        """
                    ).fetchone()
                    affected = int(row[0] if row else 0)
                    if affected:
                        connection.execute(
                            """
                            UPDATE instruments SET
                                first_date = NULL, last_date = NULL,
                                bar_count = 0, updated_at = ?
                            FROM _coverage_reset AS reset
                            WHERE instruments.market = reset.market
                              AND instruments.code = reset.code
                            """,
                            [_now()],
                        )
                finally:
                    connection.unregister("_coverage_reset")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return affected

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
                changed_key_rows = connection.execute(
                    """
                    SELECT DISTINCT incoming.market, incoming.code
                    FROM _incoming_online_bars AS incoming
                    LEFT JOIN daily_bars AS existing
                      ON existing.market = incoming.market
                     AND existing.code = incoming.code
                     AND existing.trade_date = incoming.trade_date
                     AND existing.price_basis = incoming.price_basis
                     AND existing.source = 'online'
                    WHERE NOT EXISTS (
                        SELECT 1 FROM daily_bars AS local
                        WHERE local.market = incoming.market
                          AND local.code = incoming.code
                          AND local.trade_date = incoming.trade_date
                          AND local.price_basis = incoming.price_basis
                          AND local.source = 'local'
                    )
                      AND (
                          existing.market IS NULL
                          OR existing.instrument_type IS DISTINCT FROM incoming.instrument_type
                          OR existing.board IS DISTINCT FROM incoming.board
                          OR existing.open IS DISTINCT FROM incoming.open
                          OR existing.high IS DISTINCT FROM incoming.high
                          OR existing.low IS DISTINCT FROM incoming.low
                          OR existing.close IS DISTINCT FROM incoming.close
                          OR existing.volume IS DISTINCT FROM incoming.volume
                          OR existing.amount IS DISTINCT FROM incoming.amount
                          OR existing.provider IS DISTINCT FROM incoming.provider
                          OR existing.bar_status IS DISTINCT FROM incoming.bar_status
                      )
                    """
                ).fetchall()
                changed_keys = {
                    (str(market).strip().upper(), str(code).strip())
                    for market, code in changed_key_rows
                }
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
                        force_updated_at=(str(market).strip().upper(), str(code).strip())
                        in changed_keys,
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
                changed_key_rows = connection.execute(
                    """
                    SELECT DISTINCT incoming.market, incoming.code
                    FROM _staged_online AS incoming
                    LEFT JOIN daily_bars AS existing
                      ON existing.market = incoming.market
                     AND existing.code = incoming.code
                     AND existing.trade_date = incoming.trade_date
                     AND existing.price_basis = incoming.price_basis
                     AND existing.source = 'online'
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM daily_bars AS local
                        WHERE local.market = incoming.market
                          AND local.code = incoming.code
                          AND local.trade_date = incoming.trade_date
                          AND local.price_basis = incoming.price_basis
                          AND local.source = 'local'
                    )
                      AND (
                          existing.market IS NULL
                          OR existing.instrument_type IS DISTINCT FROM incoming.instrument_type
                          OR existing.board IS DISTINCT FROM incoming.board
                          OR existing.open IS DISTINCT FROM incoming.open
                          OR existing.high IS DISTINCT FROM incoming.high
                          OR existing.low IS DISTINCT FROM incoming.low
                          OR existing.close IS DISTINCT FROM incoming.close
                          OR existing.volume IS DISTINCT FROM incoming.volume
                          OR existing.amount IS DISTINCT FROM incoming.amount
                          OR existing.provider IS DISTINCT FROM incoming.provider
                          OR existing.bar_status IS DISTINCT FROM incoming.bar_status
                      )
                    """
                ).fetchall()
                changed_keys = {
                    (str(market).strip().upper(), str(code).strip())
                    for market, code in changed_key_rows
                }
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
                            force_updated_at=(str(market).strip().upper(), str(code).strip())
                            in changed_keys,
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
        price_basis: str | None = DEFAULT_PRICE_BASIS,
    ) -> pd.DataFrame:
        """读取单标日线，默认只返回统一口径（raw 不复权）的 K 线。

        ``price_basis=None`` 表示不过滤（维护工具需要看到全部口径时用）。
        """

        market = market.strip().upper()
        code = code.strip()
        conditions = ["market = ?", "code = ?"]
        parameters: list[object] = [market, code]
        if price_basis is not None:
            conditions.append("price_basis = ?")
            parameters.append(price_basis)
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

        conditions = ["bars.bar_status = 'completed'", "bars.price_basis = ?"]
        parameters: list[object] = [DEFAULT_PRICE_BASIS]
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
                " AND price_basis = ?",
                [DEFAULT_PRICE_BASIS],
            ).fetchone()
        if row is None or row[0] is None:
            return None
        value = row[0]
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value)[:10])

    def save_market_trading_calendar(
        self,
        trade_dates: Iterable[date],
        *,
        source: str,
    ) -> int:
        """Persist a rolling snapshot of observed mainland market sessions."""

        self._store._require_writer()
        dates = tuple(sorted({item for item in trade_dates if isinstance(item, date)}))
        normalized_source = source.strip()
        if not dates:
            return 0
        if not normalized_source:
            raise ValueError("交易日历来源不能为空")
        now = _now()
        rows = [(item, normalized_source, now) for item in dates]
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.executemany(
                """
                INSERT INTO market_trading_calendar (trade_date, source, fetched_at)
                VALUES (?, ?, ?)
                ON CONFLICT (trade_date) DO UPDATE SET
                    source = excluded.source,
                    fetched_at = excluded.fetched_at
                """,
                rows,
            )
            connection.execute(
                "DELETE FROM market_trading_calendar WHERE trade_date < ?",
                [dates[0]],
            )
        return len(dates)

    def upsert_corporate_actions(
        self,
        actions: Sequence[CorporateAction],
        *,
        source: str = "online",
        provider: str = "eastmoney",
    ) -> int:
        """写入分红送转事件，按 ``(market, code, ex_date)`` 覆盖。

        写入本身就是一道校验：``factor_ratio`` 在 :class:`CorporateAction` 构造时
        就已验证为正有限数，算不出因子的事件根本进不来 —— 这是刻意的，宁可缺
        一条事件（校验工具会报出来，表现为某个除权日仍有跳空），也不能写一条错
        因子去静默污染全部历史指标。
        """

        self._store._require_writer()
        normalized_source = source.strip()
        normalized_provider = provider.strip()
        if not normalized_source or not normalized_provider:
            raise ValueError("事件来源与 provider 不能为空")
        now = _now()
        rows = [
            (
                action.market.strip().upper(),
                action.code.strip(),
                action.ex_date,
                float(action.share_ratio),
                float(action.cash_per_share),
                float(action.factor_ratio),
                action.action_type.strip() or "dividend",
                normalized_source,
                normalized_provider,
                now,
            )
            for action in actions
        ]
        if not rows:
            return 0
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.executemany(
                """
                INSERT INTO corporate_actions (
                    market, code, ex_date, share_ratio, cash_per_share,
                    factor_ratio, action_type, source, provider, imported_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (market, code, ex_date) DO UPDATE SET
                    share_ratio = excluded.share_ratio,
                    cash_per_share = excluded.cash_per_share,
                    factor_ratio = excluded.factor_ratio,
                    action_type = excluded.action_type,
                    source = excluded.source,
                    provider = excluded.provider,
                    imported_at = excluded.imported_at
                """,
                rows,
            )
        return len(rows)

    def read_corporate_actions(
        self,
        market: str,
        code: str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        """读取单标的事件，按除权日升序。"""

        conditions = ["market = ?", "code = ?"]
        parameters: list[object] = [market.strip().upper(), code.strip()]
        if start_date is not None:
            conditions.append("ex_date >= ?")
            parameters.append(start_date)
        if end_date is not None:
            conditions.append("ex_date <= ?")
            parameters.append(end_date)
        query = (
            "SELECT market, code, ex_date, share_ratio, cash_per_share, factor_ratio, "
            "action_type, source, provider, imported_at "
            f"FROM corporate_actions WHERE {' AND '.join(conditions)} ORDER BY ex_date"
        )
        with self._store.read_session() as connection:
            return self._corporate_actions_frame(connection.execute(query, parameters).df())

    def read_corporate_actions_batch(
        self,
        instruments: Iterable[InstrumentRef | StockRef | tuple[str, str]],
    ) -> pd.DataFrame:
        """一次取回整批标的的事件，供全市场预加载使用。

        逐标的查事件表会在全市场 5000+ 只的规模下变成 5000 次查询。事件表本身很小
        （五千只 × 平均三十个事件 ≈ 15 万行），按 ``(market, code)`` 组合条件一次
        拉回来更划算，也避免把 N+1 带进回测的预热路径。
        """

        pairs = sorted(
            {
                (item[0].strip().upper(), item[1].strip())
                if isinstance(item, tuple)
                else (item.market.strip().upper(), item.code.strip())
                for item in instruments
            }
        )
        if not pairs:
            return self._store._frame_from_query(pd.DataFrame())
        conditions = " OR ".join(["(market = ? AND code = ?)"] * len(pairs))
        parameters: list[object] = [value for pair in pairs for value in pair]
        query = (
            "SELECT market, code, ex_date, share_ratio, cash_per_share, factor_ratio, "
            "action_type, source, provider, imported_at "
            f"FROM corporate_actions WHERE {conditions} ORDER BY market, code, ex_date"
        )
        with self._store.read_session() as connection:
            return self._corporate_actions_frame(connection.execute(query, parameters).df())

    @staticmethod
    def _corporate_actions_frame(frame: pd.DataFrame) -> pd.DataFrame:
        """规范化事件表查询结果。

        刻意不走 ``_frame_from_query``：那个函数是行情专用的，会把 ``trade_date``
        改名成 ``date``。事件表的日期列叫 ``ex_date``，语义是除权除息日而不是
        成交日，套用那套改名只会让调用方拿错列。
        """

        if frame.empty:
            return frame
        normalized = frame.copy()
        normalized["ex_date"] = pd.to_datetime(normalized["ex_date"])
        return normalized

    def corporate_actions_version(self) -> str:
        """全库事件表内容哈希，指标缓存靠它判断"事件有没有变过"。

        用内容聚合哈希而不是 ``COUNT`` / ``MAX(imported_at)``：后者在事件被改写
        成相同时间戳时会漏判，而哈希直接反映内容。事件全空时返回空串 —— 调用方
        把它当成"没有事件"处理即可，不必特殊分支。
        """

        with self._store.read_session() as connection:
            row = connection.execute(
                """
                SELECT md5(string_agg(
                    market || '|' || code || '|' || CAST(ex_date AS VARCHAR) || '|' ||
                    CAST(share_ratio AS VARCHAR) || '|' || CAST(cash_per_share AS VARCHAR)
                    || '|' || CAST(factor_ratio AS VARCHAR),
                    '\n' ORDER BY market, code, ex_date
                )) AS digest
                FROM corporate_actions
                """
            ).fetchone()
        digest = row[0] if row else None
        return str(digest) if digest else ""

    def read_trading_dates(self) -> list[date]:
        """返回库中实际出现过的交易日，升序。

        刻意不读 ``market_trading_calendar``：那张表需要单独回填，库里可能
        还没数据，而它是空的时候 :meth:`latest_market_trading_date` 会一直
        返回 ``None``。行情自身的日期并集等价于交易日历 —— 单只股票停牌
        不影响其他股票，那天仍是交易日。
        """

        with self._store.read_session() as connection:
            frame = connection.execute(
                "SELECT DISTINCT trade_date FROM daily_bars ORDER BY trade_date"
            ).df()
        # ``.dt.date`` 而不是逐个 isinstance：pandas 的 Timestamp 是 datetime.date
        # 的子类，isinstance 会放它过去，调用方拿到的就是一列 Timestamp。它们和
        # 纯 date 混进 bisect 会抛 TypeError，所以在这里强制降级成 date。
        return list(frame["trade_date"].dt.date)

    def rebuild_trading_calendar_from_bars(self) -> int:
        """用行情自身的日期并集重建交易日历表，返回写入天数。

        这张表正常由 ``auto_sync`` 从上证综指日线写入，但它被卡在两个前提上：
        收盘后才执行（``AUTO_SYNC_AFTER_CLOSE``），且依赖上游 adapter 提供
        ``fetch_trading_calendar``。任何一环不满足就整段静默跳过 —— 表会一直是
        空的，而 :meth:`latest_market_trading_date` 会一直返回 ``None``，没有
        任何报错指向它。

        从 bar 反推是等价的：日线本来就只在交易日生成，全市场没有任何一天是
        "所有股票都停牌"的，所以日期并集就是实际交易日历。``source`` 标成
        ``derived_from_bars`` 而不是冒充 provider，审计时能看出这不是交易所口径。

        只删除早于最早 bar 的记录，所以 provider 写入的、更靠近今天的日历不受影响。
        """

        dates = self.read_trading_dates()
        if not dates:
            return 0
        return self.save_market_trading_calendar(dates, source="derived_from_bars")

    def read_price_gap_candidates(self) -> pd.DataFrame:
        """找出"跌幅超出板块跌停幅度、但事件表无记录"的 bar。

        这是复权事件漏记的检测入口。判据是市场自身的硬约束：主板跌停就是
        -10.00%、创业板与科创板 -20%，所以跌幅超过该幅度（留 0.5% 缓冲）就不
        可能是市场波动，只能发生过股本变化。未知 ``board`` 一律按 -20.5%
        处理 —— 宁可漏检（那只是没修好）不可误判（那会往事件表写进错因子）。

        **只做"是除权"这一半判断，且不含停牌过滤。** 跌幅是否可信取决于
        前后两根 bar 是否紧邻交易日：跨越停牌的复牌跌幅不受涨跌幅约束，
        那类跳空必须排除，交给调用方按交易日历判定。

        ``ROW_NUMBER() <= 5`` 排掉的是**新股**：创业板与科创板新股上市前 5 个
        交易日不设涨跌幅限制，期间出现 -50% 很正常。688615 在 2024-10-09
        上市第 5 日从 350 跌到 175.11，若当成 10 转 10 的除权就会写进一条
        因子 1.25 的错事件，留下的跳空还有 -37%。用序号而不是日期判断，是因为
        新股期停牌极少，按 bar 序号更贴合"第几个交易日"的监管口径。
        """

        with self._store.read_session() as connection:
            return self._store._frame_from_query(
                connection.execute(
                    """
                    WITH bars AS (
                        SELECT market, code, board, trade_date, open, close,
                            LAG(trade_date) OVER (
                                PARTITION BY market, code ORDER BY trade_date
                            ) AS prev_date,
                            LAG(close) OVER (
                                PARTITION BY market, code ORDER BY trade_date
                            ) AS prev_close,
                            ROW_NUMBER() OVER (
                                PARTITION BY market, code ORDER BY trade_date
                            ) AS rn
                        FROM daily_bars
                        WHERE price_basis = 'raw' AND bar_status = 'completed'
                    )
                    SELECT b.market, b.code, b.board, b.trade_date, b.prev_date,
                           b.prev_close, b.open, b.close
                    FROM bars b
                    LEFT JOIN corporate_actions a
                        ON a.market = b.market AND a.code = b.code
                        AND a.ex_date = b.trade_date
                    WHERE b.rn > 5
                        AND b.prev_close > 0
                        AND b.open > 0
                        AND a.ex_date IS NULL
                        AND b.close / b.prev_close - 1
                            < CASE b.board WHEN 'main' THEN -0.105 ELSE -0.205 END
                    ORDER BY b.market, b.code, b.trade_date
                    """
                ).df()
            )

    def latest_market_trading_date(self, on_or_before: date) -> date | None:
        with self._store._connect(read_only=True) as connection:
            row = connection.execute(
                "SELECT MAX(trade_date) FROM market_trading_calendar WHERE trade_date <= ?",
                [on_or_before],
            ).fetchone()
        if row is None or row[0] is None:
            return None
        value = row[0]
        return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])

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
        # 统一口径：只读 hfq，防止残留的旧 raw 数据混进全市场研究。
        conditions: list[str] = ["bars.price_basis = ?"]
        parameters: list[object] = [DEFAULT_PRICE_BASIS]
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
                    prior_conditions = ["bars.price_basis = ?", "bars.trade_date < ?"]
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
                    prior_parameters = [DEFAULT_PRICE_BASIS, start_date, warmup_bars]
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
