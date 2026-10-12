"""Persistence methods for the strategies domain."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime

from .store_base import _WRITE_LOCK, _DuckDbStoreBase, _now
from .store_history_common import (
    _STRATEGY_COLUMNS,
    SAVED_STRATEGY_TABLE,
    STRATEGY_NAME_MAX_LENGTH,
    STRATEGY_NOTE_MAX_LENGTH,
    STRATEGY_TRACKING_PLAN_TABLE,
    STRATEGY_TRACKING_TABLE,
    STRATEGY_TRACKING_TRADES_TABLE,
    _decode_strategy_row,
    _normalize_timestamp,
    _strategy_record,
)


class StrategyHistoryMixin:
    _store: _DuckDbStoreBase

    def strategy_name_taken(self, name: str, *, exclude_strategy_id: str = "") -> bool:
        """Whether a strategy name is already used by another record."""

        normalized = name.strip()
        if not normalized:
            return False
        with self._store._connect(read_only=True) as connection:
            row = connection.execute(
                f"SELECT 1 FROM {SAVED_STRATEGY_TABLE} WHERE name = ? AND strategy_id <> ? LIMIT 1",
                [normalized, exclude_strategy_id.strip()],
            ).fetchone()
        return row is not None

    def save_strategy(
        self,
        strategy_id: str,
        *,
        name: str,
        request: Mapping[str, object],
        summary: Mapping[str, object],
        note: str = "",
        source_history_id: str | None = None,
        created_at: datetime | str | None = None,
    ) -> dict[str, object]:
        """Persist one reusable strategy configuration."""

        self._store._require_writer()
        normalized_id = strategy_id.strip()
        if not normalized_id:
            raise ValueError("策略 ID 不能为空")
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("策略名称不能为空")
        if len(normalized_name) > STRATEGY_NAME_MAX_LENGTH:
            raise ValueError(f"策略名称不能超过 {STRATEGY_NAME_MAX_LENGTH} 个字符")
        normalized_note = note.strip()
        if len(normalized_note) > STRATEGY_NOTE_MAX_LENGTH:
            raise ValueError(f"策略备注不能超过 {STRATEGY_NOTE_MAX_LENGTH} 个字符")
        if self.strategy_name_taken(normalized_name, exclude_strategy_id=normalized_id):
            raise ValueError(f"已存在同名策略: {normalized_name}")
        created = _normalize_timestamp(created_at, _now())
        payload = {
            "strategy_id": normalized_id,
            "name": normalized_name,
            "note": normalized_note,
            "source_history_id": (source_history_id or "").strip() or None,
            "created_at": created,
            "updated_at": created,
            "request": dict(request),
            "summary": dict(summary),
        }
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
            try:
                connection.execute(
                    f"""
                    INSERT INTO {SAVED_STRATEGY_TABLE} (
                        strategy_id, name, note, source_history_id,
                        created_at, updated_at, request_json, summary_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (strategy_id) DO UPDATE SET
                        name = excluded.name,
                        note = excluded.note,
                        source_history_id = excluded.source_history_id,
                        updated_at = excluded.updated_at,
                        request_json = excluded.request_json,
                        summary_json = excluded.summary_json
                    """,
                    [
                        payload["strategy_id"],
                        payload["name"],
                        payload["note"],
                        payload["source_history_id"],
                        created,
                        created,
                        json.dumps(payload["request"], ensure_ascii=False, separators=(",", ":")),
                        json.dumps(payload["summary"], ensure_ascii=False, separators=(",", ":")),
                    ],
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return _strategy_record(payload)

    def list_strategies(self) -> list[dict[str, object]]:
        """Return every saved strategy, most recently updated first."""

        with self._store._connect(read_only=True) as connection:
            rows = connection.execute(
                f"""
                SELECT {_STRATEGY_COLUMNS}
                FROM {SAVED_STRATEGY_TABLE}
                ORDER BY updated_at DESC, strategy_id DESC
                """
            ).fetchall()
        records: list[dict[str, object]] = []
        for row in rows:
            payload = _decode_strategy_row(row)
            payload.pop("request", None)
            payload.pop("tracking_state", None)
            records.append(payload)
        return records

    def read_strategy(self, strategy_id: str) -> dict[str, object] | None:
        """Read one saved strategy including its replayable request."""

        with self._store._connect(read_only=True) as connection:
            row = connection.execute(
                f"""
                SELECT {_STRATEGY_COLUMNS}
                FROM {SAVED_STRATEGY_TABLE}
                WHERE strategy_id = ?
                """,
                [strategy_id.strip()],
            ).fetchone()
        if row is None:
            return None
        return _decode_strategy_row(row)

    def update_strategy(
        self,
        strategy_id: str,
        *,
        name: str | None = None,
        note: str | None = None,
    ) -> dict[str, object] | None:
        """Rename or re-note a saved strategy, leaving its request untouched."""

        self._store._require_writer()
        normalized_id = strategy_id.strip()
        if not normalized_id:
            return None
        if name is None and note is None:
            return self.read_strategy(normalized_id)
        normalized_name = name.strip() if isinstance(name, str) else None
        if normalized_name is not None:
            if not normalized_name:
                raise ValueError("策略名称不能为空")
            if len(normalized_name) > STRATEGY_NAME_MAX_LENGTH:
                raise ValueError(f"策略名称不能超过 {STRATEGY_NAME_MAX_LENGTH} 个字符")
            if self.strategy_name_taken(normalized_name, exclude_strategy_id=normalized_id):
                raise ValueError(f"已存在同名策略: {normalized_name}")
        normalized_note = note.strip() if isinstance(note, str) else None
        if normalized_note is not None and len(normalized_note) > STRATEGY_NOTE_MAX_LENGTH:
            raise ValueError(f"策略备注不能超过 {STRATEGY_NOTE_MAX_LENGTH} 个字符")
        assignments: list[str] = []
        parameters: list[object] = []
        if normalized_name is not None:
            assignments.append("name = ?")
            parameters.append(normalized_name)
        if normalized_note is not None:
            assignments.append("note = ?")
            parameters.append(normalized_note)
        assignments.append("updated_at = ?")
        parameters.append(_now())
        parameters.append(normalized_id)
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
            try:
                updated = connection.execute(
                    f"UPDATE {SAVED_STRATEGY_TABLE} SET {', '.join(assignments)} "
                    "WHERE strategy_id = ? RETURNING strategy_id",
                    parameters,
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        if updated is None:
            return None
        return self.read_strategy(normalized_id)

    def update_strategy_summary(
        self,
        strategy_id: str,
        summary: Mapping[str, object],
    ) -> dict[str, object] | None:
        """Replace the stored performance snapshot.

        追踪上线时用它把「研究段绩效」钉进策略里，之后**永不再改**——分母要是
        会动，衰减比就没有意义了。
        """

        self._store._require_writer()
        normalized_id = strategy_id.strip()
        if not normalized_id:
            return None
        with _WRITE_LOCK, self._store._connect() as connection:
            updated = connection.execute(
                f"UPDATE {SAVED_STRATEGY_TABLE} SET summary_json = ?, updated_at = ? "
                "WHERE strategy_id = ? RETURNING strategy_id",
                [
                    json.dumps(dict(summary), ensure_ascii=False, separators=(",", ":")),
                    _now(),
                    normalized_id,
                ],
            ).fetchone()
        if updated is None:
            return None
        return self.read_strategy(normalized_id)

    def delete_strategy(self, strategy_id: str) -> bool:
        """Delete one saved strategy together with its tracking observations."""

        self._store._require_writer()
        normalized_id = strategy_id.strip()
        if not normalized_id:
            return False
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
            try:
                deleted = connection.execute(
                    f"DELETE FROM {SAVED_STRATEGY_TABLE} "
                    "WHERE strategy_id = ? RETURNING strategy_id",
                    [normalized_id],
                ).fetchone()
                if deleted is not None:
                    # 追踪观察值是策略的附属品，留着会变成永远查不到的孤儿行。
                    connection.execute(
                        f"DELETE FROM {STRATEGY_TRACKING_TABLE} WHERE strategy_id = ?",
                        [normalized_id],
                    )
                    connection.execute(
                        f"DELETE FROM {STRATEGY_TRACKING_TRADES_TABLE} WHERE strategy_id = ?",
                        [normalized_id],
                    )
                    connection.execute(
                        f"DELETE FROM {STRATEGY_TRACKING_PLAN_TABLE} WHERE strategy_id = ?",
                        [normalized_id],
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return deleted is not None
