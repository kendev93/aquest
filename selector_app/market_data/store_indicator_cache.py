"""Repository operations for persisted built-in and custom indicator caches."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from typing import cast

import duckdb
import pandas as pd

from .store_base import _WRITE_LOCK, _DuckDbStoreBase, _now, _timestamp_text


class DuckDbIndicatorCacheStore:
    """Read and write versioned derived indicator generations."""

    def __init__(self, store: _DuckDbStoreBase) -> None:
        self._store = store

    def data_revisions(
        self,
        refs: Iterable[tuple[str, str]] | None = None,
    ) -> dict[tuple[str, str], str]:
        """Per-instrument data revision used to invalidate cached indicators.

        A revision changes whenever the stored bar series for one instrument
        changes, which is exactly the granularity an indicator cache needs.  It
        deliberately ignores the requested window so a full-history cache entry
        can serve any shorter request.
        """

        wanted = (
            {(str(market).strip().upper(), str(code).strip()) for market, code in refs}
            if refs is not None
            else None
        )
        if wanted is not None and not wanted:
            return {}
        try:
            with self._store._connect(read_only=True) as connection:
                rows = connection.execute(
                    """
                    SELECT market, code, bar_count, last_date, updated_at
                    FROM instruments
                    """
                ).fetchall()
        except duckdb.CatalogException:
            return {}
        revisions: dict[tuple[str, str], str] = {}
        for market, code, bar_count, last_date, updated_at in rows:
            key = (str(market).strip().upper(), str(code).strip())
            if wanted is not None and key not in wanted:
                continue
            revisions[key] = _revision_text(bar_count, last_date, updated_at)
        return revisions

    def data_revision(self, market: str, code: str) -> str | None:
        """Return the data revision of a single instrument."""

        try:
            with self._store._connect(read_only=True) as connection:
                row = connection.execute(
                    """
                    SELECT bar_count, last_date, updated_at
                    FROM instruments
                    WHERE market = ? AND code = ?
                    """,
                    [str(market).strip().upper(), str(code).strip()],
                ).fetchone()
        except duckdb.CatalogException:
            return None
        if row is None:
            return None
        return _revision_text(row[0], row[1], row[2])

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
        """Read every stored chunk of one immutable, versioned indicator result."""

        try:
            with self._store._connect(read_only=True) as connection:
                rows = connection.execute(
                    """
                    SELECT market, code, timeframe, price_basis, formula_id,
                           formula_version, data_fingerprint, bar_offset, data_start,
                           data_end, data_length, values_blob, signals_blob, computed_at
                    FROM indicator_cache
                    WHERE market = ?
                      AND code = ?
                      AND timeframe = ?
                      AND price_basis = ?
                      AND formula_id = ?
                      AND formula_version = ?
                      AND data_fingerprint = ?
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
                ).fetchall()
        except duckdb.CatalogException:
            # A read-only connection opened against a pre-cache database can
            # still serve source bars; treat the absent derived table as miss.
            return []
        if not rows:
            return []
        keys = [
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
        ]
        return [dict(zip(keys, row, strict=True)) for row in rows]

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
        """Read several formula generations for one series in one connection.

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
        window = " AND data_end >= ?" if min_end_date is not None else ""
        if window:
            parameters.append(min_end_date)
        query = (
            "SELECT market, code, timeframe, price_basis, formula_id, formula_version, "
            "data_fingerprint, bar_offset, data_start, data_end, data_length, values_blob, "
            "signals_blob, computed_at FROM indicator_cache "
            "WHERE market = ? AND code = ? AND timeframe = ? AND price_basis = ? "
            "AND data_fingerprint = ? AND (" + " OR ".join(predicates) + ")" + window + " "
            "ORDER BY formula_id, bar_offset"
        )
        try:
            with self._store._connect(read_only=True) as connection:
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
            "bar_offset",
            "data_start",
            "data_end",
            "data_length",
            "values_blob",
            "signals_blob",
            "computed_at",
        ]
        grouped: dict[str, list[dict[str, object]]] = {}
        for row in rows:
            grouped.setdefault(str(row[4]), []).append(dict(zip(keys, row, strict=True)))
        return grouped

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
            with self._store._connect(read_only=True) as connection:
                connection.register("_indicator_cache_keys", lookup_frame)
                try:
                    rows = connection.execute(
                        """
                        SELECT cache.market, cache.code, cache.timeframe,
                               cache.price_basis, cache.formula_id,
                               cache.formula_version, cache.data_fingerprint,
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
            "bar_offset",
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
            with self._store._connect(read_only=True) as connection:
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
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
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
                        WHERE market = ?
                          AND code = ?
                          AND timeframe = ?
                          AND price_basis = ?
                          AND formula_id = ?
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
                for entry in pending:
                    connection.execute(
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
                        [
                            str(entry["market"]).strip().upper(),
                            str(entry["code"]).strip(),
                            str(entry["timeframe"]),
                            str(entry["price_basis"]),
                            str(entry["formula_id"]),
                            str(entry["formula_version"]),
                            str(entry["data_fingerprint"]),
                            int(cast(int, entry["bar_offset"])),
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

        self._store._require_writer()
        normalized_market = market.strip().upper()
        normalized_code = code.strip()
        with _WRITE_LOCK, self._store._connect() as connection:
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

    def clear_indicator_caches(self) -> dict[str, int]:
        """Drop every persisted derived result so later requests recompute it.

        Required whenever the cache key semantics change: rows written under the
        old scheme can never match again, so they only occupy store space.
        """

        self._store._require_writer()
        removed = {"builtin_entries": 0, "custom_entries": 0}
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
            try:
                for table, key in (
                    ("indicator_cache", "builtin_entries"),
                    ("custom_indicator_cache", "custom_entries"),
                ):
                    try:
                        row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                    except duckdb.CatalogException:
                        continue
                    removed[key] = int(row[0]) if row else 0
                    connection.execute(f"DELETE FROM {table}")
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return removed

    def stale_symbols(self, formula_versions: Mapping[str, str]) -> set[tuple[str, str]]:
        """Instruments whose cached indicators are missing or out of date.

        增量预计算靠它缩小范围：行情更新往往只动一小部分标的，全市场重算会把
        没变化的标的也重写一遍。判据是「该标的在当前数据版本号下，每个公式的
        当前口径版本是否都有缓存」。

        必须同时比 `data_fingerprint` 和 `formula_version`——读取路径
        （`read_indicator_cache`）就是两者都比。只比前者的话，bump
        `cache_version` 而行情没变的那些标的会被判成「已缓存」，增量预计算
        整批跳过，预热白跑（结果本身不受影响，首次请求仍会按需重算）。
        """

        wanted = {(str(fid), str(version)) for fid, version in formula_versions.items()}
        if not wanted:
            return set()
        revisions = self.data_revisions()
        if not revisions:
            return set()
        try:
            with self._store._connect(read_only=True) as connection:
                rows = connection.execute(
                    "SELECT market, code, data_fingerprint, formula_id, formula_version "
                    "FROM indicator_cache"
                ).fetchall()
        except duckdb.CatalogException:
            return set(revisions)
        covered: dict[tuple[str, str], dict[str, set[tuple[str, str]]]] = {}
        for market, code, fingerprint, formula_id, formula_version in rows:
            key = (str(market).strip().upper(), str(code).strip())
            covered.setdefault(key, {}).setdefault(str(fingerprint), set()).add(
                (str(formula_id), str(formula_version))
            )
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

        try:
            with self._store._connect(read_only=True) as connection:
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


def _revision_text(bar_count: object, last_date: object, updated_at: object) -> str:
    """Render one instrument's data revision as a stable comparable string."""

    try:
        count = int(cast(int, bar_count))
    except (TypeError, ValueError):
        count = 0
    return f"{count}|{last_date}|{updated_at}"
