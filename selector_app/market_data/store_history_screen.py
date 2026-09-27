"""Screen history persistence methods."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone

from .store_base import (
    _WRITE_LOCK,
    SCREEN_HISTORY_LIMIT,
    MarketDataStoreError,
    _DuckDbStoreBase,
    _now,
)


class ScreenHistoryMixin:
    _store: _DuckDbStoreBase

    def save_screen_history(
        self,
        history_id: str,
        *,
        results: Iterable[Mapping[str, object]],
        meta: Mapping[str, object],
        created_at: datetime | None = None,
    ) -> None:
        """Save one completed formula-screen result and trim older entries."""

        self._store._require_writer()
        normalized_id = history_id.strip()
        if not normalized_id:
            raise ValueError("筛选历史 ID 不能为空")
        result_rows = [dict(result) for result in results]
        meta_dict = dict(meta)
        required_counts: dict[str, int] = {}
        for key in ("total_candidates", "total_scanned", "total_signals", "errors", "skipped"):
            raw_value = meta_dict.get(key, 0)
            required_counts[key] = int(raw_value) if isinstance(raw_value, (int, float, str)) else 0
        created = created_at or _now()
        if created.tzinfo is not None:
            created = created.astimezone(timezone.utc).replace(tzinfo=None)
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
            try:
                connection.execute(
                    """
                    INSERT INTO screen_history (
                        history_id, created_at, total_candidates, total_scanned,
                        total_signals, errors, skipped, result_count, meta_json, results_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (history_id) DO UPDATE SET
                        created_at = excluded.created_at,
                        total_candidates = excluded.total_candidates,
                        total_scanned = excluded.total_scanned,
                        total_signals = excluded.total_signals,
                        errors = excluded.errors,
                        skipped = excluded.skipped,
                        result_count = excluded.result_count,
                        meta_json = excluded.meta_json,
                        results_json = excluded.results_json
                    """,
                    [
                        normalized_id,
                        created,
                        required_counts["total_candidates"],
                        required_counts["total_scanned"],
                        required_counts["total_signals"],
                        required_counts["errors"],
                        required_counts["skipped"],
                        len(result_rows),
                        json.dumps(meta_dict, ensure_ascii=False, separators=(",", ":")),
                        json.dumps(result_rows, ensure_ascii=False, separators=(",", ":")),
                    ],
                )
                stale = connection.execute(
                    """
                    SELECT history_id
                    FROM screen_history
                    ORDER BY created_at DESC, history_id DESC
                    LIMIT 100000 OFFSET ?
                    """,
                    [SCREEN_HISTORY_LIMIT],
                ).fetchall()
                if stale:
                    connection.executemany(
                        "DELETE FROM screen_history WHERE history_id = ?",
                        stale,
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def list_screen_history(self) -> list[dict[str, object]]:
        """Return the most recent formula-screen history summaries."""

        with self._store._connect(read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT history_id, created_at, total_candidates, total_scanned,
                       total_signals, errors, skipped, result_count
                FROM screen_history
                ORDER BY created_at DESC, history_id DESC
                LIMIT ?
                """,
                [SCREEN_HISTORY_LIMIT],
            ).fetchall()
        return [
            {
                "history_id": str(row[0]),
                "created_at": str(row[1]),
                "total_candidates": int(row[2]),
                "total_scanned": int(row[3]),
                "total_signals": int(row[4]),
                "errors": int(row[5]),
                "skipped": int(row[6]),
                "result_count": int(row[7]),
            }
            for row in rows
        ]

    def read_screen_history(self, history_id: str) -> dict[str, object] | None:
        """Read one formula-screen history record with its complete results."""

        with self._store._connect(read_only=True) as connection:
            row = connection.execute(
                """
                SELECT history_id, created_at, meta_json, results_json
                FROM screen_history
                WHERE history_id = ?
                """,
                [history_id.strip()],
            ).fetchone()
        if row is None:
            return None
        try:
            meta = json.loads(str(row[2]))
            results = json.loads(str(row[3]))
        except json.JSONDecodeError as exc:
            raise MarketDataStoreError("筛选历史内容损坏") from exc
        if not isinstance(meta, dict) or not isinstance(results, list):
            raise MarketDataStoreError("筛选历史内容无效")
        return {
            "history_id": str(row[0]),
            "created_at": str(row[1]),
            "meta": meta,
            "results": results,
        }
