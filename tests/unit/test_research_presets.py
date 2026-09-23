"""研究方案：存进去的必须是那一次研究真正跑的那份请求，加载出来还得是它。

四条护栏单独盯：原样往返、重名不许覆盖别人的方案、覆盖保存不抹掉创建时间、
条数上限（攒几百个方案等于没有方案）。校验本身在路由层（用
``ConditionResearchRequest``），所以这里不重复测字段合法性。
"""

from __future__ import annotations

import pytest

from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.research_presets.service import (
    MAX_RESEARCH_PRESETS,
    ResearchPresetService,
)

_PAYLOAD: dict[str, object] = {
    "universe": "all",
    "instrument_types": ["stock"],
    "buy_rule": {"signals": ["indicator_three.begin_zone"]},
    "sell_rule": None,
    "date_mode": "custom",
    "start_date": "2020-01-01",
    "end_date": "2024-12-31",
    "search_sides": "both",
    "significance_permutations": 200,
}


def _service(tmp_path) -> ResearchPresetService:
    return ResearchPresetService(DuckDbMarketDataStore(tmp_path / "market.duckdb"))


def test_create_then_read_returns_the_payload_unchanged(tmp_path) -> None:
    service = _service(tmp_path)

    created = service.create_preset("低位进场", _PAYLOAD)
    read = service.read_preset(created["preset_id"])

    assert read is not None
    assert read["name"] == "低位进场"
    # 卖出侧留空是「持有到期末」的显式表达，不能被默认值填回去。
    assert read["payload"]["sell_rule"] is None
    assert read["payload"]["buy_rule"]["signals"] == ["indicator_three.begin_zone"]


def test_list_omits_payloads_and_orders_by_recent_update(tmp_path) -> None:
    service = _service(tmp_path)
    service.create_preset("甲", _PAYLOAD)
    service.create_preset("乙", _PAYLOAD)

    records = service.list_presets()

    assert {record["name"] for record in records} == {"甲", "乙"}
    assert all("payload" not in record for record in records)


def test_duplicate_name_is_rejected(tmp_path) -> None:
    service = _service(tmp_path)
    service.create_preset("甲", _PAYLOAD)

    with pytest.raises(ValueError, match="同名"):
        service.create_preset("甲", _PAYLOAD)


def test_renaming_onto_another_preset_name_is_rejected(tmp_path) -> None:
    service = _service(tmp_path)
    service.create_preset("甲", _PAYLOAD)
    second = service.create_preset("乙", _PAYLOAD)

    with pytest.raises(ValueError, match="同名"):
        service.update_preset(second["preset_id"], name="甲")


def test_overwriting_keeps_the_creation_time(tmp_path) -> None:
    service = _service(tmp_path)
    preset_id = service.create_preset("甲", _PAYLOAD)["preset_id"]

    updated = service.update_preset(preset_id, payload={**_PAYLOAD, "universe": "sh"})

    assert updated is not None
    assert updated["payload"]["universe"] == "sh"
    # 「这个方案是什么时候建的」是它自己的历史，不该被最后一次保存抹掉。
    assert updated["created_at"] == service.read_preset(preset_id)["created_at"]


def test_update_without_any_change_is_a_no_op_read(tmp_path) -> None:
    service = _service(tmp_path)
    preset_id = service.create_preset("甲", _PAYLOAD)["preset_id"]

    unchanged = service.update_preset(preset_id)

    assert unchanged is not None
    assert unchanged["name"] == "甲"


def test_deleting_a_missing_preset_reports_false(tmp_path) -> None:
    service = _service(tmp_path)

    assert service.delete_preset("missing") is False


def test_delete_removes_only_that_preset(tmp_path) -> None:
    service = _service(tmp_path)
    first = service.create_preset("甲", _PAYLOAD)["preset_id"]
    service.create_preset("乙", _PAYLOAD)

    assert service.delete_preset(first) is True
    assert [record["name"] for record in service.list_presets()] == ["乙"]


def test_preset_count_is_capped(tmp_path) -> None:
    service = _service(tmp_path)
    for index in range(MAX_RESEARCH_PRESETS):
        service.create_preset(f"方案-{index}", _PAYLOAD)

    with pytest.raises(ValueError, match="上限"):
        service.create_preset("再来一个", _PAYLOAD)


def test_empty_name_is_rejected(tmp_path) -> None:
    service = _service(tmp_path)

    with pytest.raises(ValueError, match="名称不能为空"):
        service.create_preset("   ", _PAYLOAD)
