"""Persistence methods for the presets domain."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime

from .store_base import _WRITE_LOCK, _DuckDbStoreBase, _now
from .store_history_common import (
    _PRESET_COLUMNS,
    PRESET_NAME_MAX_LENGTH,
    PRESET_NOTE_MAX_LENGTH,
    RESEARCH_PRESET_TABLE,
    SCREEN_PRESET_TABLE,
    _decode_preset_row,
    _normalize_timestamp,
    _preset_record,
)


class PresetHistoryMixin:
    _store: _DuckDbStoreBase

    def screen_preset_name_taken(self, name: str, *, exclude_preset_id: str = "") -> bool:
        """Whether a preset name is already used by another record."""

        normalized = name.strip()
        if not normalized:
            return False
        with self._store._connect(read_only=True) as connection:
            row = connection.execute(
                f"SELECT 1 FROM {SCREEN_PRESET_TABLE} WHERE name = ? AND preset_id <> ? LIMIT 1",
                [normalized, exclude_preset_id.strip()],
            ).fetchone()
        return row is not None

    def save_screen_preset(
        self,
        preset_id: str,
        *,
        name: str,
        payload: Mapping[str, object],
        note: str = "",
        created_at: datetime | str | None = None,
    ) -> dict[str, object]:
        """Persist one reusable screening configuration."""

        self._store._require_writer()
        normalized_id = preset_id.strip()
        if not normalized_id:
            raise ValueError("方案 ID 不能为空")
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("方案名称不能为空")
        if len(normalized_name) > PRESET_NAME_MAX_LENGTH:
            raise ValueError(f"方案名称不能超过 {PRESET_NAME_MAX_LENGTH} 个字符")
        normalized_note = note.strip()
        if len(normalized_note) > PRESET_NOTE_MAX_LENGTH:
            raise ValueError(f"方案备注不能超过 {PRESET_NOTE_MAX_LENGTH} 个字符")
        if self.screen_preset_name_taken(normalized_name, exclude_preset_id=normalized_id):
            raise ValueError(f"已存在同名方案: {normalized_name}")
        created = _normalize_timestamp(created_at, _now())
        record = {
            "preset_id": normalized_id,
            "name": normalized_name,
            "note": normalized_note,
            "created_at": created,
            "updated_at": created,
            "payload": dict(payload),
        }
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
            try:
                connection.execute(
                    f"""
                    INSERT INTO {SCREEN_PRESET_TABLE} (
                        preset_id, name, note, created_at, updated_at, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (preset_id) DO UPDATE SET
                        name = excluded.name,
                        note = excluded.note,
                        updated_at = excluded.updated_at,
                        payload_json = excluded.payload_json
                    """,
                    [
                        normalized_id,
                        normalized_name,
                        normalized_note,
                        created,
                        created,
                        json.dumps(record["payload"], ensure_ascii=False, separators=(",", ":")),
                    ],
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return _preset_record(record)

    def list_screen_presets(self) -> list[dict[str, object]]:
        """Return every saved preset, most recently updated first, without payloads."""

        with self._store._connect(read_only=True) as connection:
            rows = connection.execute(
                f"""
                SELECT {_PRESET_COLUMNS}
                FROM {SCREEN_PRESET_TABLE}
                ORDER BY updated_at DESC, preset_id DESC
                """
            ).fetchall()
        records: list[dict[str, object]] = []
        for row in rows:
            payload = _decode_preset_row(row)
            payload.pop("payload", None)
            records.append(payload)
        return records

    def read_screen_preset(self, preset_id: str) -> dict[str, object] | None:
        """Read one saved preset including its replayable payload."""

        with self._store._connect(read_only=True) as connection:
            row = connection.execute(
                f"SELECT {_PRESET_COLUMNS} FROM {SCREEN_PRESET_TABLE} WHERE preset_id = ?",
                [str(preset_id).strip()],
            ).fetchone()
        if row is None:
            return None
        return _decode_preset_row(row)

    def update_screen_preset(
        self,
        preset_id: str,
        *,
        name: str | None = None,
        note: str | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object] | None:
        """Rename, re-note or overwrite the payload of one preset.

        覆盖保存只改 payload 与 updated_at，created_at 不动——「这个方案是什么时候
        建的」是它自己的历史，不该被最后一次保存抹掉。
        """

        self._store._require_writer()
        normalized_id = preset_id.strip()
        if not normalized_id:
            return None
        if name is None and note is None and payload is None:
            return self.read_screen_preset(normalized_id)
        normalized_name = name.strip() if isinstance(name, str) else None
        if normalized_name is not None:
            if not normalized_name:
                raise ValueError("方案名称不能为空")
            if len(normalized_name) > PRESET_NAME_MAX_LENGTH:
                raise ValueError(f"方案名称不能超过 {PRESET_NAME_MAX_LENGTH} 个字符")
            if self.screen_preset_name_taken(normalized_name, exclude_preset_id=normalized_id):
                raise ValueError(f"已存在同名方案: {normalized_name}")
        normalized_note = note.strip() if isinstance(note, str) else None
        if normalized_note is not None and len(normalized_note) > PRESET_NOTE_MAX_LENGTH:
            raise ValueError(f"方案备注不能超过 {PRESET_NOTE_MAX_LENGTH} 个字符")
        assignments: list[str] = []
        parameters: list[object] = []
        if normalized_name is not None:
            assignments.append("name = ?")
            parameters.append(normalized_name)
        if normalized_note is not None:
            assignments.append("note = ?")
            parameters.append(normalized_note)
        if payload is not None:
            assignments.append("payload_json = ?")
            parameters.append(json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")))
        assignments.append("updated_at = ?")
        parameters.append(_now())
        parameters.append(normalized_id)
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
            try:
                updated = connection.execute(
                    f"UPDATE {SCREEN_PRESET_TABLE} SET {', '.join(assignments)} "
                    "WHERE preset_id = ? RETURNING preset_id",
                    parameters,
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        if updated is None:
            return None
        return self.read_screen_preset(normalized_id)

    def delete_screen_preset(self, preset_id: str) -> bool:
        """Delete one saved preset."""

        self._store._require_writer()
        normalized_id = preset_id.strip()
        if not normalized_id:
            return False
        with _WRITE_LOCK, self._store._connect() as connection:
            deleted = connection.execute(
                f"DELETE FROM {SCREEN_PRESET_TABLE} WHERE preset_id = ? RETURNING preset_id",
                [normalized_id],
            ).fetchone()
        return deleted is not None

    def research_preset_name_taken(self, name: str, *, exclude_preset_id: str = "") -> bool:
        """Whether a research preset name is already used by another record."""

        normalized = name.strip()
        if not normalized:
            return False
        with self._store._connect(read_only=True) as connection:
            row = connection.execute(
                f"SELECT 1 FROM {RESEARCH_PRESET_TABLE} WHERE name = ? AND preset_id <> ? LIMIT 1",
                [normalized, exclude_preset_id.strip()],
            ).fetchone()
        return row is not None

    def save_research_preset(
        self,
        preset_id: str,
        *,
        name: str,
        payload: Mapping[str, object],
        note: str = "",
        created_at: datetime | str | None = None,
    ) -> dict[str, object]:
        """Persist one reusable research configuration."""

        self._store._require_writer()
        normalized_id = preset_id.strip()
        if not normalized_id:
            raise ValueError("研究方案 ID 不能为空")
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("研究方案名称不能为空")
        if len(normalized_name) > PRESET_NAME_MAX_LENGTH:
            raise ValueError(f"研究方案名称不能超过 {PRESET_NAME_MAX_LENGTH} 个字符")
        normalized_note = note.strip()
        if len(normalized_note) > PRESET_NOTE_MAX_LENGTH:
            raise ValueError(f"研究方案备注不能超过 {PRESET_NOTE_MAX_LENGTH} 个字符")
        if self.research_preset_name_taken(normalized_name, exclude_preset_id=normalized_id):
            raise ValueError(f"已存在同名研究方案: {normalized_name}")
        created = _normalize_timestamp(created_at, _now())
        record = {
            "preset_id": normalized_id,
            "name": normalized_name,
            "note": normalized_note,
            "created_at": created,
            "updated_at": created,
            "payload": dict(payload),
        }
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
            try:
                connection.execute(
                    f"""
                    INSERT INTO {RESEARCH_PRESET_TABLE} (
                        preset_id, name, note, created_at, updated_at, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (preset_id) DO UPDATE SET
                        name = excluded.name,
                        note = excluded.note,
                        updated_at = excluded.updated_at,
                        payload_json = excluded.payload_json
                    """,
                    [
                        normalized_id,
                        normalized_name,
                        normalized_note,
                        created,
                        created,
                        json.dumps(record["payload"], ensure_ascii=False, separators=(",", ":")),
                    ],
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return _preset_record(record)

    def list_research_presets(self) -> list[dict[str, object]]:
        """Return every saved research preset, most recently updated first, without payloads."""

        with self._store._connect(read_only=True) as connection:
            rows = connection.execute(
                f"""
                SELECT {_PRESET_COLUMNS}
                FROM {RESEARCH_PRESET_TABLE}
                ORDER BY updated_at DESC, preset_id DESC
                """
            ).fetchall()
        records: list[dict[str, object]] = []
        for row in rows:
            payload = _decode_preset_row(row)
            payload.pop("payload", None)
            records.append(payload)
        return records

    def read_research_preset(self, preset_id: str) -> dict[str, object] | None:
        """Read one saved research preset including its replayable payload."""

        with self._store._connect(read_only=True) as connection:
            row = connection.execute(
                f"SELECT {_PRESET_COLUMNS} FROM {RESEARCH_PRESET_TABLE} WHERE preset_id = ?",
                [str(preset_id).strip()],
            ).fetchone()
        if row is None:
            return None
        return _decode_preset_row(row)

    def update_research_preset(
        self,
        preset_id: str,
        *,
        name: str | None = None,
        note: str | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object] | None:
        """Rename, re-note or overwrite the payload of one research preset.

        覆盖保存只改 payload 与 updated_at，created_at 不动——「这个方案是什么时候
        建的」是它自己的历史，不该被最后一次保存抹掉。
        """

        self._store._require_writer()
        normalized_id = preset_id.strip()
        if not normalized_id:
            return None
        if name is None and note is None and payload is None:
            return self.read_research_preset(normalized_id)
        normalized_name = name.strip() if isinstance(name, str) else None
        if normalized_name is not None:
            if not normalized_name:
                raise ValueError("研究方案名称不能为空")
            if len(normalized_name) > PRESET_NAME_MAX_LENGTH:
                raise ValueError(f"研究方案名称不能超过 {PRESET_NAME_MAX_LENGTH} 个字符")
            if self.research_preset_name_taken(normalized_name, exclude_preset_id=normalized_id):
                raise ValueError(f"已存在同名研究方案: {normalized_name}")
        normalized_note = note.strip() if isinstance(note, str) else None
        if normalized_note is not None and len(normalized_note) > PRESET_NOTE_MAX_LENGTH:
            raise ValueError(f"研究方案备注不能超过 {PRESET_NOTE_MAX_LENGTH} 个字符")
        assignments: list[str] = []
        parameters: list[object] = []
        if normalized_name is not None:
            assignments.append("name = ?")
            parameters.append(normalized_name)
        if normalized_note is not None:
            assignments.append("note = ?")
            parameters.append(normalized_note)
        if payload is not None:
            assignments.append("payload_json = ?")
            parameters.append(json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")))
        assignments.append("updated_at = ?")
        parameters.append(_now())
        parameters.append(normalized_id)
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
            try:
                updated = connection.execute(
                    f"UPDATE {RESEARCH_PRESET_TABLE} SET {', '.join(assignments)} "
                    "WHERE preset_id = ? RETURNING preset_id",
                    parameters,
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        if updated is None:
            return None
        return self.read_research_preset(normalized_id)

    def delete_research_preset(self, preset_id: str) -> bool:
        """Delete one saved research preset."""

        self._store._require_writer()
        normalized_id = preset_id.strip()
        if not normalized_id:
            return False
        with _WRITE_LOCK, self._store._connect() as connection:
            deleted = connection.execute(
                f"DELETE FROM {RESEARCH_PRESET_TABLE} WHERE preset_id = ? RETURNING preset_id",
                [normalized_id],
            ).fetchone()
        return deleted is not None
