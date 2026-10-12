"""筛选方案：存进去的是什么，加载出来就必须还是什么。

三条护栏单独盯：白名单裁剪（不认识的字段不留）、重名不许覆盖别人的方案、
条数上限（攒几百个方案等于没有方案）。
"""

from __future__ import annotations

import duckdb
import pytest

from aquest.market_data.store import DuckDbMarketDataStore
from aquest.market_data.store_base import SCHEMA_VERSION
from aquest.screen_presets.payload import (
    MAX_CONDITIONS,
    MAX_SIGNALS,
    normalize_payload,
)
from aquest.screen_presets.service import MAX_PRESETS, ScreenPresetService

_PAYLOAD = {
    "mode": "preset",
    "rule_mode": "conditions",
    "selected_signals": ["indicator_one.main_force_entry"],
    "conditions": [
        {"left_value": "indicator_one.wash", "operator": "lt", "right_constant": 10},
    ],
    "combine_mode": "all",
    "minimum_matches": None,
    "universe": "all",
    "universe_file": "",
    "instrument_types": ["stock"],
    "boards": ["main"],
    "workers": 8,
    "period": "daily",
    "formula_text": "",
    "formula_parameters": {},
}


def _service(tmp_path) -> ScreenPresetService:
    return ScreenPresetService(DuckDbMarketDataStore(tmp_path / "market.duckdb"))


# ----------------------------------------------------------------------
# payload 归一化
# ----------------------------------------------------------------------


def test_normalize_drops_unknown_fields_and_reports_them() -> None:
    payload, notes = normalize_payload({**_PAYLOAD, "results": [1, 2], "job_id": "abc"})

    assert "results" not in payload
    assert "job_id" not in payload
    assert any("未知字段" in note for note in notes)


def test_normalize_rejects_conditions_that_cannot_be_evaluated() -> None:
    payload, notes = normalize_payload(
        {
            **_PAYLOAD,
            "conditions": [
                {"left_value": "indicator_one.wash", "operator": "lt", "right_constant": 10},
                {"left_value": "", "operator": "lt", "right_constant": 1},
                {"left_value": "a", "operator": "nope", "right_constant": 1},
                # 左右都不给 / 左右都给 —— 都求不出值
                {"left_value": "a", "operator": "lt"},
                {"left_value": "a", "operator": "lt", "right_value": "b", "right_constant": 1},
                {"left_value": "a", "operator": "lt", "right_value": "b"},
            ],
        }
    )

    assert len(payload["conditions"]) == 2
    assert [item["left_value"] for item in payload["conditions"]] == ["indicator_one.wash", "a"]
    assert any("无法求值" in note for note in notes)


def test_normalize_clamps_minimum_matches_to_the_condition_count() -> None:
    payload, notes = normalize_payload(
        {
            **_PAYLOAD,
            "conditions": [
                {"left_value": "a", "operator": "lt", "right_constant": 1},
                {"left_value": "b", "operator": "lt", "right_constant": 1},
            ],
            "combine_mode": "at_least",
            "minimum_matches": 99,
        }
    )

    assert payload["minimum_matches"] == 2
    assert any("至少满足数量" in note for note in notes)


def test_normalize_clamps_workers() -> None:
    payload, _ = normalize_payload({**_PAYLOAD, "workers": 999})
    assert payload["workers"] == 8

    payload, _ = normalize_payload({**_PAYLOAD, "workers": 0})
    assert payload["workers"] == 1


def test_normalize_keeps_only_known_instrument_types_and_boards() -> None:
    payload, _ = normalize_payload(
        {**_PAYLOAD, "instrument_types": ["stock", "etf"], "boards": ["main", "bse"]}
    )

    assert payload["instrument_types"] == ["stock"]
    assert payload["boards"] == ["main"]


def test_normalize_truncates_an_oversized_payload() -> None:
    """信号 + 公式都顶到各自的条数/长度上限后仍然超限时才走截断。"""

    payload, notes = normalize_payload(
        {
            **_PAYLOAD,
            "selected_signals": [f"signal-{index}-{'x' * 100}" for index in range(MAX_SIGNALS)],
            # 中文一个字三字节，8000 字就是 24 KB。
            "formula_text": "值" * 8_000,
        }
    )

    assert len(str(payload["formula_text"])) < 8_000
    assert payload["formula_parameters"] == {}
    assert any("过大" in note for note in notes)


def test_normalize_falls_back_to_defaults_for_a_non_mapping() -> None:
    payload, notes = normalize_payload("not a payload")

    assert payload["mode"] == "preset"
    assert payload["conditions"] == []
    assert notes


def test_normalize_caps_signal_count() -> None:
    signals = [f"signal-{index}" for index in range(MAX_SIGNALS + 10)]
    payload, notes = normalize_payload({**_PAYLOAD, "selected_signals": signals})

    assert len(payload["selected_signals"]) == MAX_SIGNALS
    assert any("信号数量" in note for note in notes)


def test_normalize_caps_condition_count() -> None:
    conditions = [
        {"left_value": f"value-{index}", "operator": "lt", "right_constant": 1}
        for index in range(MAX_CONDITIONS + 5)
    ]
    payload, _ = normalize_payload({**_PAYLOAD, "conditions": conditions})

    assert len(payload["conditions"]) == MAX_CONDITIONS


def test_normalize_drops_non_finite_parameters() -> None:
    payload, _ = normalize_payload(
        {
            **_PAYLOAD,
            "formula_parameters": {"n": 5, "bad": float("nan"), "text": "x", "flag": True},
        }
    )

    assert payload["formula_parameters"] == {"n": 5.0}


# ----------------------------------------------------------------------
# service
# ----------------------------------------------------------------------


def test_preset_round_trip_keeps_the_payload(tmp_path) -> None:
    service = _service(tmp_path)

    created = service.create_preset("主力进场低位", _PAYLOAD, note="沪深主板")
    record = service.read_preset(str(created["preset_id"]))

    assert record is not None
    assert record["name"] == "主力进场低位"
    assert record["note"] == "沪深主板"
    # 常数统一存成 float：JSON 里 10 和 10.0 是同一个数，比较时要按归一化后的形态比。
    assert record["payload"]["conditions"] == [
        {
            "left_value": "indicator_one.wash",
            "operator": "lt",
            "right_value": None,
            "right_constant": 10.0,
        }
    ]
    # 列表不带 payload：选方案只需要名字，带着它等于把整份配置反复反序列化。
    assert "payload" not in service.list_presets()[0]


def test_creating_a_preset_with_a_taken_name_is_rejected(tmp_path) -> None:
    service = _service(tmp_path)
    service.create_preset("主力进场低位", _PAYLOAD)

    with pytest.raises(ValueError, match="已存在同名方案"):
        service.create_preset("主力进场低位", _PAYLOAD)


def test_renaming_onto_a_taken_name_is_rejected(tmp_path) -> None:
    service = _service(tmp_path)
    first = service.create_preset("甲", _PAYLOAD)
    service.create_preset("乙", _PAYLOAD)

    with pytest.raises(ValueError, match="已存在同名方案"):
        service.update_preset(str(first["preset_id"]), name="乙")


def test_overwriting_keeps_created_at(tmp_path) -> None:
    service = _service(tmp_path)
    created = service.create_preset("甲", _PAYLOAD)
    created_at = created["created_at"]

    updated = service.update_preset(str(created["preset_id"]), payload={**_PAYLOAD, "workers": 4})

    assert updated is not None
    assert updated["created_at"] == created_at
    assert updated["payload"]["workers"] == 4


def test_updating_a_missing_preset_returns_none(tmp_path) -> None:
    service = _service(tmp_path)

    assert service.update_preset("missing", name="甲") is None
    assert service.delete_preset("missing") is False


def test_deleting_removes_the_preset(tmp_path) -> None:
    service = _service(tmp_path)
    created = service.create_preset("甲", _PAYLOAD)

    assert service.delete_preset(str(created["preset_id"])) is True
    assert service.list_presets() == []


def test_preset_count_is_capped(tmp_path) -> None:
    service = _service(tmp_path)
    for index in range(MAX_PRESETS):
        service.create_preset(f"方案-{index}", _PAYLOAD)

    with pytest.raises(ValueError, match="上限"):
        service.create_preset("再来一个", _PAYLOAD)


def test_empty_name_is_rejected(tmp_path) -> None:
    service = _service(tmp_path)

    with pytest.raises(ValueError, match="方案名称不能为空"):
        service.create_preset("   ", _PAYLOAD)


# ----------------------------------------------------------------------
# 迁移：v15 的库必须现场长出 screen_presets
# ----------------------------------------------------------------------


def test_a_v15_database_grows_the_preset_table(tmp_path) -> None:
    path = tmp_path / "market.duckdb"
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS data_meta ("
            "key VARCHAR PRIMARY KEY, value VARCHAR NOT NULL, updated_at TIMESTAMP NOT NULL)"
        )
        connection.execute(
            "INSERT INTO data_meta (key, value, updated_at) VALUES ('schema_version', '15', ?)",
            ["2026-09-01 09:00:00"],
        )

    store = DuckDbMarketDataStore(path)
    store.save_screen_preset("preset-1", name="甲", payload={"mode": "preset"})

    assert store.read_screen_preset("preset-1") is not None
    assert store.status().schema_version == SCHEMA_VERSION
