"""研究方案端点：保存 / 加载 / 另存为 / 重命名 / 覆盖 / 删除。

payload 的形状就是跑研究的请求本身，所以「存得进去却跑不起来」这条路必须堵死：
非法请求一律 422，不做裁剪。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.research_presets.service import ResearchPresetService
from selector_app.web.app import create_app

_PAYLOAD = {
    "universe": "all",
    "instrument_types": ["stock"],
    "buy_rule": {"signals": ["indicator_three.begin_zone"]},
    "sell_rule": None,
    "date_mode": "custom",
    "start_date": "2020-01-01",
    "end_date": "2024-12-31",
}


def _client(store: DuckDbMarketDataStore) -> TestClient:
    return TestClient(
        create_app(market_data_store=store, research_preset_service=ResearchPresetService(store))
    )


def test_list_is_empty_before_anything_is_saved(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")

    response = _client(store).get("/api/v1/research-presets")

    assert response.status_code == 200
    assert response.json()["data"] == []


def test_create_then_read_returns_the_payload(tmp_path) -> None:
    client = _client(DuckDbMarketDataStore(tmp_path / "market.duckdb"))

    created = client.post(
        "/api/v1/research-presets", json={"name": "低位进场", "payload": _PAYLOAD}
    )
    assert created.status_code == 201
    body = created.json()["data"]
    assert body["name"] == "低位进场"

    read = client.get(f"/api/v1/research-presets/{body['preset_id']}")
    assert read.status_code == 200
    assert read.json()["data"]["payload"]["sell_rule"] is None


def test_duplicate_name_is_rejected_with_422(tmp_path) -> None:
    client = _client(DuckDbMarketDataStore(tmp_path / "market.duckdb"))
    client.post("/api/v1/research-presets", json={"name": "甲", "payload": _PAYLOAD})

    response = client.post("/api/v1/research-presets", json={"name": "甲", "payload": _PAYLOAD})

    assert response.status_code == 422
    assert "同名" in response.json()["error"]["message"]


def test_unknown_fields_are_rejected_rather_than_clipped(tmp_path) -> None:
    """研究请求的字段互相咬合，裁剪会把整套规则的语义改掉，所以只能拒。"""

    client = _client(DuckDbMarketDataStore(tmp_path / "market.duckdb"))

    response = client.post(
        "/api/v1/research-presets",
        json={"name": "带垃圾字段", "payload": {**_PAYLOAD, "results": [1, 2, 3]}},
    )

    assert response.status_code == 422


def test_payload_that_cannot_run_is_rejected(tmp_path) -> None:
    """买卖两侧同时留空的研究请求跑不起来，方案里也不许存。"""

    client = _client(DuckDbMarketDataStore(tmp_path / "market.duckdb"))

    response = client.post(
        "/api/v1/research-presets",
        json={"name": "两侧皆空", "payload": {**_PAYLOAD, "buy_rule": None}},
    )

    assert response.status_code == 422


def test_rename_and_overwrite(tmp_path) -> None:
    client = _client(DuckDbMarketDataStore(tmp_path / "market.duckdb"))
    preset_id = client.post(
        "/api/v1/research-presets", json={"name": "甲", "payload": _PAYLOAD}
    ).json()["data"]["preset_id"]

    renamed = client.patch(f"/api/v1/research-presets/{preset_id}", json={"name": "乙"})
    assert renamed.status_code == 200
    assert renamed.json()["data"]["name"] == "乙"

    overwritten = client.patch(
        f"/api/v1/research-presets/{preset_id}",
        json={"payload": {**_PAYLOAD, "universe": "sz"}},
    )
    assert overwritten.status_code == 200
    assert overwritten.json()["data"]["payload"]["universe"] == "sz"


def test_updating_a_missing_preset_is_404(tmp_path) -> None:
    client = _client(DuckDbMarketDataStore(tmp_path / "market.duckdb"))

    response = client.patch("/api/v1/research-presets/missing", json={"name": "甲"})

    assert response.status_code == 404


def test_delete_removes_the_preset(tmp_path) -> None:
    client = _client(DuckDbMarketDataStore(tmp_path / "market.duckdb"))
    preset_id = client.post(
        "/api/v1/research-presets", json={"name": "甲", "payload": _PAYLOAD}
    ).json()["data"]["preset_id"]

    deleted = client.delete(f"/api/v1/research-presets/{preset_id}")
    assert deleted.status_code == 200
    assert deleted.json()["data"]["deleted"] is True
    assert client.get("/api/v1/research-presets").json()["data"] == []


def test_empty_name_is_rejected_with_422(tmp_path) -> None:
    client = _client(DuckDbMarketDataStore(tmp_path / "market.duckdb"))

    response = client.post("/api/v1/research-presets", json={"name": "  ", "payload": _PAYLOAD})

    assert response.status_code == 422
