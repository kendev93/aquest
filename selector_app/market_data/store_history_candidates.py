"""Persistence methods for the candidates domain."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime

from .store_base import _WRITE_LOCK, MarketDataStoreError, _DuckDbStoreBase, _now
from .store_history_common import (
    _CANDIDATE_COLUMNS,
    _CANDIDATE_STATUSES,
    CANDIDATE_LIBRARY_TABLE,
    CANDIDATE_NOTE_MAX_LENGTH,
    _decode_candidate_row,
    _normalize_timestamp,
)


class CandidateHistoryMixin:
    _store: _DuckDbStoreBase

    def find_candidate_by_source(
        self,
        source_history_id: str,
        source_candidate_id: str,
    ) -> dict[str, object] | None:
        """Look up a candidate by where it came from (入库去重用)。"""

        with self._store._connect(read_only=True) as connection:
            row = connection.execute(
                f"SELECT {_CANDIDATE_COLUMNS} FROM {CANDIDATE_LIBRARY_TABLE} "
                "WHERE source_history_id = ? AND source_candidate_id = ? LIMIT 1",
                [str(source_history_id).strip(), str(source_candidate_id).strip()],
            ).fetchone()
        if row is None:
            return None
        return _decode_candidate_row(row)

    def save_candidate(
        self,
        candidate_id: str,
        *,
        source: str,
        source_history_id: str,
        source_candidate_id: str,
        side: str,
        label: str,
        condition: Mapping[str, object],
        evidence: Mapping[str, object],
        status: str,
        note: str = "",
        published_strategy_id: str | None = None,
        created_at: datetime | str | None = None,
    ) -> dict[str, object]:
        """Persist one mined candidate together with its evidence snapshot."""

        self._store._require_writer()
        normalized_id = candidate_id.strip()
        if not normalized_id:
            raise ValueError("候选 ID 不能为空")
        if status not in _CANDIDATE_STATUSES:
            raise ValueError(f"候选状态无效: {status}")
        normalized_note = note.strip()
        if len(normalized_note) > CANDIDATE_NOTE_MAX_LENGTH:
            raise ValueError(f"候选备注不能超过 {CANDIDATE_NOTE_MAX_LENGTH} 个字符")
        created = _normalize_timestamp(created_at, _now())
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
            try:
                connection.execute(
                    f"""
                    INSERT INTO {CANDIDATE_LIBRARY_TABLE} (
                        candidate_id, source, source_history_id, source_candidate_id,
                        side, label, condition_json, evidence_json, status, note,
                        published_strategy_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (candidate_id) DO UPDATE SET
                        label = excluded.label,
                        evidence_json = excluded.evidence_json,
                        status = excluded.status,
                        note = excluded.note,
                        published_strategy_id = excluded.published_strategy_id,
                        updated_at = excluded.updated_at
                    """,
                    [
                        normalized_id,
                        source,
                        str(source_history_id).strip(),
                        str(source_candidate_id).strip(),
                        side,
                        label,
                        json.dumps(dict(condition), ensure_ascii=False, separators=(",", ":")),
                        json.dumps(dict(evidence), ensure_ascii=False, separators=(",", ":")),
                        status,
                        normalized_note,
                        published_strategy_id,
                        created,
                        created,
                    ],
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        record = self.read_candidate(normalized_id)
        if record is None:  # pragma: no cover - 刚写入的行必然读得到
            raise MarketDataStoreError("候选写入后无法读回")
        return record

    def list_candidates(self, status: str | None = None) -> list[dict[str, object]]:
        """Return stored candidates, most recently updated first."""

        normalized = status.strip() if isinstance(status, str) else ""
        if normalized and normalized not in _CANDIDATE_STATUSES:
            raise ValueError(f"候选状态无效: {normalized}")
        if normalized:
            sql = (
                f"SELECT {_CANDIDATE_COLUMNS} FROM {CANDIDATE_LIBRARY_TABLE} "
                "WHERE status = ? ORDER BY updated_at DESC, candidate_id DESC"
            )
            parameters: list[object] = [normalized]
        else:
            sql = (
                f"SELECT {_CANDIDATE_COLUMNS} FROM {CANDIDATE_LIBRARY_TABLE} "
                "ORDER BY updated_at DESC, candidate_id DESC"
            )
            parameters = []
        with self._store._connect(read_only=True) as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [_decode_candidate_row(row) for row in rows]

    def read_candidate(self, candidate_id: str) -> dict[str, object] | None:
        """Read one stored candidate."""

        with self._store._connect(read_only=True) as connection:
            row = connection.execute(
                f"SELECT {_CANDIDATE_COLUMNS} FROM {CANDIDATE_LIBRARY_TABLE} "
                "WHERE candidate_id = ?",
                [str(candidate_id).strip()],
            ).fetchone()
        if row is None:
            return None
        return _decode_candidate_row(row)

    def update_candidate(
        self,
        candidate_id: str,
        *,
        status: str | None = None,
        note: str | None = None,
        published_strategy_id: str | None = None,
        publish: bool = False,
    ) -> dict[str, object] | None:
        """Change the adoption status, the note, or the linked strategy.

        ``publish=True`` 时才写 published_strategy_id；撤回传 ``publish=True``
        且 ``published_strategy_id=None``，这样"清掉链接"和"不动链接"是两种明
        确的调用，不会靠 None 的歧义蒙混过去。
        """

        self._store._require_writer()
        normalized_id = candidate_id.strip()
        if not normalized_id:
            return None
        normalized_status = status.strip() if isinstance(status, str) else None
        if normalized_status is not None and normalized_status not in _CANDIDATE_STATUSES:
            raise ValueError(f"候选状态无效: {normalized_status}")
        normalized_note = note.strip() if isinstance(note, str) else None
        if normalized_note is not None and len(normalized_note) > CANDIDATE_NOTE_MAX_LENGTH:
            raise ValueError(f"候选备注不能超过 {CANDIDATE_NOTE_MAX_LENGTH} 个字符")
        if normalized_status is None and normalized_note is None and not publish:
            return self.read_candidate(normalized_id)
        assignments: list[str] = []
        parameters: list[object] = []
        if normalized_status is not None:
            assignments.append("status = ?")
            parameters.append(normalized_status)
        if normalized_note is not None:
            assignments.append("note = ?")
            parameters.append(normalized_note)
        if publish:
            assignments.append("published_strategy_id = ?")
            parameters.append(published_strategy_id)
        assignments.append("updated_at = ?")
        parameters.append(_now())
        parameters.append(normalized_id)
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
            try:
                updated = connection.execute(
                    f"UPDATE {CANDIDATE_LIBRARY_TABLE} SET {', '.join(assignments)} "
                    "WHERE candidate_id = ? RETURNING candidate_id",
                    parameters,
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        if updated is None:
            return None
        return self.read_candidate(normalized_id)

    def delete_candidate(self, candidate_id: str) -> bool:
        """Delete one stored candidate."""

        self._store._require_writer()
        normalized_id = candidate_id.strip()
        if not normalized_id:
            return False
        with _WRITE_LOCK, self._store._connect() as connection:
            deleted = connection.execute(
                f"DELETE FROM {CANDIDATE_LIBRARY_TABLE} WHERE candidate_id = ? "
                "RETURNING candidate_id",
                [normalized_id],
            ).fetchone()
        return deleted is not None
