"""Persistence methods for the candidates domain."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime

from .store_base import _WRITE_LOCK, MarketDataStoreError, _DuckDbStoreBase, _now
from .store_history_common import (
    _CANDIDATE_COLUMNS,
    _CANDIDATE_STATUSES,
    _STRATEGY_COLUMNS,
    CANDIDATE_LIBRARY_TABLE,
    CANDIDATE_NOTE_MAX_LENGTH,
    CANDIDATE_STATUS_ADOPTED,
    CANDIDATE_STATUS_PUBLISHED,
    SAVED_STRATEGY_TABLE,
    STRATEGY_NAME_MAX_LENGTH,
    STRATEGY_NOTE_MAX_LENGTH,
    STRATEGY_TRACKING_OFF,
    _decode_candidate_row,
    _decode_strategy_row,
    _normalize_timestamp,
)


class CandidatePublishConflict(MarketDataStoreError):
    """Candidate changed or disappeared while its publish plan was prepared."""


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

    def publish_candidate_strategy(
        self,
        candidate_id: str,
        *,
        expected_updated_at: object,
        strategy_id: str,
        name: str,
        note: str,
        request: Mapping[str, object],
        summary: Mapping[str, object],
        source_history_id: str | None,
    ) -> tuple[dict[str, object], dict[str, object]]:
        """Atomically publish a strategy and transition its adopted candidate.

        The expected timestamp is a compare-and-swap token for the candidate used
        to prepare the request. Both returned records are read before commit, so
        a concurrent delete cannot turn a committed publish into a ``None`` result.
        """

        self._store._require_writer()
        normalized_candidate = candidate_id.strip()
        normalized_strategy = strategy_id.strip()
        normalized_name = name.strip()
        normalized_note = note.strip()
        if not normalized_candidate or not normalized_strategy:
            raise ValueError("候选 ID 和策略 ID 不能为空")
        if not normalized_name:
            raise ValueError("策略名称不能为空")
        if len(normalized_name) > STRATEGY_NAME_MAX_LENGTH:
            raise ValueError(f"策略名称不能超过 {STRATEGY_NAME_MAX_LENGTH} 个字符")
        if len(normalized_note) > STRATEGY_NOTE_MAX_LENGTH:
            raise ValueError(f"策略备注不能超过 {STRATEGY_NOTE_MAX_LENGTH} 个字符")
        created = _normalize_timestamp(None, _now())
        normalized_source = (source_history_id or "").strip() or None
        encoded_request = json.dumps(dict(request), ensure_ascii=False, separators=(",", ":"))
        encoded_summary = json.dumps(dict(summary), ensure_ascii=False, separators=(",", ":"))
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
            try:
                name_taken = connection.execute(
                    f"SELECT 1 FROM {SAVED_STRATEGY_TABLE} WHERE name = ? LIMIT 1",
                    [normalized_name],
                ).fetchone()
                if name_taken is not None:
                    raise ValueError(f"已存在同名策略: {normalized_name}")
                candidate_row = connection.execute(
                    f"SELECT {_CANDIDATE_COLUMNS} FROM {CANDIDATE_LIBRARY_TABLE} "
                    "WHERE candidate_id = ? AND status = ? AND updated_at = CAST(? AS TIMESTAMP)",
                    [normalized_candidate, CANDIDATE_STATUS_ADOPTED, expected_updated_at],
                ).fetchone()
                if candidate_row is None:
                    raise CandidatePublishConflict("候选状态或内容已变化，请刷新后重新发布")
                connection.execute(
                    f"""
                    INSERT INTO {SAVED_STRATEGY_TABLE} (
                        strategy_id, name, note, source_history_id, created_at,
                        updated_at, request_json, summary_json, tracking_status, launched_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    [
                        normalized_strategy,
                        normalized_name,
                        normalized_note,
                        normalized_source,
                        created,
                        created,
                        encoded_request,
                        encoded_summary,
                        STRATEGY_TRACKING_OFF,
                    ],
                )
                updated_row = connection.execute(
                    f"""
                    UPDATE {CANDIDATE_LIBRARY_TABLE}
                    SET status = ?, published_strategy_id = ?, updated_at = ?
                    WHERE candidate_id = ? AND status = ?
                      AND updated_at = CAST(? AS TIMESTAMP)
                    RETURNING {_CANDIDATE_COLUMNS}
                    """,
                    [
                        CANDIDATE_STATUS_PUBLISHED,
                        normalized_strategy,
                        created,
                        normalized_candidate,
                        CANDIDATE_STATUS_ADOPTED,
                        expected_updated_at,
                    ],
                ).fetchone()
                if updated_row is None:
                    raise CandidatePublishConflict("候选状态或内容已变化，请刷新后重新发布")
                strategy_row = connection.execute(
                    f"SELECT {_STRATEGY_COLUMNS} FROM {SAVED_STRATEGY_TABLE} WHERE strategy_id = ?",
                    [normalized_strategy],
                ).fetchone()
                if strategy_row is None:  # pragma: no cover - inserted above in same transaction
                    raise MarketDataStoreError("策略写入后无法读回")
                candidate = _decode_candidate_row(updated_row)
                strategy = _decode_strategy_row(strategy_row)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return candidate, strategy
