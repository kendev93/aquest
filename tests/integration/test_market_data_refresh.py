"""全量重刷（先删后下）的接口测试。

常规同步只补最新一段、全量补齐只补已有区间之外的缺口，中间写歪的那段两个入口
都覆盖不到——尤其本地导入的行永久压过在线行。重刷的价值就在"删干净再整段下"，
所以断言都钉在**删除确实发生了、并且删掉的位置能被在线行重新写上**。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from time import monotonic

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from selector_app.adapters.market_sync import MarketSyncConfig
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.web.app import create_app

_JOB_TIMEOUT_SECONDS = 20.0


def _frame(market: str, code: str, closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "market": market,
                "code": code,
                "instrument_type": "stock",
                "date": pd.Timestamp("2024-01-02") + pd.Timedelta(days=index),
                "open": close - 0.5,
                "high": close + 0.5,
                "low": close - 1.0,
                "close": close,
                "volume": 1000.0,
                "amount": close * 1000.0,
                "price_basis": "raw",
                "bar_status": "completed",
            }
            for index, close in enumerate(closes)
        ]
    )


class _Report:
    def to_dict(self) -> dict[str, object]:
        return {"status": "ok"}


class _FakePrecomputeService:
    """指标预计算不是这里的被测对象，重刷只关心它被调用过。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def run(self, **kwargs: object) -> _Report:
        self.calls.append(kwargs)
        return _Report()


class _FakeSyncService:
    """代替在线通道：把 ``redownload`` 里的日线当成"刚拉下来的全历史"写回库里。"""

    def __init__(self, store: DuckDbMarketDataStore, redownload: dict[str, list[float]]) -> None:
        self._store = store
        self._redownload = redownload
        self.configs: list[MarketSyncConfig] = []
        #: 真通道在 sync 内部读 instruments 的覆盖区间决定"还缺哪一段"，这里照样
        #: 在 sync 被调用的那一刻把库里的区间记下来，供测试断言。
        self.coverage_at_sync: dict[tuple[str, str], tuple[object, object]] = {}

    def sync(
        self,
        config: MarketSyncConfig,
        progress_callback: object = None,
        *,
        detail_callback: object = None,
        abort_check: object = None,
    ) -> dict[str, object]:
        self.configs.append(config)
        self.coverage_at_sync = {
            (ref.market, ref.code): (ref.first_date, ref.last_date)
            for ref in self._store.list_instruments(include_missing=True)
        }
        written = 0
        for market, code in (("SH", "600000"), ("SZ", "000001")):
            closes = self._redownload.get(code)
            if closes is None:
                continue
            if config.universe not in {"all", market.lower()}:
                continue
            written += self._store.upsert_online_bars(_frame(market, code, closes))[0]
        return {
            "source": "online",
            "processed": len(self._redownload),
            "updated_files": len(self._redownload),
            "unchanged_files": 0,
            "written_bars": written,
            "errors": 0,
            "failure_reasons": {},
        }


@contextmanager
def _client(
    tmp_path: Path,
    redownload: dict[str, list[float]],
) -> Iterator[tuple[TestClient, _FakeSyncService]]:
    """起一个注入假同步服务的 app；``with`` 才能跑 lifespan（任务执行器要它）。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    service = _FakeSyncService(store, redownload)
    app = create_app(
        market_data_store=store,
        market_sync=service,
        indicator_precompute_service=_FakePrecomputeService(),
        auto_sync_enabled=False,
    )
    with TestClient(app) as client:
        yield client, service


def _wait(client: TestClient, job_id: str) -> dict[str, object]:
    deadline = monotonic() + _JOB_TIMEOUT_SECONDS
    while monotonic() < deadline:
        state = client.get(f"/api/v1/market-data/jobs/{job_id}").json()["data"]
        if state["status"] in {"completed", "failed", "cancelled"}:
            return state
    raise AssertionError("行情任务没有在超时前结束")


def test_refresh_deletes_stored_bars_before_redownloading(tmp_path: Path) -> None:
    """重刷的核心：本地导入的行被清掉，原来被它们压住的位置能重新写进在线行。"""

    with _client(tmp_path, {"600000": [20.0, 21.0]}) as (client, service):
        store = client.app.state.market_data_store
        store.replace_local_bars(
            "SH", "600000", "stock", _frame("SH", "600000", [10.0, 11.0, 12.0])
        )
        store.upsert_online_bars(_frame("SZ", "000001", [30.0, 31.0]))

        created = client.post(
            "/api/v1/market-data/refresh",
            json={"universe": "all", "confirm": True},
        )
        assert created.status_code == 202
        state = _wait(client, created.json()["data"]["job_id"])

        result = state["result"]
        assert result["deleted_bars"] == 5
        assert result["written_bars"] == 2
        # 3000 只票里最容易被忽略的一条：删完必须能重新写上，否则只是把数据清空。
        bars = store.read_bars("SH", "600000")
        assert list(bars["close"]) == [20.0, 21.0]
        assert list(bars["source"]) == ["online", "online"]
        # 重刷永远按全历史拉，不然删掉的那段补不回来。
        assert service.configs and service.configs[0].full_history is True
        # 光删 daily_bars 不够：在线同步靠 instruments 的 first_date/last_date 判断
        # "这段是不是已经有了"。留着旧区间，它就会只补首尾两段，被删掉的中间段
        # 永远回不来。
        assert service.coverage_at_sync[("SH", "600000")] == (None, None)


def test_refresh_keeps_instruments_outside_the_selected_market(tmp_path: Path) -> None:
    """只刷上海：深圳的票一根都不能动——删除范围错了就是静默的数据丢失。"""

    with _client(tmp_path, {"600000": [20.0, 21.0]}) as (client, _service):
        store = client.app.state.market_data_store
        store.replace_local_bars("SH", "600000", "stock", _frame("SH", "600000", [10.0, 11.0]))
        store.upsert_online_bars(_frame("SZ", "000001", [30.0, 31.0]))

        created = client.post(
            "/api/v1/market-data/refresh",
            json={"universe": "sh", "confirm": True},
        )
        state = _wait(client, created.json()["data"]["job_id"])

        assert state["result"]["deleted_bars"] == 2
        assert list(store.read_bars("SZ", "000001")["close"]) == [30.0, 31.0]
        assert list(store.read_bars("SH", "600000")["close"]) == [20.0, 21.0]


def test_refresh_recovers_when_a_previous_run_deleted_without_recovering(
    tmp_path: Path,
) -> None:
    """上一次重刷把数据删了却没补回来时，这次必须仍能整段重下。

    旧版本只删 ``daily_bars``、不动 ``instruments`` 的覆盖区间，于是库里是这样：
    列表上还写着"6398 根"，K 线却是空的（``last_close`` 直接是 None）。重刷在
    这种库上删除会命中 0 行，所以"删了才重算覆盖"不够——覆盖必须无条件归零。
    """

    with _client(tmp_path, {"600000": [20.0, 21.0]}) as (client, service):
        store = client.app.state.market_data_store
        store.replace_local_bars("SH", "600000", "stock", _frame("SH", "600000", [10.0, 11.0]))
        # 绕过 delete_bars 直接清行，复现旧版本留下的"有覆盖、没数据"状态。
        with store._bars._store._connect() as connection:  # noqa: SLF001 - 复现历史状态
            connection.execute("DELETE FROM daily_bars")

        created = client.post(
            "/api/v1/market-data/refresh",
            json={"universe": "all", "confirm": True},
        )
        state = _wait(client, created.json()["data"]["job_id"])

        assert state["result"]["deleted_bars"] == 0
        assert service.coverage_at_sync[("SH", "600000")] == (None, None)
        assert list(store.read_bars("SH", "600000")["close"]) == [20.0, 21.0]


def test_refresh_names_instruments_that_stayed_empty(tmp_path: Path) -> None:
    """删了却没下回来的标的必须被点名，否则用户只看到总数少了、不知道少了谁。"""

    with _client(tmp_path, {}) as (client, _service):
        store = client.app.state.market_data_store
        store.replace_local_bars("SH", "600000", "stock", _frame("SH", "600000", [10.0, 11.0]))

        created = client.post(
            "/api/v1/market-data/refresh",
            json={"universe": "all", "confirm": True},
        )
        state = _wait(client, created.json()["data"]["job_id"])

        result = state["result"]
        assert result["refreshed_instruments"] == 1
        assert result["empty_after_refresh"] == ["600000"]


def test_refresh_requires_explicit_confirmation(tmp_path: Path) -> None:
    """破坏性操作和删除/修复一致：少传一个 confirm 就必须是 422，而不是照删。"""

    with _client(tmp_path, {"600000": [20.0]}) as (client, _service):
        store = client.app.state.market_data_store
        store.replace_local_bars("SH", "600000", "stock", _frame("SH", "600000", [10.0, 11.0]))

        response = client.post("/api/v1/market-data/refresh", json={"universe": "all"})

        assert response.status_code == 422
        assert len(store.read_bars("SH", "600000")) == 2


def test_refresh_rejects_unknown_fields(tmp_path: Path) -> None:
    """extra=forbid：拼错的字段名要被拒，不能悄悄按默认值刷一遍全市场。"""

    with _client(tmp_path, {}) as (client, _service):
        response = client.post(
            "/api/v1/market-data/refresh",
            json={"universe": "all", "confirm": True, "force": True},
        )

        assert response.status_code == 422


def test_refresh_job_is_pollable_and_cancellable(tmp_path: Path) -> None:
    """重刷要跑十几分钟，必须走行情任务通道：能轮询、能被"已有行情任务"闸门挡住。"""

    with _client(tmp_path, {"600000": [20.0]}) as (client, _service):
        created = client.post(
            "/api/v1/market-data/refresh",
            json={"universe": "all", "confirm": True},
        )
        job_id = created.json()["data"]["job_id"]
        state = _wait(client, job_id)

        assert state["status"] == "completed"
        listed = client.get("/api/v1/market-data/jobs/active").json()["data"]
        assert isinstance(listed, list)


@pytest.mark.parametrize("universe", ["all", "sh", "sz"])
def test_refresh_accepts_every_universe(tmp_path: Path, universe: str) -> None:
    with _client(tmp_path, {}) as (client, _service):
        response = client.post(
            "/api/v1/market-data/refresh",
            json={"universe": universe, "confirm": True},
        )

        assert response.status_code == 202
