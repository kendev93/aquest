"""筛选方案的读写服务。

只有一层薄封装：名字唯一性、条数上限、内容白名单裁剪都在这一层收口，
存储层不认识「方案」这个概念（它只认 preset_id / name / payload_json）。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, cast

from .payload import normalize_payload

MAX_PRESETS = 200
PRESET_NAME_MAX_LENGTH = 60
PRESET_NOTE_MAX_LENGTH = 200


class ScreenPresetService:
    """Named, reloadable screening configurations."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def list_presets(self) -> list[dict[str, object]]:
        """Every saved preset without its payload (names are enough to pick one)."""

        return cast("list[dict[str, object]]", self._store.list_screen_presets())

    def read_preset(self, preset_id: str) -> dict[str, object] | None:
        return cast("dict[str, object] | None", self._store.read_screen_preset(preset_id))

    def create_preset(
        self,
        name: str,
        payload: Mapping[str, object],
        note: str = "",
    ) -> dict[str, object]:
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("方案名称不能为空")
        if len(normalized_name) > PRESET_NAME_MAX_LENGTH:
            raise ValueError(f"方案名称不能超过 {PRESET_NAME_MAX_LENGTH} 个字符")
        if len(self.list_presets()) >= MAX_PRESETS:
            raise ValueError(f"方案数量已达上限 {MAX_PRESETS} 个，请先删除不再使用的方案")
        normalized_note = note.strip()
        if len(normalized_note) > PRESET_NOTE_MAX_LENGTH:
            raise ValueError(f"方案备注不能超过 {PRESET_NOTE_MAX_LENGTH} 个字符")
        clipped, notes = normalize_payload(payload)
        record = self._store.save_screen_preset(
            uuid.uuid4().hex,
            name=normalized_name,
            payload=clipped,
            note=normalized_note,
        )
        return {**record, "notes": notes}

    def update_preset(
        self,
        preset_id: str,
        *,
        name: str | None = None,
        note: str | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object] | None:
        clipped: dict[str, object] | None = None
        notes: list[str] = []
        if payload is not None:
            clipped, notes = normalize_payload(payload)
        updated = self._store.update_screen_preset(preset_id, name=name, note=note, payload=clipped)
        if updated is None:
            return None
        return {**updated, "notes": notes}

    def delete_preset(self, preset_id: str) -> bool:
        return bool(self._store.delete_screen_preset(preset_id))
