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
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import cast

from .store_base import _WRITE_LOCK, _DuckDbStoreBase, _now, _timestamp_text
from .store_indicator_cache import DuckDbIndicatorCacheStore

logger = logging.getLogger(__name__)

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
            raise sqlite3.OperationalError("indicator cache file is absent")
        connection = cast(sqlite3.Connection | None, getattr(self._local, "connection", None))
        if connection is None:
            self._ensure_schema()
            # isolation_level=None 关掉 sqlite3 的隐式 BEGIN：写路径自己发
            # BEGIN IMMEDIATE，否则两条 BEGIN 会撞成
            # "cannot start a transaction within a transaction"。
            connection = sqlite3.connect(str(self._path), timeout=30.0, isolation_level=None)
            connection.execute("PRAGMA busy_timeout=30000")
            try:
                # WAL lets the read paths run while a precompute batch is
                # writing.  It needs shared memory on the volume, which not
                # every mount provides — falling back to the rollback journal
                # costs concurrency, not correctness.
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.OperationalError:  # pragma: no cover - depends on mount
                logger.debug("SQLite WAL 不可用，退回默认日志模式", exc_info=True)
            self._local.connection = connection
        return connection

    def _query(
        self,
        sql: str,
        parameters: Sequence[object] = (),
    ) -> list[tuple[object, ...]]:
        """Run a read query, treating a missing table as "nothing cached".

        Mirrors the DuckDB backend, which swallows ``CatalogException``: a store
        opened before the derived tables existed must still serve source bars.
        """

        try:
            return self._connection().execute(sql, parameters).fetchall()
        except sqlite3.OperationalError:
            return []

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
              AND formula_id = ? AND formula_version = ? AND data_fingerprint = ?
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
            "AND data_fingerprint = ? AND (" + " OR ".join(predicates) + ")" + window + " "
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
        connection = self._connection()
        try:
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
                 AND cache.data_fingerprint = keys.data_fingerprint
                ORDER BY cache.market, cache.code, cache.formula_id, cache.bar_offset
                """
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [_builtin_row(row) for row in rows]

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

    def write_indicator_caches(self, entries: Iterable[Mapping[str, object]]) -> None:
        """Atomically replace several formula cache generations in one transaction."""

        pending = list(entries)
        if not pending:
            return
        self._store._require_writer()
        # 同一个公式的多个分块共用一次清理：先按公式身份去重删除过期版本，
        # 再逐块写入，避免每一块都重复扫一遍表。
        stale: dict[tuple[str, str, str, str, str], tuple[str, str]] = {}
        for entry in pending:
            stale[
                (
                    str(entry["market"]).strip().upper(),
                    str(entry["code"]).strip(),
                    str(entry["timeframe"]),
                    str(entry["price_basis"]),
                    str(entry["formula_id"]),
                )
            ] = (str(entry["formula_version"]), str(entry["data_fingerprint"]))
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

    def stale_symbols(self, formula_ids: Iterable[str]) -> set[tuple[str, str]]:
        """Instruments whose cached indicators are missing or out of date.

        增量预计算靠它缩小范围：行情更新往往只动一小部分标的，全市场重算会把
        没变化的标的也重写一遍。判据是「该标的在当前数据版本号下是否每个公式
        都有缓存」，版本号变化后旧行自然不再匹配。
        """

        wanted = {str(formula_id) for formula_id in formula_ids}
        if not wanted:
            return set()
        revisions = self.data_revisions()
        if not revisions:
            return set()
        rows = self._query("SELECT market, code, data_fingerprint, formula_id FROM indicator_cache")
        covered: dict[tuple[str, str], dict[str, set[str]]] = {}
        for market, code, fingerprint, formula_id in rows:
            key = (str(market).strip().upper(), str(code).strip())
            covered.setdefault(key, {}).setdefault(str(fingerprint), set()).add(str(formula_id))
        stale: set[tuple[str, str]] = set()
        for key, revision in revisions.items():
            by_fingerprint = covered.get(key)
            if not by_fingerprint:
                stale.add(key)
                continue
            if wanted - by_fingerprint.get(revision, set()):
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
