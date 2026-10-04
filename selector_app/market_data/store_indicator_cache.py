"""Repository operations for persisted built-in and custom indicator caches."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from typing import cast

import duckdb
import pandas as pd

from .store_base import _WRITE_LOCK, _DuckDbStoreBase, _now, _timestamp_text

_CHUNK_FINGERPRINT_MARKER = "|chunk:"


def _fingerprint_matches_revision(fingerprint: str, revision: str) -> bool:
    return fingerprint == revision or fingerprint.startswith(
        f"{revision}{_CHUNK_FINGERPRINT_MARKER}"
    )


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
                digests = _corporate_action_digests(connection)
        except duckdb.CatalogException:
            return {}
        revisions: dict[tuple[str, str], str] = {}
        for market, code, bar_count, last_date, updated_at in rows:
            key = (str(market).strip().upper(), str(code).strip())
            if wanted is not None and key not in wanted:
                continue
            revisions[key] = _revision_text(bar_count, last_date, updated_at, digests.get(key, ""))
        return revisions

    def data_revision(self, market: str, code: str) -> str | None:
        """Return the data revision of a single instrument."""

        try:
            with self._store._connect(read_only=True) as connection:
                row = connection.execute(
                    """
                    SELECT i.bar_count, i.last_date, i.updated_at,
                           COALESCE((
                               SELECT md5(string_agg(
                                   CAST(a.ex_date AS VARCHAR) || '|' ||
                                   CAST(a.share_ratio AS VARCHAR) || '|' ||
                                   CAST(a.cash_per_share AS VARCHAR) || '|' ||
                                   CAST(a.factor_ratio AS VARCHAR),
                                   '\n' ORDER BY a.ex_date
                               ))
                               FROM corporate_actions AS a
                               WHERE a.market = i.market AND a.code = i.code
                           ), '') AS action_digest
                    FROM instruments AS i
                    WHERE i.market = ? AND i.code = ?
                    """,
                    [str(market).strip().upper(), str(code).strip()],
                ).fetchone()
        except duckdb.CatalogException:
            # 旧库里还没有 corporate_actions 表；返回 None 让调用方退化为帧指纹。
            return None
        if row is None:
            return None
        return _revision_text(row[0], row[1], row[2], row[3])

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
                      AND (
                          data_fingerprint = ?
                          OR substr(data_fingerprint, 1, length(? || '|chunk:'))
                             = ? || '|chunk:'
                      )
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
            data_fingerprint,
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
            "AND (data_fingerprint = ? OR substr(data_fingerprint, 1, "
            "length(? || '|chunk:')) = ? || '|chunk:') AND ("
            + " OR ".join(predicates)
            + ")"
            + window
            + " "
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

    def indicator_cache_chunk_index(
        self,
        keys: Iterable[Mapping[str, str]],
    ) -> list[dict[str, object]]:
        """Read cache chunk metadata without loading the indicator blobs."""

        pending = list(keys)
        if not pending:
            return []
        lookup_frame = pd.DataFrame(
            pending,
            columns=["market", "code", "timeframe", "price_basis", "formula_id", "formula_version"],
        ).drop_duplicates()
        try:
            with self._store._connect(read_only=True) as connection:
                connection.register("_indicator_cache_index_keys", lookup_frame)
                try:
                    rows = connection.execute(
                        """
                        SELECT cache.market, cache.code, cache.timeframe,
                               cache.price_basis, cache.formula_id,
                               cache.formula_version, cache.data_fingerprint,
                               cache.bar_offset, cache.data_start, cache.data_end,
                               cache.data_length
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
                finally:
                    connection.unregister("_indicator_cache_index_keys")
        except duckdb.CatalogException:
            return []
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
                if preserve_other_chunks:
                    for (
                        market,
                        code,
                        timeframe,
                        price_basis,
                        formula_id,
                    ), revision in legacy_revisions.items():
                        # A previous whole-series cache with this same current
                        # revision overlaps the annual offsets but has no chunk
                        # source hash. Retire it before publishing annual rows.
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
                              AND data_start >= date_trunc('year', CAST(? AS DATE))
                              AND data_start < date_trunc('year', CAST(? AS DATE))
                                                + INTERVAL '1 year'
                              AND (formula_version <> ? OR data_fingerprint <> ?)
                            """,
                            [
                                market,
                                code,
                                timeframe,
                                price_basis,
                                formula_id,
                                data_start,
                                data_start,
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

    def retag_indicator_cache_chunks(
        self,
        entries: Iterable[Mapping[str, object]],
    ) -> None:
        """Refresh chunk revision metadata without rewriting cached value blobs."""

        pending = list(entries)
        if not pending:
            return
        self._store._require_writer()
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
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
                              AND bar_offset = ? AND data_start IS NOT DISTINCT FROM ?
                            """,
                            [*old_key, data_start],
                        )
                        continue
                    connection.execute(
                        """
                        UPDATE indicator_cache
                        SET data_fingerprint = ?, bar_offset = ?
                        WHERE market = ? AND code = ? AND timeframe = ?
                          AND price_basis = ? AND formula_id = ?
                          AND formula_version = ? AND data_fingerprint = ?
                          AND bar_offset = ? AND data_start IS NOT DISTINCT FROM ?
                        """,
                        [new_fingerprint, new_offset, *old_key, data_start],
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
        try:
            with self._store._connect(read_only=True) as connection:
                rows = connection.execute(
                    "SELECT market, code, data_fingerprint, formula_id, formula_version, "
                    "bar_offset, data_length "
                    "FROM indicator_cache"
                ).fetchall()
        except duckdb.CatalogException:
            return set(revisions)
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


def _revision_text(
    bar_count: object,
    last_date: object,
    updated_at: object,
    action_digest: object = "",
) -> str:
    """Render one instrument's data revision as a stable comparable string.

    ``action_digest`` 是该标的分红送转事件表的内容哈希。行情（raw）本身在事件
    补录或修正时一行都不会变，所以只靠 bar_count / last_date / updated_at 判断，
    指标缓存会被判定为仍然有效 —— 而实际上按除权校正后的价格算出来的指标已经
    完全不同了。把事件版本并进来，事件一变，缓存自动失效。
    """

    try:
        count = int(cast(int, bar_count))
    except (TypeError, ValueError):
        count = 0
    return f"{count}|{last_date}|{updated_at}|{action_digest or ''}"


def _corporate_action_digests(connection: object) -> dict[tuple[str, str], str]:
    """每只标的的事件表内容哈希；表不存在时返回空（旧库不该在这里崩）。"""

    try:
        rows = connection.execute(  # type: ignore[attr-defined]
            """
            SELECT market, code, md5(string_agg(
                CAST(ex_date AS VARCHAR) || '|' ||
                CAST(share_ratio AS VARCHAR) || '|' ||
                CAST(cash_per_share AS VARCHAR) || '|' ||
                CAST(factor_ratio AS VARCHAR),
                '\n' ORDER BY ex_date
            )) AS action_digest
            FROM corporate_actions
            GROUP BY market, code
            """
        ).fetchall()
    except duckdb.CatalogException:
        return {}
    return {
        (str(market).strip().upper(), str(code).strip()): str(digest)
        for market, code, digest in rows
        if digest
    }
