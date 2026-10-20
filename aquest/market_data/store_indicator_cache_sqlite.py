"""SQLite-backed repository for the persisted indicator caches.

DuckDB is excellent at the narrow ``daily_bars`` table but measurably bad at
large blobs: writing 2k cache rows takes 20.4s versus 0.28s in SQLite, and a
single point lookup 7.3ms versus 0.07ms.  Batch reads — which is what the
screening and backtest paths actually issue — are a wash between the two, so
moving the two derived cache tables out costs nothing and removes the dominant
write cost of a full precompute run (25.5% of wall time).

The source-of-truth tables stay in DuckDB: ``data_revisions`` still reads
``instruments``, because the revision is derived from stored bars.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import weakref
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import cast

from .store_base import _WRITE_LOCK, _DuckDbStoreBase, _now, _timestamp_text
from .store_indicator_cache import (
    _CHUNK_FINGERPRINT_MARKER,
    DuckDbIndicatorCacheStore,
    _fingerprint_matches_revision,
)

logger = logging.getLogger(__name__)

#: ``_connection`` 用这条哨兵消息表示「只读探测且缓存文件还不存在」；``_query``
#: 认得它并当成"没缓存"，而不是真的失败。
_ABSENT_FILE_MESSAGE = "indicator cache file is absent"


def _is_missing_cache_error(exc: sqlite3.OperationalError) -> bool:
    """区分「还没这套缓存」和「缓存读失败」。

    前者（缺表 / 缺列 / 只读探测时文件尚未创建）等价于"没缓存"，上层按未命中处理
    即可；后者必须抛出去——吞成"没缓存"会触发整批重算，且日志里什么都不留。
    """

    message = str(exc).lower()
    return (
        "no such table" in message or "no such column" in message or message == _ABSENT_FILE_MESSAGE
    )


_BUILTIN_COLUMNS = (
    "market",
    "code",
    "timeframe",
    "price_basis",
    "formula_id",
    "formula_version",
    "data_fingerprint",
    "bar_offset",
    "data_start",
    "data_end",
    "data_length",
    "values_blob",
    "signals_blob",
    "computed_at",
)

_CUSTOM_COLUMNS = (
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
)

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS indicator_cache (
        market TEXT NOT NULL,
        code TEXT NOT NULL,
        timeframe TEXT NOT NULL,
        price_basis TEXT NOT NULL,
        formula_id TEXT NOT NULL,
        formula_version TEXT NOT NULL,
        data_fingerprint TEXT NOT NULL,
        bar_offset INTEGER NOT NULL,
        data_start TEXT,
        data_end TEXT,
        data_length INTEGER NOT NULL,
        values_blob BLOB NOT NULL,
        signals_blob BLOB NOT NULL,
        computed_at TEXT NOT NULL,
        PRIMARY KEY (
            market, code, timeframe, price_basis, formula_id,
            formula_version, data_fingerprint, bar_offset
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS custom_indicator_cache (
        market TEXT NOT NULL,
        code TEXT NOT NULL,
        timeframe TEXT NOT NULL,
        price_basis TEXT NOT NULL,
        formula_hash TEXT NOT NULL,
        parameter_hash TEXT NOT NULL,
        engine_version TEXT NOT NULL,
        parameter_json TEXT NOT NULL,
        value_names_json TEXT NOT NULL,
        signal_names_json TEXT NOT NULL,
        data_fingerprint TEXT NOT NULL,
        data_start TEXT,
        data_end TEXT,
        data_length INTEGER NOT NULL,
        minimum_bars INTEGER NOT NULL,
        recommended_bars INTEGER NOT NULL,
        values_blob BLOB NOT NULL,
        signals_blob BLOB NOT NULL,
        computed_at TEXT NOT NULL,
        PRIMARY KEY (
            market, code, timeframe, price_basis, formula_hash,
            parameter_hash, engine_version, data_fingerprint
        )
    )
    """,
    # 批量读只带 (market, code, timeframe, price_basis, data_fingerprint) 去 join，
    # 主键索引最左列就是它们，但排序按 formula_id/bar_offset，显式索引省一次排序。
    """
    CREATE INDEX IF NOT EXISTS idx_indicator_cache_series
        ON indicator_cache (market, code, timeframe, price_basis, data_fingerprint)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_indicator_cache_freshness
        ON indicator_cache (data_fingerprint, formula_id)
    """,
)


def _close_cached_connection(connection: sqlite3.Connection) -> None:
    """Close an idle per-thread connection when its owner is collected."""

    try:
        connection.close()
    except sqlite3.Error:  # pragma: no cover - defensive finalizer path
        logger.debug("SQLite 指标缓存连接关闭失败", exc_info=True)


class _SqliteConnectionOwner:
    """Keep a cached connection alive only as long as its thread-local owner."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        # The callback closes over only the connection, not this owner. weakref.finalize
        # therefore runs when thread exit or store-cycle collection removes the local value.
        self._finalizer = weakref.finalize(self, _close_cached_connection, connection)


def _date_text(value: object) -> str | None:
    """Normalize a caller-supplied date into the ISO text SQLite stores."""

    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _as_date(value: object) -> date | None:
    """Restore a stored ISO text column as ``date``, matching DuckDB's driver."""

    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _as_datetime(value: object) -> datetime | None:
    """Restore a stored timestamp column as ``datetime``."""

    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = str(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


class SqliteIndicatorCacheStore:
    """Read and write versioned derived indicator generations in SQLite."""

    def __init__(
        self,
        store: _DuckDbStoreBase,
        database_path: str | Path | None = None,
    ) -> None:
        self._store = store
        # 版本号来自行情主表，仍然走 DuckDB。
        self._revisions = DuckDbIndicatorCacheStore(store)
        self._path = (
            Path(database_path)
            if database_path is not None
            else default_indicator_cache_path(store.database_path)
        )
        self._local = threading.local()
        # 建表推迟到真正读写缓存的时候。有些调用方（测试里的模块级 app、
        # 只读探测）只是构造 store 就丢在一边，无条件建库会在它们不该写盘的
        # 地方凭空多出一个文件。
        self._schema_ready = False

    @property
    def database_path(self) -> Path:
        return self._path

    # ------------------------------------------------------------------ schema

    def _ensure_schema(self) -> None:
        """Create both derived tables once, in a single transaction.

        只读副本（策略 worker 用 ``read_only=True`` 打开同一个库）不该建表：
        表由可写的那个 store 建好，这里直接连上去读就行。
        """

        if self._schema_ready or self._store.read_only:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self._path), timeout=30.0, isolation_level=None)
        try:
            connection.execute("BEGIN")
            for statement in _SCHEMA:
                connection.execute(statement)
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        self._schema_ready = True

    def _connection(self) -> sqlite3.Connection:
        """Per-thread connection; SQLite connections are not shareable."""

        if self._store.read_only and not self._path.exists():
            # 只读探测不该把空文件落在盘上；``_query`` 会把这个错当“没缓存”。
            raise sqlite3.OperationalError(_ABSENT_FILE_MESSAGE)
        owner = cast(
            _SqliteConnectionOwner | None,
            getattr(self._local, "owner", None),
        )
        if owner is None:
            self._ensure_schema()
            # isolation_level=None 关掉 sqlite3 的隐式 BEGIN：写路径自己发
            # BEGIN IMMEDIATE，否则两条 BEGIN 会撞成
            # "cannot start a transaction within a transaction"。
            # Connection access stays thread-local. Disable sqlite's thread check only so
            # weakref.finalize can close an idle connection if GC collects the store/local
            # cycle on a different thread; an active operation retains the store and owner.
            connection = sqlite3.connect(
                str(self._path),
                timeout=30.0,
                isolation_level=None,
                check_same_thread=False,
            )
            try:
                connection.execute("PRAGMA busy_timeout=30000")
                # WAL lets the read paths run while a precompute batch is
                # writing.  It needs shared memory on the volume, which not
                # every mount provides — falling back to the rollback journal
                # costs concurrency, not correctness.
                try:
                    connection.execute("PRAGMA journal_mode=WAL")
                    connection.execute("PRAGMA synchronous=NORMAL")
                except sqlite3.OperationalError:  # pragma: no cover - depends on mount
                    logger.debug("SQLite WAL 不可用，退回默认日志模式", exc_info=True)
                owner = _SqliteConnectionOwner(connection)
                self._local.owner = owner
            except BaseException:
                _close_cached_connection(connection)
                raise
        return owner.connection

    def _query(
        self,
        sql: str,
        parameters: Sequence[object] = (),
    ) -> list[tuple[object, ...]]:
        """Run a read query, treating a missing table as "nothing cached".

        Mirrors the DuckDB backend, which swallows ``CatalogException``: a store
        opened before the derived tables existed must still serve source bars.

        只有「缺表 / 缺列」能当成没缓存。``OperationalError`` 同样覆盖
        ``database is locked``、``disk I/O error`` 这类瞬时故障——把它们也吞成 ``[]``
        会让上层认为全部未缓存，表现为毫无征兆的整批重算。
        """

        try:
            return self._connection().execute(sql, parameters).fetchall()
        except sqlite3.OperationalError as exc:
            if _is_missing_cache_error(exc):
                return []
            logger.warning("指标缓存 SQLite 查询失败：%s", exc)
            raise

    def _query_one(
        self,
        sql: str,
        parameters: Sequence[object] = (),
    ) -> tuple[object, ...] | None:
        rows = self._query(sql, parameters)
        return rows[0] if rows else None

    # ------------------------------------------------------------- revisions

    def data_revisions(
        self,
        refs: Iterable[tuple[str, str]] | None = None,
    ) -> dict[tuple[str, str], str]:
        return self._revisions.data_revisions(refs)

    def data_revision(self, market: str, code: str) -> str | None:
        return self._revisions.data_revision(market, code)

    # ------------------------------------------------------------------ reads

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
    ) -> list[dict[str, object]]:
        rows = self._query(
            """
            SELECT market, code, timeframe, price_basis, formula_id,
                   formula_version, data_fingerprint, bar_offset, data_start,
                   data_end, data_length, values_blob, signals_blob, computed_at
            FROM indicator_cache
            WHERE market = ? AND code = ? AND timeframe = ? AND price_basis = ?
              AND formula_id = ? AND formula_version = ?
              AND (data_fingerprint = ? OR substr(data_fingerprint, 1,
                   length(? || '|chunk:')) = ? || '|chunk:')
            ORDER BY bar_offset
            """,
            [
                market.strip().upper(),
                code.strip(),
                timeframe,
                price_basis,
                formula_id,
                formula_version,
                data_fingerprint,
                data_fingerprint,
                data_fingerprint,
            ],
        )
        return [_builtin_row(row) for row in rows]

    def read_indicator_caches(
        self,
        *,
        market: str,
        code: str,
        timeframe: str,
        price_basis: str,
        data_fingerprint: str,
        formula_versions: Mapping[str, str],
        min_end_date: date | None = None,
    ) -> dict[str, list[dict[str, object]]]:
        """Read several formula generations for one series in one query.

        缓存按自然年分块存放，一个公式对应多行。`min_end_date` 让调用方只取覆盖
        请求窗口的那几块——读最近 250 根不必把 5 年的 blob 都读出来。这里按日期
        而不是按根数过滤：缓存可能只覆盖最近几年，块的下标基准和「全历史」并不
        一致，日期才是两边都能对齐的坐标。
        """

        if not formula_versions:
            return {}
        predicates: list[str] = []
        parameters: list[object] = [
            market.strip().upper(),
            code.strip(),
            timeframe,
            price_basis,
            data_fingerprint,
            data_fingerprint,
            data_fingerprint,
        ]
        for formula_id, formula_version in formula_versions.items():
            predicates.append("(formula_id = ? AND formula_version = ?)")
            parameters.extend([formula_id, formula_version])
        window = ""
        if min_end_date is not None:
            window = " AND data_end >= ?"
            parameters.append(min_end_date.isoformat())
        query = (
            "SELECT market, code, timeframe, price_basis, formula_id, formula_version, "
            "data_fingerprint, bar_offset, data_start, data_end, data_length, values_blob, "
            "signals_blob, computed_at FROM indicator_cache "
            "WHERE market = ? AND code = ? AND timeframe = ? AND price_basis = ? "
            "AND (data_fingerprint = ? OR substr(data_fingerprint, 1, "
            "length(? || '|chunk:')) = ? || '|chunk:') AND ("
            + " OR ".join(predicates)
            + ")"
            + window
            + " "
            "ORDER BY formula_id, bar_offset"
        )
        rows = self._query(query, parameters)
        grouped: dict[str, list[dict[str, object]]] = {}
        for row in rows:
            entry = _builtin_row(row)
            grouped.setdefault(str(entry["formula_id"]), []).append(entry)
        return grouped

    def read_indicator_caches_batch(
        self,
        keys: Iterable[Mapping[str, str]],
    ) -> list[dict[str, object]]:
        """Read cache rows for many instruments with one query."""

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
        # 临时表跟着连接走，而连接是每线程一份，不会互相踩到。
        normalized: list[tuple[str, ...]] = []
        seen: set[tuple[str, ...]] = set()
        for entry in pending:
            key = tuple(str(entry[column]) for column in lookup_columns)
            if key in seen:
                continue
            seen.add(key)
            normalized.append(key)
        try:
            connection = self._connection()
            connection.execute(
                "CREATE TEMP TABLE IF NOT EXISTS _indicator_cache_keys ("
                "market TEXT, code TEXT, timeframe TEXT, price_basis TEXT, "
                "formula_id TEXT, formula_version TEXT, data_fingerprint TEXT)"
            )
            connection.execute("DELETE FROM _indicator_cache_keys")
            connection.executemany(
                "INSERT INTO _indicator_cache_keys VALUES (?, ?, ?, ?, ?, ?, ?)",
                normalized,
            )
            rows = connection.execute(
                """
                SELECT cache.market, cache.code, cache.timeframe, cache.price_basis,
                       cache.formula_id, cache.formula_version, cache.data_fingerprint,
                       cache.bar_offset, cache.data_start, cache.data_end,
                       cache.data_length, cache.values_blob, cache.signals_blob,
                       cache.computed_at
                FROM indicator_cache AS cache
                INNER JOIN _indicator_cache_keys AS keys
                  ON cache.market = keys.market
                 AND cache.code = keys.code
                 AND cache.timeframe = keys.timeframe
                 AND cache.price_basis = keys.price_basis
                 AND cache.formula_id = keys.formula_id
                 AND cache.formula_version = keys.formula_version
                         AND (
                             cache.data_fingerprint = keys.data_fingerprint
                             OR substr(
                                 cache.data_fingerprint,
                                 1,
                                 length(keys.data_fingerprint || '|chunk:')
                             ) = keys.data_fingerprint || '|chunk:'
                         )
                ORDER BY cache.market, cache.code, cache.formula_id, cache.bar_offset
                """
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if _is_missing_cache_error(exc):
                return []
            raise
        return [_builtin_row(row) for row in rows]

    def indicator_cache_chunk_index(
        self,
        keys: Iterable[Mapping[str, str]],
    ) -> list[dict[str, object]]:
        """Read cache chunk metadata without loading the indicator blobs."""

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
        ]
        normalized: list[tuple[str, ...]] = []
        seen: set[tuple[str, ...]] = set()
        for entry in pending:
            key = tuple(str(entry[column]) for column in lookup_columns)
            if key not in seen:
                seen.add(key)
                normalized.append(key)
        try:
            connection = self._connection()
            connection.execute(
                "CREATE TEMP TABLE IF NOT EXISTS _indicator_cache_index_keys ("
                "market TEXT, code TEXT, timeframe TEXT, price_basis TEXT, "
                "formula_id TEXT, formula_version TEXT)"
            )
            connection.execute("DELETE FROM _indicator_cache_index_keys")
            connection.executemany(
                "INSERT INTO _indicator_cache_index_keys VALUES (?, ?, ?, ?, ?, ?)",
                normalized,
            )
            rows = connection.execute(
                """
                SELECT cache.market, cache.code, cache.timeframe, cache.price_basis,
                       cache.formula_id, cache.formula_version, cache.data_fingerprint,
                       cache.bar_offset, cache.data_start, cache.data_end, cache.data_length
                FROM indicator_cache AS cache
                INNER JOIN _indicator_cache_index_keys AS keys
                  ON cache.market = keys.market
                 AND cache.code = keys.code
                 AND cache.timeframe = keys.timeframe
                 AND cache.price_basis = keys.price_basis
                 AND cache.formula_id = keys.formula_id
                 AND cache.formula_version = keys.formula_version
                ORDER BY cache.market, cache.code, cache.formula_id,
                         cache.bar_offset, cache.computed_at DESC
                """
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if _is_missing_cache_error(exc):
                return []
            raise
        names = (
            "market",
            "code",
            "timeframe",
            "price_basis",
            "formula_id",
            "formula_version",
            "data_fingerprint",
            "bar_offset",
            "data_start",
            "data_end",
            "data_length",
        )
        return [dict(zip(names, row, strict=True)) for row in rows]

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
        row = self._query_one(
            """
            SELECT market, code, timeframe, price_basis, formula_hash,
                   parameter_hash, engine_version, parameter_json,
                   value_names_json, signal_names_json, data_fingerprint,
                   data_start, data_end, data_length, minimum_bars,
                   recommended_bars, values_blob, signals_blob, computed_at
            FROM custom_indicator_cache
            WHERE market = ? AND code = ? AND timeframe = ? AND price_basis = ?
              AND formula_hash = ? AND parameter_hash = ? AND engine_version = ?
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
        )
        return None if row is None else _custom_row(row)

    # ----------------------------------------------------------------- writes

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
        bar_offset: int = 0,
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
                    "bar_offset": bar_offset,
                    "data_start": data_start,
                    "data_end": data_end,
                    "data_length": data_length,
                    "values_blob": values_blob,
                    "signals_blob": signals_blob,
                }
            ]
        )

    def write_indicator_caches(
        self,
        entries: Iterable[Mapping[str, object]],
        *,
        preserve_other_chunks: bool = False,
    ) -> None:
        """Atomically replace several formula cache generations in one transaction."""

        pending = list(entries)
        if not pending:
            return
        self._store._require_writer()
        # 同一个公式的多个分块共用一次清理：先按公式身份去重删除过期版本，
        # 再逐块写入，避免每一块都重复扫一遍表。
        stale: dict[tuple[str, str, str, str, str], tuple[str, str]] = {}
        stale_chunks: dict[tuple[str, str, str, str, str, object], tuple[str, str]] = {}
        legacy_revisions: dict[tuple[str, str, str, str, str], str] = {}
        for entry in pending:
            identity = (
                str(entry["market"]).strip().upper(),
                str(entry["code"]).strip(),
                str(entry["timeframe"]),
                str(entry["price_basis"]),
                str(entry["formula_id"]),
            )
            version_and_fingerprint = (
                str(entry["formula_version"]),
                str(entry["data_fingerprint"]),
            )
            if preserve_other_chunks:
                stale_chunks[(*identity, entry.get("data_start"))] = version_and_fingerprint
                revision, marker, _digest = str(entry["data_fingerprint"]).partition(
                    _CHUNK_FINGERPRINT_MARKER
                )
                if marker:
                    legacy_revisions[identity] = revision
            else:
                stale[identity] = version_and_fingerprint
        with _WRITE_LOCK:
            connection = self._connection()
            connection.execute("BEGIN IMMEDIATE")
            try:
                for (
                    market,
                    code,
                    timeframe,
                    price_basis,
                    formula_id,
                ), (formula_version, data_fingerprint) in stale.items():
                    # Only the current data generation is useful. Keeping stale
                    # generations would make this derived table grow forever.
                    connection.execute(
                        """
                        DELETE FROM indicator_cache
                        WHERE market = ? AND code = ? AND timeframe = ?
                          AND price_basis = ? AND formula_id = ?
                          AND (formula_version <> ? OR data_fingerprint <> ?)
                        """,
                        [
                            market,
                            code,
                            timeframe,
                            price_basis,
                            formula_id,
                            formula_version,
                            data_fingerprint,
                        ],
                    )
                if preserve_other_chunks:
                    for (
                        market,
                        code,
                        timeframe,
                        price_basis,
                        formula_id,
                    ), revision in legacy_revisions.items():
                        # Retire whole-series rows for this same revision. They
                        # overlap annual offsets and have no source chunk hash.
                        connection.execute(
                            """
                            DELETE FROM indicator_cache
                            WHERE market = ? AND code = ? AND timeframe = ?
                              AND price_basis = ? AND formula_id = ?
                              AND data_fingerprint = ?
                            """,
                            [market, code, timeframe, price_basis, formula_id, revision],
                        )
                    for (
                        market,
                        code,
                        timeframe,
                        price_basis,
                        formula_id,
                        data_start,
                    ), (formula_version, data_fingerprint) in stale_chunks.items():
                        connection.execute(
                            """
                            DELETE FROM indicator_cache
                            WHERE market = ? AND code = ? AND timeframe = ?
                              AND price_basis = ? AND formula_id = ?
                              AND strftime('%Y', data_start) = strftime('%Y', ?)
                              AND (formula_version <> ? OR data_fingerprint <> ?)
                            """,
                            [
                                market,
                                code,
                                timeframe,
                                price_basis,
                                formula_id,
                                _date_text(data_start),
                                formula_version,
                                data_fingerprint,
                            ],
                        )
                connection.executemany(
                    """
                    INSERT INTO indicator_cache (
                        market, code, timeframe, price_basis, formula_id,
                        formula_version, data_fingerprint, bar_offset, data_start,
                        data_end, data_length, values_blob, signals_blob, computed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (
                        market, code, timeframe, price_basis, formula_id,
                        formula_version, data_fingerprint, bar_offset
                    ) DO UPDATE SET
                        data_start = excluded.data_start,
                        data_end = excluded.data_end,
                        data_length = excluded.data_length,
                        values_blob = excluded.values_blob,
                        signals_blob = excluded.signals_blob,
                        computed_at = excluded.computed_at
                    """,
                    [_builtin_parameters(entry) for entry in pending],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def retag_indicator_cache_chunks(
        self,
        entries: Iterable[Mapping[str, object]],
    ) -> None:
        """Refresh chunk revision metadata without rewriting cached value blobs."""

        pending = list(entries)
        if not pending:
            return
        self._store._require_writer()
        with _WRITE_LOCK:
            connection = self._connection()
            connection.execute("BEGIN IMMEDIATE")
            try:
                for entry in pending:
                    identity = [
                        str(entry["market"]).strip().upper(),
                        str(entry["code"]).strip(),
                        str(entry["timeframe"]),
                        str(entry["price_basis"]),
                        str(entry["formula_id"]),
                        str(entry["formula_version"]),
                    ]
                    old_fingerprint = str(entry["old_data_fingerprint"])
                    old_offset = int(cast(int, entry["old_bar_offset"]))
                    new_fingerprint = str(entry["data_fingerprint"])
                    new_offset = int(cast(int, entry["bar_offset"]))
                    data_start = entry.get("data_start")
                    if old_fingerprint == new_fingerprint and old_offset == new_offset:
                        continue
                    target = connection.execute(
                        """
                        SELECT 1 FROM indicator_cache
                        WHERE market = ? AND code = ? AND timeframe = ?
                          AND price_basis = ? AND formula_id = ?
                          AND formula_version = ? AND data_fingerprint = ?
                          AND bar_offset = ?
                        LIMIT 1
                        """,
                        [*identity, new_fingerprint, new_offset],
                    ).fetchone()
                    old_key = [*identity, old_fingerprint, old_offset]
                    if target is not None:
                        connection.execute(
                            """
                            DELETE FROM indicator_cache
                            WHERE market = ? AND code = ? AND timeframe = ?
                              AND price_basis = ? AND formula_id = ?
                              AND formula_version = ? AND data_fingerprint = ?
                              AND bar_offset = ? AND data_start IS ?
                            """,
                            [*old_key, _date_text(data_start)],
                        )
                        continue
                    connection.execute(
                        """
                        UPDATE indicator_cache
                        SET data_fingerprint = ?, bar_offset = ?
                        WHERE market = ? AND code = ? AND timeframe = ?
                          AND price_basis = ? AND formula_id = ?
                          AND formula_version = ? AND data_fingerprint = ?
                          AND bar_offset = ? AND data_start IS ?
                        """,
                        [new_fingerprint, new_offset, *old_key, _date_text(data_start)],
                    )
                connection.commit()
            except Exception:
                connection.rollback()
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

        self._store._require_writer()
        normalized_market = market.strip().upper()
        normalized_code = code.strip()
        with _WRITE_LOCK:
            connection = self._connection()
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    DELETE FROM custom_indicator_cache
                    WHERE market = ? AND code = ? AND timeframe = ?
                      AND price_basis = ? AND formula_hash = ?
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
                        _date_text(data_start),
                        _date_text(data_end),
                        int(data_length),
                        int(minimum_bars),
                        int(recommended_bars),
                        values_blob,
                        signals_blob,
                        _now().isoformat(sep=" "),
                    ],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def clear_indicator_caches(self) -> dict[str, int]:
        """Drop every persisted derived result so later requests recompute it."""

        self._store._require_writer()
        removed = {"builtin_entries": 0, "custom_entries": 0}
        with _WRITE_LOCK:
            connection = self._connection()
            connection.execute("BEGIN IMMEDIATE")
            try:
                for table, key in (
                    ("indicator_cache", "builtin_entries"),
                    ("custom_indicator_cache", "custom_entries"),
                ):
                    row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                    removed[key] = int(row[0]) if row else 0
                    connection.execute(f"DELETE FROM {table}")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return removed

    # ----------------------------------------------------------------- status

    def stale_symbols(self, formula_versions: Mapping[str, str]) -> set[tuple[str, str]]:
        """Instruments whose cached indicators are missing or out of date.

        增量预计算按标的缩小范围：每个公式的当前版本都必须有一条从偏移 0 开始
        连续覆盖的缓存序列，并包含当前行情版本下的年度块。年度块指纹还会校验实际
        输入行情，因此同一标的的其他年度可以保留旧指纹并在预计算时直接复用。
        """

        wanted = {(str(fid), str(version)) for fid, version in formula_versions.items()}
        if not wanted:
            return set()
        revisions = self.data_revisions()
        if not revisions:
            return set()
        rows = self._query(
            "SELECT market, code, data_fingerprint, formula_id, formula_version, "
            "bar_offset, data_length "
            "FROM indicator_cache"
        )
        covered: dict[tuple[str, str], dict[tuple[str, str], set[tuple[int, int]]]] = {}
        for market, code, fingerprint, formula_id, formula_version, offset, length in rows:
            key = (str(market).strip().upper(), str(code).strip())
            revision = revisions.get(key)
            if revision is None or not _fingerprint_matches_revision(str(fingerprint), revision):
                continue
            pair = (str(formula_id), str(formula_version))
            if pair not in wanted:
                continue
            try:
                chunk = (int(cast(int, offset)), int(cast(int, length)))
            except (TypeError, ValueError):
                continue
            if chunk[0] >= 0 and chunk[1] > 0:
                covered.setdefault(key, {}).setdefault(pair, set()).add(chunk)
        stale: set[tuple[str, str]] = set()
        for key, revision in revisions.items():
            complete = True
            for formula_id, formula_version in wanted:
                chunks = covered.get(key, {}).get((formula_id, formula_version))
                if not chunks:
                    complete = False
                    break
                ordered = sorted(chunks)
                if ordered[0][0] != 0:
                    complete = False
                    break
                cursor = 0
                for offset, length in ordered:
                    if offset != cursor:
                        complete = False
                        break
                    cursor += length
                if not complete:
                    break
            if not complete:
                stale.add(key)
        return stale

    def indicator_cache_status(self) -> dict[str, int | str | None]:
        """Return persistent cache size and freshness metadata."""

        built_in = _status_row(
            self._query_one(
                """
                SELECT COUNT(*),
                       COALESCE(SUM(length(values_blob) + length(signals_blob)), 0),
                       MIN(computed_at), MAX(computed_at)
                FROM indicator_cache
                """
            )
        )
        custom = _status_row(
            self._query_one(
                """
                SELECT COUNT(*),
                       COALESCE(SUM(length(values_blob) + length(signals_blob)), 0),
                       MIN(computed_at), MAX(computed_at)
                FROM custom_indicator_cache
                """
            )
        )
        total_entries = built_in[0] + custom[0]
        return {
            "builtin_entries": built_in[0],
            "custom_entries": custom[0],
            "total_entries": total_entries,
            "bytes": built_in[1] + custom[1],
            "oldest_computed_at": _timestamp_text(_earliest(built_in[2], custom[2])),
            "latest_computed_at": _timestamp_text(_latest(built_in[3], custom[3])),
        }

    def prune_stale_generations(self) -> dict[str, int]:
        """删掉每个（标的, 周期, 口径, 公式）里不是最新一代的缓存行。

        写入路径只清理「这次重算过的」标的；数据变了却再没被重算的标的会一直留着旧指纹的
        行。旧指纹永远查不到（读取按当前指纹精确匹配），删掉最多换来一次重算，所以这里按
        每个分组的最新 ``computed_at`` 判定——同一代的分块共享同一个时间戳。
        """

        pruned = {"indicator_cache": 0, "custom_indicator_cache": 0}
        with _WRITE_LOCK:
            connection = self._connection()
            connection.execute("BEGIN IMMEDIATE")
            try:
                for table, partition in (
                    (
                        "indicator_cache",
                        "market, code, timeframe, price_basis, formula_id",
                    ),
                    (
                        "custom_indicator_cache",
                        "market, code, timeframe, price_basis, formula_hash, "
                        "parameter_hash, engine_version",
                    ),
                ):
                    cursor = connection.execute(
                        f"""
                        DELETE FROM {table} WHERE rowid IN (
                            SELECT rowid FROM (
                                SELECT rowid,
                                       MAX(computed_at) OVER (PARTITION BY {partition})
                                           AS newest
                                FROM {table}
                            ) WHERE computed_at < newest
                        )
                        """
                    )
                    pruned[table] = max(0, int(cursor.rowcount or 0))
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return pruned

    def maintain(self, *, vacuum_min_free_ratio: float = 0.2) -> dict[str, object]:
        """清理过期代并（必要时）回收文件空间；返回一份可写进日志的统计。

        VACUUM 要独占整个文件，只在没有其他连接时（启动维护阶段）调用才安全；这里失败
        不算错误——清理已经生效，空间下次再收。
        """

        if self._store.read_only:
            return {"skipped": "read_only"}
        pruned = self.prune_stale_generations()
        connection = self._connection()
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        free_pages = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
        size_before = self._path.stat().st_size if self._path.is_file() else 0
        free_ratio = free_pages / page_count if page_count else 0.0
        stats: dict[str, object] = {
            "pruned_indicator_cache": pruned["indicator_cache"],
            "pruned_custom_indicator_cache": pruned["custom_indicator_cache"],
            "free_ratio_before": round(free_ratio, 3),
            "size_before": size_before,
            "vacuumed": False,
        }
        if free_ratio < vacuum_min_free_ratio:
            stats["size_after"] = size_before
            return stats
        try:
            connection.execute("VACUUM")
        except sqlite3.Error as exc:
            stats["vacuum_error"] = str(exc)[:200]
            stats["size_after"] = self._path.stat().st_size if self._path.is_file() else 0
            return stats
        stats["vacuumed"] = True
        stats["size_after"] = self._path.stat().st_size if self._path.is_file() else 0
        stats["page_size"] = page_size
        return stats


def default_indicator_cache_path(database_path: str | Path) -> Path:
    """Sibling SQLite file for the market database's derived caches."""

    base = Path(database_path)
    return base.with_name(f"{base.stem}-indicator-cache.sqlite")


def _builtin_parameters(entry: Mapping[str, object]) -> tuple[object, ...]:
    return (
        str(entry["market"]).strip().upper(),
        str(entry["code"]).strip(),
        str(entry["timeframe"]),
        str(entry["price_basis"]),
        str(entry["formula_id"]),
        str(entry["formula_version"]),
        str(entry["data_fingerprint"]),
        int(cast(int, entry["bar_offset"])),
        _date_text(entry["data_start"]),
        _date_text(entry["data_end"]),
        int(cast(int, entry["data_length"])),
        entry["values_blob"],
        entry["signals_blob"],
        _now().isoformat(sep=" "),
    )


def _builtin_row(row: tuple[object, ...]) -> dict[str, object]:
    values = dict(zip(_BUILTIN_COLUMNS, row, strict=True))
    values["data_start"] = _as_date(values["data_start"])
    values["data_end"] = _as_date(values["data_end"])
    values["computed_at"] = _as_datetime(values["computed_at"])
    return values


def _custom_row(row: tuple[object, ...]) -> dict[str, object]:
    values = dict(zip(_CUSTOM_COLUMNS, row, strict=True))
    values["data_start"] = _as_date(values["data_start"])
    values["data_end"] = _as_date(values["data_end"])
    values["computed_at"] = _as_datetime(values["computed_at"])
    return values


def _status_row(row: tuple[object, ...] | None) -> tuple[int, int, object, object]:
    """Normalize one ``COUNT/SUM/MIN/MAX`` row into typed values."""

    if row is None:
        return (0, 0, None, None)
    return (int(cast(int, row[0])), int(cast(int, row[1])), row[2], row[3])


def _earliest(*values: object) -> datetime | None:
    parsed = [value for value in (_as_datetime(item) for item in values) if value is not None]
    return min(parsed) if parsed else None


def _latest(*values: object) -> datetime | None:
    parsed = [value for value in (_as_datetime(item) for item in values) if value is not None]
    return max(parsed) if parsed else None
