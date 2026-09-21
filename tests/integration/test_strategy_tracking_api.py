"""策略追踪接口：两个入口的状态守卫。

「停止」必须真的停住，「已上线」不能被重复上线覆盖掉冻结的研究段。
这两条 UI 上都点不出来（按钮按状态切换），但接口层漏了就是漏了——
脚本、误操作、以及以后接自动化时都会踩到。
"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.web.app import create_app

_REQUEST = {
    "ranking_value": "indicator_one.wash",
    "rank_order": "desc",
    "selected_signals": ["indicator_one.main_force_entry"],
    "max_positions": 5,
    "stop_loss_pct": 0.1,
    "date_mode": "custom",
    "start_date": "2026-02-02",
    "end_date": "2026-03-02",
}


def _save_strategy(client: TestClient) -> str:
    response = client.post(
        "/api/v1/strategies",
        json={
            "name": f"追踪-{uuid.uuid4().hex[:8]}",
            "request": _REQUEST,
            "summary": {},
        },
    )
    assert response.status_code == 201
    return str(response.json()["data"]["strategy_id"])


def test_rerun_is_rejected_while_the_strategy_is_offline(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    with TestClient(create_app(market_data_store=store)) as client:
        strategy_id = _save_strategy(client)

        response = client.post(f"/api/v1/strategies/tracking/{strategy_id}/rerun")

        assert response.status_code == 409
        assert "未上线" in response.json()["error"]["message"]


def test_stopped_strategy_cannot_be_rerun_either(tmp_path: Path) -> None:
    """停止之后再手动重算，等于「停止」这个动作形同虚设。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    with TestClient(create_app(market_data_store=store)) as client:
        strategy_id = _save_strategy(client)
        store.start_strategy_tracking(strategy_id, launched_at=20260302)
        store.stop_strategy_tracking(strategy_id)
        assert store.read_strategy(strategy_id) is not None

        response = client.post(f"/api/v1/strategies/tracking/{strategy_id}/rerun")

        assert response.status_code == 409


def test_launch_is_rejected_while_the_strategy_is_already_online(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    with TestClient(create_app(market_data_store=store)) as client:
        strategy_id = _save_strategy(client)
        store.start_strategy_tracking(strategy_id, launched_at=20260302)

        response = client.post(f"/api/v1/strategies/tracking/{strategy_id}/launch")

        assert response.status_code == 409
        assert "已上线" in response.json()["error"]["message"]


def test_unknown_strategy_still_answers_404(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    with TestClient(create_app(market_data_store=store)) as client:
        assert client.post("/api/v1/strategies/tracking/nope/rerun").status_code == 404
        assert client.post("/api/v1/strategies/tracking/nope/launch").status_code == 404
