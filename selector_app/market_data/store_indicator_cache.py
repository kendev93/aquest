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
    ) -> dict[str, object] | None:
        """Read one immutable, versioned indicator result from DuckDB."""

        try:
            with self._store._connect(read_only=True) as connection:
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
            with self._store._connect(read_only=True) as connection:
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

        self._store._require_writer()
        with _WRITE_LOCK, self._store._connect() as connection:
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
