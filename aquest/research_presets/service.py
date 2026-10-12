"""研究方案：买卖条件研究页整套可复现输入的保存与加载。

与筛选方案（``screen_presets``）是同一件事的两个页面，差别只在 payload 的形状：
这里是研究请求本身，也就是历史记录里存的那份 request。校验不在这里做——它
由路由层用 ``ConditionResearchRequest`` 完成，和真正跑研究用的是同一个模型，
所以不会出现「存得进去但跑不起来」的方案。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, cast

MAX_RESEARCH_PRESETS = 200
RESEARCH_PRESET_NAME_MAX_LENGTH = 60
RESEARCH_PRESET_NOTE_MAX_LENGTH = 200


class ResearchPresetService:
    """Named, reloadable buy/sell condition research configurations."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def list_presets(self) -> list[dict[str, object]]:
        """Every saved preset without its payload (names are enough to pick one)."""

        return cast("list[dict[str, object]]", self._store.list_research_presets())

    def read_preset(self, preset_id: str) -> dict[str, object] | None:
        return cast("dict[str, object] | None", self._store.read_research_preset(preset_id))

    def create_preset(
        self,
        name: str,
        payload: Mapping[str, object],
        note: str = "",
    ) -> dict[str, object]:
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("研究方案名称不能为空")
        if len(normalized_name) > RESEARCH_PRESET_NAME_MAX_LENGTH:
            raise ValueError(f"研究方案名称不能超过 {RESEARCH_PRESET_NAME_MAX_LENGTH} 个字符")
        if len(self.list_presets()) >= MAX_RESEARCH_PRESETS:
            raise ValueError(
                f"研究方案数量已达上限 {MAX_RESEARCH_PRESETS} 个，请先删除不再使用的方案"
            )
        normalized_note = note.strip()
        if len(normalized_note) > RESEARCH_PRESET_NOTE_MAX_LENGTH:
            raise ValueError(f"研究方案备注不能超过 {RESEARCH_PRESET_NOTE_MAX_LENGTH} 个字符")
        return cast(
            "dict[str, object]",
            self._store.save_research_preset(
                uuid.uuid4().hex,
                name=normalized_name,
                payload=payload,
                note=normalized_note,
            ),
        )

    def update_preset(
        self,
        preset_id: str,
        *,
        name: str | None = None,
        note: str | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object] | None:
        return cast(
            "dict[str, object] | None",
            self._store.update_research_preset(preset_id, name=name, note=note, payload=payload),
        )

    def delete_preset(self, preset_id: str) -> bool:
        return bool(self._store.delete_research_preset(preset_id))


__all__ = [
    "MAX_RESEARCH_PRESETS",
    "RESEARCH_PRESET_NAME_MAX_LENGTH",
    "RESEARCH_PRESET_NOTE_MAX_LENGTH",
    "ResearchPresetService",
]
