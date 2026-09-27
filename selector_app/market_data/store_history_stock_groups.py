"""Persistence methods for the stock groups domain."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence, Sized
from datetime import datetime
from typing import cast

from .store_base import _WRITE_LOCK, _DuckDbStoreBase, _now
from .store_history_common import (
    _STOCK_GROUP_COLUMNS,
    _STOCK_GROUP_CRITERIA_COLUMNS,
    STOCK_GROUP_CRITERIA_PAYLOAD_MAX_LENGTH,
    STOCK_GROUP_CRITERIA_SUMMARY_MAX_LENGTH,
    STOCK_GROUP_CRITERIA_TABLE,
    STOCK_GROUP_MAX_ITEMS,
    STOCK_GROUP_NAME_MAX_LENGTH,
    STOCK_GROUP_NOTE_MAX_LENGTH,
    STOCK_GROUP_SOURCE_MANUAL,
    STOCK_GROUP_TABLE,
    _decode_stock_group_row,
    _normalize_timestamp,
    _stock_group_record,
)


class StockGroupHistoryMixin:
    _store: _DuckDbStoreBase

    def stock_group_name_taken(self, name: str, *, exclude_group_id: str = "") -> bool:
        """Whether a group name is already used by another group."""

        normalized = name.strip()
        if not normalized:
            return False
        with self._store._connect(read_only=True) as connection:
            row = connection.execute(
                f"SELECT 1 FROM {STOCK_GROUP_TABLE} WHERE name = ? AND group_id <> ? LIMIT 1",
                [normalized, exclude_group_id.strip()],
            ).fetchone()
        return row is not None

    def save_stock_group(
        self,
        group_id: str,
        *,
        name: str,
        items: Sequence[Mapping[str, object]],
        note: str = "",
        source: str = STOCK_GROUP_SOURCE_MANUAL,
        source_history_id: str | None = None,
        created_at: datetime | str | None = None,
    ) -> dict[str, object]:
        """Persist one named basket of instruments.

        成员整份存 JSON 而不是拆一张子表：一组的量级是几十只，读写都是整取整存，
        拆表只会多一次 join 且没法保证顺序。顺序有意义——用户勾选的先后就是他想
        看到的样子，不能让 SQL 的返回顺序去决定。
        """

        self._store._require_writer()
        normalized_id = group_id.strip()
        if not normalized_id:
            raise ValueError("分组 ID 不能为空")
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("分组名称不能为空")
        if len(normalized_name) > STOCK_GROUP_NAME_MAX_LENGTH:
            raise ValueError(f"分组名称不能超过 {STOCK_GROUP_NAME_MAX_LENGTH} 个字符")
        normalized_note = note.strip()
        if len(normalized_note) > STOCK_GROUP_NOTE_MAX_LENGTH:
            raise ValueError(f"分组备注不能超过 {STOCK_GROUP_NOTE_MAX_LENGTH} 个字符")
        normalized_items = [dict(item) for item in items]
        if not normalized_items:
            raise ValueError("分组至少要包含一只标的")
        if len(normalized_items) > STOCK_GROUP_MAX_ITEMS:
            raise ValueError(f"一组最多只能放 {STOCK_GROUP_MAX_ITEMS} 只标的")
        if self.stock_group_name_taken(normalized_name, exclude_group_id=normalized_id):
            raise ValueError(f"已存在同名分组: {normalized_name}")
        created = _normalize_timestamp(created_at, _now())
        record = {
            "group_id": normalized_id,
            "name": normalized_name,
            "note": normalized_note,
            "source": source,
            "source_history_id": source_history_id,
            "created_at": created,
            "updated_at": created,
            "items": normalized_items,
        }
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
            try:
                connection.execute(
                    f"""
                    INSERT INTO {STOCK_GROUP_TABLE} (
                        group_id, name, note, source, source_history_id,
                        created_at, updated_at, items_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (group_id) DO UPDATE SET
                        name = excluded.name,
                        note = excluded.note,
                        updated_at = excluded.updated_at,
                        items_json = excluded.items_json
                    """,
                    [
                        normalized_id,
                        normalized_name,
                        normalized_note,
                        source,
                        source_history_id,
                        created,
                        created,
                        json.dumps(normalized_items, ensure_ascii=False, separators=(",", ":")),
                    ],
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return _stock_group_record(record)

    def list_stock_groups(self) -> list[dict[str, object]]:
        """Return every group, most recently updated first, without members."""

        return self._list_stock_groups(with_items=False)

    def list_stock_groups_with_items(self) -> list[dict[str, object]]:
        """Same list, members included - for maths that needs every group at once.

        批量阶段收益要一次拿到所有组的成员；逐个 read 会让「算一次全部组」变成
        N+1 次查询，组数不限的情况下迟早咬人。
        """

        return self._list_stock_groups(with_items=True)

    def _list_stock_groups(self, *, with_items: bool) -> list[dict[str, object]]:
        with self._store._connect(read_only=True) as connection:
            rows = connection.execute(
                f"""
                SELECT {_STOCK_GROUP_COLUMNS}
                FROM {STOCK_GROUP_TABLE}
                ORDER BY updated_at DESC, group_id DESC
                """
            ).fetchall()
        records: list[dict[str, object]] = []
        for row in rows:
            decoded = _decode_stock_group_row(row)
            decoded["item_count"] = len(cast(Sized, decoded["items"]))
            if not with_items:
                decoded.pop("items", None)
            records.append(decoded)
        return records

    def read_stock_group(self, group_id: str) -> dict[str, object] | None:
        """Read one group including its members."""

        with self._store._connect(read_only=True) as connection:
            row = connection.execute(
                f"SELECT {_STOCK_GROUP_COLUMNS} FROM {STOCK_GROUP_TABLE} WHERE group_id = ?",
                [str(group_id).strip()],
            ).fetchone()
        if row is None:
            return None
        return _decode_stock_group_row(row)

    def update_stock_group(
        self,
        group_id: str,
        *,
        name: str | None = None,
        note: str | None = None,
        items: Sequence[Mapping[str, object]] | None = None,
    ) -> dict[str, object] | None:
        """Rename, re-note or replace the members of one group.

        改名不改 created_at：这组是什么时候建的属于它自己的历史，不该被最后一次
        编辑抹掉。成员是整份替换，不提供「增量加一只」——增量会让「是谁加的、什么
        时候加的」变成一笔糊涂账，而成员来源本来就该在保存时一次性确定。
        """

        self._store._require_writer()
        normalized_id = group_id.strip()
        if not normalized_id:
            return None
        if name is None and note is None and items is None:
            return self.read_stock_group(normalized_id)
        normalized_name = name.strip() if isinstance(name, str) else None
        if normalized_name is not None:
            if not normalized_name:
                raise ValueError("分组名称不能为空")
            if len(normalized_name) > STOCK_GROUP_NAME_MAX_LENGTH:
                raise ValueError(f"分组名称不能超过 {STOCK_GROUP_NAME_MAX_LENGTH} 个字符")
            if self.stock_group_name_taken(normalized_name, exclude_group_id=normalized_id):
                raise ValueError(f"已存在同名分组: {normalized_name}")
        normalized_note = note.strip() if isinstance(note, str) else None
        if normalized_note is not None and len(normalized_note) > STOCK_GROUP_NOTE_MAX_LENGTH:
            raise ValueError(f"分组备注不能超过 {STOCK_GROUP_NOTE_MAX_LENGTH} 个字符")
        normalized_items: list[dict[str, object]] | None = None
        if items is not None:
            normalized_items = [dict(item) for item in items]
            if not normalized_items:
                raise ValueError("分组至少要包含一只标的")
            if len(normalized_items) > STOCK_GROUP_MAX_ITEMS:
                raise ValueError(f"一组最多只能放 {STOCK_GROUP_MAX_ITEMS} 只标的")
        assignments: list[str] = []
        parameters: list[object] = []
        if normalized_name is not None:
            assignments.append("name = ?")
            parameters.append(normalized_name)
        if normalized_note is not None:
            assignments.append("note = ?")
            parameters.append(normalized_note)
        if normalized_items is not None:
            assignments.append("items_json = ?")
            parameters.append(
                json.dumps(normalized_items, ensure_ascii=False, separators=(",", ":"))
            )
        assignments.append("updated_at = ?")
        parameters.append(_now())
        parameters.append(normalized_id)
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
            try:
                updated = connection.execute(
                    f"UPDATE {STOCK_GROUP_TABLE} SET {', '.join(assignments)} "
                    "WHERE group_id = ? RETURNING group_id",
                    parameters,
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        if updated is None:
            return None
        return self.read_stock_group(normalized_id)

    def delete_stock_group(self, group_id: str) -> bool:
        """Delete one group, together with its criteria snapshot.

        快照跟组走：组没了还留着一条孤儿快照，导出的备份里就会多出一行谁也读不
        到的记录。两个删除放在同一个事务里，避免出现「组删了快照还在」。
        """

        self._store._require_writer()
        normalized_id = group_id.strip()
        if not normalized_id:
            return False
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
            try:
                deleted = connection.execute(
                    f"DELETE FROM {STOCK_GROUP_TABLE} WHERE group_id = ? RETURNING group_id",
                    [normalized_id],
                ).fetchone()
                if deleted is not None:
                    connection.execute(
                        f"DELETE FROM {STOCK_GROUP_CRITERIA_TABLE} WHERE group_id = ?",
                        [normalized_id],
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return deleted is not None

    def save_stock_group_criteria(
        self,
        group_id: str,
        *,
        criteria: Mapping[str, object],
        created_at: datetime | str | None = None,
    ) -> dict[str, object]:
        """Store what the group was selected by, as a snapshot.

        存快照而不是回指选股历史：历史每种类型只留最近 20 条，等组还在、历史已经
        被淘汰时，「这组是怎么选出来的」就永远读不出来了。
        """

        self._store._require_writer()
        normalized_id = group_id.strip()
        if not normalized_id:
            raise ValueError("分组 ID 不能为空")
        summary = criteria.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("筛选条件摘要不能为空")
        if len(summary.strip()) > STOCK_GROUP_CRITERIA_SUMMARY_MAX_LENGTH:
            raise ValueError(
                f"筛选条件摘要不能超过 {STOCK_GROUP_CRITERIA_SUMMARY_MAX_LENGTH} 个字符"
            )
        payload = criteria.get("payload")
        if payload is not None and not isinstance(payload, Mapping):
            raise ValueError("筛选条件快照必须是对象")
        encoded = json.dumps(
            {"summary": summary.strip(), "payload": dict(payload) if payload else None},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(encoded) > STOCK_GROUP_CRITERIA_PAYLOAD_MAX_LENGTH:
            raise ValueError("筛选条件快照过大，请精简后再保存")
        stamp = _normalize_timestamp(created_at, _now())
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
            try:
                connection.execute(
                    f"""
                    INSERT INTO {STOCK_GROUP_CRITERIA_TABLE} (
                        group_id, criteria_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT (group_id) DO UPDATE SET
                        criteria_json = excluded.criteria_json,
                        updated_at = excluded.updated_at
                    """,
                    [normalized_id, encoded, stamp, stamp],
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {"group_id": normalized_id, **json.loads(encoded)}

    def read_stock_group_criteria(
        self,
        group_ids: Sequence[str],
    ) -> dict[str, dict[str, object]]:
        """Criteria for many groups at once - never one query per group."""

        wanted = {str(group_id).strip() for group_id in group_ids if str(group_id).strip()}
        if not wanted:
            return {}
        placeholders = ", ".join("?" for _ in wanted)
        with self._store._connect(read_only=True) as connection:
            rows = connection.execute(
                f"SELECT {_STOCK_GROUP_CRITERIA_COLUMNS} FROM {STOCK_GROUP_CRITERIA_TABLE} "
                f"WHERE group_id IN ({placeholders})",
                list(wanted),
            ).fetchall()
        decoded: dict[str, dict[str, object]] = {}
        for row in rows:
            try:
                payload = json.loads(str(row[1]))
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, Mapping):
                continue
            decoded[str(row[0])] = {
                "group_id": str(row[0]),
                **dict(payload),
                "created_at": str(row[2]),
                "updated_at": str(row[3]),
            }
        return decoded

    def delete_stock_group_criteria(self, group_id: str) -> bool:
        """Drop the criteria snapshot of one group."""

        self._store._require_writer()
        normalized_id = group_id.strip()
        if not normalized_id:
            return False
        with _WRITE_LOCK, self._store._connect() as connection:
            deleted = connection.execute(
                f"DELETE FROM {STOCK_GROUP_CRITERIA_TABLE} WHERE group_id = ? RETURNING group_id",
                [normalized_id],
            ).fetchone()
        return deleted is not None
