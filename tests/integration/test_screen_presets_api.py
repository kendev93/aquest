"""筛选方案端点：保存 / 加载 / 另存为 / 重命名 / 覆盖 / 删除。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from aquest.market_data.store import DuckDbMarketDataStore
from aquest.screen_presets.service import ScreenPresetService
from aquest.web.app import create_app

_PAYLOAD = {
    "mode": "preset",
    "rule_mode": "conditions",
    "selected_signals": ["indicator_one.main_force_entry"],
    "conditions": [
        {"left_value": "indicator_one.wash", "operator": "lt", "right_constant": 10},
    ],
    "combine_mode": "all",
    "minimum_matches": None,
    "universe": "sh",
    "universe_file": "",
    "instrument_types": ["stock"],
    "boards": ["main"],
    "workers": 8,
    "period": "daily",
    "formula_text": "",
    "formula_parameters": {},
}


def _client(store: DuckDbMarketDataStore) -> TestClient:
    return TestClient(
        create_app(market_data_store=store, screen_preset_service=ScreenPresetService(store))
    )


def test_list_is_empty_before_anything_is_saved(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")

    response = _client(store).get("/api/v1/screen-presets")

    assert response.status_code == 200
    assert response.json()["data"] == []


def test_create_then_read_returns_the_payload(tmp_path) -> None:
    client = _client(DuckDbMarketDataStore(tmp_path / "market.duckdb"))

    created = client.post(
        "/api/v1/screen-presets", json={"name": "主力进场低位", "payload": _PAYLOAD}
    )
    assert created.status_code == 201
    body = created.json()["data"]
    assert body["name"] == "主力进场低位"
    assert body["payload"]["universe"] == "sh"

    read = client.get(f"/api/v1/screen-presets/{body['preset_id']}")
    assert read.status_code == 200
    assert read.json()["data"]["payload"]["conditions"][0]["left_value"] == "indicator_one.wash"


def test_duplicate_name_is_rejected_with_422(tmp_path) -> None:
    client = _client(DuckDbMarketDataStore(tmp_path / "market.duckdb"))
    client.post("/api/v1/screen-presets", json={"name": "甲", "payload": _PAYLOAD})

    response = client.post("/api/v1/screen-presets", json={"name": "甲", "payload": _PAYLOAD})

    assert response.status_code == 422
    assert "同名" in response.json()["error"]["message"]


def test_invalid_payload_is_clipped_and_reported(tmp_path) -> None:
    client = _client(DuckDbMarketDataStore(tmp_path / "market.duckdb"))

    created = client.post(
        "/api/v1/screen-presets",
        json={
            "name": "带垃圾字段",
            "payload": {**_PAYLOAD, "workers": 999, "results": [1, 2, 3]},
        },
    )

    assert created.status_code == 201
    body = created.json()["data"]
    assert body["payload"]["workers"] == 8
    assert "results" not in body["payload"]
    assert body["notes"]


def test_rename_and_overwrite(tmp_path) -> None:
    client = _client(DuckDbMarketDataStore(tmp_path / "market.duckdb"))
    preset_id = client.post(
        "/api/v1/screen-presets", json={"name": "甲", "payload": _PAYLOAD}
    ).json()["data"]["preset_id"]

    renamed = client.patch(f"/api/v1/screen-presets/{preset_id}", json={"name": "乙"})
    assert renamed.status_code == 200
    assert renamed.json()["data"]["name"] == "乙"

    overwritten = client.patch(
        f"/api/v1/screen-presets/{preset_id}", json={"payload": {**_PAYLOAD, "universe": "sz"}}
    )
    assert overwritten.status_code == 200
    assert overwritten.json()["data"]["payload"]["universe"] == "sz"


def test_updating_a_missing_preset_is_404(tmp_path) -> None:
    client = _client(DuckDbMarketDataStore(tmp_path / "market.duckdb"))

    response = client.patch("/api/v1/screen-presets/missing", json={"name": "甲"})

    assert response.status_code == 404


def test_delete_removes_the_preset(tmp_path) -> None:
    client = _client(DuckDbMarketDataStore(tmp_path / "market.duckdb"))
    preset_id = client.post(
        "/api/v1/screen-presets", json={"name": "甲", "payload": _PAYLOAD}
    ).json()["data"]["preset_id"]

    deleted = client.delete(f"/api/v1/screen-presets/{preset_id}")
    assert deleted.status_code == 200
    assert deleted.json()["data"]["deleted"] is True
    assert client.get("/api/v1/screen-presets").json()["data"] == []


def test_empty_name_is_rejected_with_422(tmp_path) -> None:
    client = _client(DuckDbMarketDataStore(tmp_path / "market.duckdb"))

    response = client.post("/api/v1/screen-presets", json={"name": "  ", "payload": _PAYLOAD})

    assert response.status_code == 422
