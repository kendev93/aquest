"""策略追踪接口：两个入口的状态守卫。

「停止」必须真的停住，「已上线」不能被重复上线覆盖掉冻结的研究段。
这两条 UI 上都点不出来（按钮按状态切换），但接口层漏了就是漏了——
脚本、误操作、以及以后接自动化时都会踩到。

手动入口和调度器还要守同两条并发纪律：行情同步期间不重算（拿的是旧行情），
同一条策略不并发重算（会同时写同一批追踪点）。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from threading import Event

from fastapi.testclient import TestClient

from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.portfolio_backtest.auto_tracking import (
    TRACKING_DESCRIPTION,
    TRACKING_EXCLUSIVE_GROUP,
)
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


def test_the_list_keeps_trades_out_but_the_detail_brings_them(tmp_path: Path) -> None:
    """明细几百笔起步，列表接口带上它会被策略数放大；只在展开时单独取。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    with TestClient(create_app(market_data_store=store)) as client:
        strategy_id = _save_strategy(client)

        listed = client.get("/api/v1/strategies/tracking").json()["data"]
        detail = client.get(f"/api/v1/strategies/tracking/{strategy_id}").json()["data"]

        assert listed[0]["trades"] is None
        assert "next_plan" in listed[0]
        # 详情里两段都在，哪怕现在一笔成交都没有——结构要对，不能缺字段。
        assert {"before", "after", "limit"} <= set(detail["trades"])


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


def _strategy_online(client: TestClient, store: DuckDbMarketDataStore) -> str:
    strategy_id = _save_strategy(client)
    store.start_strategy_tracking(strategy_id, launched_at=20260302)
    return strategy_id


def test_rerun_is_rejected_while_market_data_is_syncing(tmp_path: Path) -> None:
    """行情同步期间重算，用的还是同步前的行情——算出来是份过时的结果。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    app = create_app(market_data_store=store)
    runner = app.state.screen_job_runner
    release = Event()

    def task(_progress):
        # 409 的前提是「行情任务还在跑」，它自己结束就验不到冲突了。
        release.wait(timeout=30)
        return {}

    runner.submit_callable(task, description="行情同步")
    try:
        with TestClient(app) as client:
            strategy_id = _strategy_online(client, store)

            response = client.post(f"/api/v1/strategies/tracking/{strategy_id}/rerun")

            # 必须在 with 退出前放人：lifespan 关闭会 join 执行器。
            release.set()

        assert response.status_code == 409
        assert "行情" in response.json()["error"]["message"]
    finally:
        release.set()
        runner.shutdown()


def test_rerun_is_rejected_while_another_tracking_job_runs(tmp_path: Path) -> None:
    """两个重算并发跑，会同时写 saved_strategies 与 tracking_points 的同一批行。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    app = create_app(market_data_store=store)
    runner = app.state.screen_job_runner
    release = Event()

    def task(_progress):
        release.wait(timeout=30)
        return {}

    runner.submit_callable(
        task,
        description=TRACKING_DESCRIPTION,
        exclusive_group=TRACKING_EXCLUSIVE_GROUP,
    )
    try:
        with TestClient(app) as client:
            strategy_id = _strategy_online(client, store)

            response = client.post(f"/api/v1/strategies/tracking/{strategy_id}/rerun")

            # 同上：这个任务也得在 lifespan 关闭前放人。
            release.set()

        assert response.status_code == 409
        assert "已有策略追踪任务" in response.json()["error"]["message"]
    finally:
        release.set()
        runner.shutdown()
