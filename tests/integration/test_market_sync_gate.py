"""「本交易日已经下完了就别再下载」的接口测试。

点一次同步就把 5000 多只标的重新问一遍，是这段时间最明显的浪费。判据有两层：交易日历说
今天（或最近一个该收盘的交易日）该有行情了，且这个下载范围被记号为「已经下到了那一天」；
库里还得真的有那一天的已收盘日线。断言钉在「该挡的挡住、不该挡的一只都不挡」——判不出
交易日、范围变了、上一次有失败标的、库里的行情其实没到那天、用户勾了强制重下，都必须照常
发起下载：少下一天行情比多问一次严重得多。

日期一律相对「今天」构造，并把两种判据都盖上记号（收盘发布时点之前判到昨天、之后判到今天），
免得测试依赖跑它的那一刻是几点。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from time import monotonic
from types import SimpleNamespace

import pandas as pd
from fastapi.testclient import TestClient

from aquest.adapters.market_sync import MarketSyncConfig
from aquest.market_data.store import DuckDbMarketDataStore
from aquest.market_data.sync_session import record_synced_session, sync_scope_fingerprint
from aquest.web.app import create_app

_TODAY = date.today()
_PREVIOUS = _TODAY - timedelta(days=1)
_JOB_TIMEOUT_SECONDS = 20.0


def _bars(code: str, days: list[date]) -> pd.DataFrame:
    close = [10.0 + index * 0.1 for index in range(len(days))]
    return pd.DataFrame(
        {
            "market": "SH",
            "code": code,
            "instrument_type": "stock",
            "date": pd.to_datetime([item.isoformat() for item in days]),
            "open": close,
            "high": [value + 0.2 for value in close],
            "low": [value - 0.2 for value in close],
            "close": close,
            "volume": [1_000.0] * len(days),
            "amount": [value * 1_000 for value in close],
            "price_basis": "raw",
            "bar_status": ["completed"] * len(days),
        }
    )


class _FakePrecompute:
    """同步后的指标补算不是这里要验的东西，但路由收尾一定会调它。"""

    def run(self, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(to_dict=lambda: {"status": "ok"})


class _FakeSyncService:
    """闸门之外没有被测对象：下载只记录「确实被放行了」。"""

    def __init__(self) -> None:
        self.configs: list[MarketSyncConfig] = []

    def sync(
        self,
        config: MarketSyncConfig,
        progress_callback: object = None,
        *,
        detail_callback: object = None,
        abort_check: object = None,
    ) -> dict[str, object]:
        del progress_callback, detail_callback, abort_check
        self.configs.append(config)
        return {
            "source": "online",
            "processed": 1,
            "updated_files": 1,
            "unchanged_files": 0,
            "written_bars": 1,
            "errors": 0,
            "failure_reasons": {},
        }


@contextmanager
def _gate_client(
    tmp_path: Path,
    *,
    stored_through: date = _TODAY,
    calendar_through: date | None = _TODAY,
) -> Iterator[tuple[TestClient, DuckDbMarketDataStore, _FakeSyncService]]:
    """库里下到 ``stored_through``；日历说到 ``calendar_through`` 才是交易日。

    两者故意分开：``calendar_through=_TODAY`` 而 ``stored_through`` 更早，就是「日历说
    今天该有行情、库里却没有」——这正是必须放行下载的那种情况。日历覆盖声明只做到
    ``calendar_through``，所以传更早的日期等于让判据失效（判不出今天是不是交易日）。
    """

    store = DuckDbMarketDataStore(tmp_path / "gate.duckdb")
    days: list[date] = []
    cursor = _TODAY - timedelta(days=6)
    while cursor <= stored_through:
        days.append(cursor)
        cursor += timedelta(days=1)
    store.upsert_online_bars(_bars("600000", days))
    if calendar_through is not None:
        sessions = []
        cursor = _TODAY - timedelta(days=6)
        while cursor <= calendar_through:
            sessions.append(cursor)
            cursor += timedelta(days=1)
        store.save_market_trading_calendar(
            sessions,
            source="eastmoney_sse_composite",
            coverage_start=_TODAY - timedelta(days=120),
            coverage_end=calendar_through,
        )
    service = _FakeSyncService()
    app = create_app(
        market_data_store=store,
        market_sync=service,
        indicator_precompute_service=_FakePrecompute(),
        auto_sync_enabled=False,
    )
    with TestClient(app) as client:
        yield client, store, service


def _stamp(
    store: DuckDbMarketDataStore,
    session: date,
    *,
    failed: int = 0,
    universe: str = "all",
    types: tuple[str, ...] | None = None,
    boards: tuple[str, ...] | None = None,
) -> None:
    """记号盖在「这次请求会发出去的那个范围」上：字段没给就是不限制。"""

    record_synced_session(
        store,
        session=session,
        scope=sync_scope_fingerprint(
            universe=universe,
            provider="eastmoney",
            instrument_types=types,
            boards=boards,
        ),
        instruments=1,
        failed=failed,
        provider="eastmoney",
    )


def _settle(client: TestClient, job_id: str) -> None:
    """等这一轮任务跑到终态：闸门之后还有「已有行情任务在跑」那道 409 在等着。"""

    deadline = monotonic() + _JOB_TIMEOUT_SECONDS
    while monotonic() < deadline:
        state = client.get(f"/api/v1/market-data/jobs/{job_id}").json()["data"]
        if state["status"] in {"completed", "failed", "cancelled"}:
            return
    raise AssertionError("行情任务没有在超时前结束")


def test_sync_is_skipped_when_the_session_is_already_downloaded(tmp_path: Path) -> None:
    with _gate_client(tmp_path) as (client, store, service):
        # 收盘发布时点之前判到昨天、之后判到今天，两种都要能挡住。
        # 记号按范围只留一份、后写的生效：先写昨天再写今天，发布时点前后都够用。
        _stamp(store, _PREVIOUS)
        _stamp(store, _TODAY)

        response = client.post("/api/v1/market-data/sync-online", json={"universe": "all"})

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["skipped"] is True
        assert data["job_id"] is None
        assert data["session"] in {_TODAY.isoformat(), _PREVIOUS.isoformat()}
        assert f"本地已覆盖 {data['session']} 收盘" in data["message"]
        # 闸门生效的判据就是「什么都没发生」：没建任务，也没调用在线通道。
        assert service.configs == []


def test_force_flag_bypasses_the_gate(tmp_path: Path) -> None:
    """除权修正、补上次失败的洞都靠这个开关绕过判定，默认那一次仍然什么都不做。"""

    with _gate_client(tmp_path) as (client, store, service):
        # 记号按范围只留一份、后写的生效：先写昨天再写今天，发布时点前后都够用。
        _stamp(store, _PREVIOUS)
        _stamp(store, _TODAY)

        response = client.post("/api/v1/market-data/sync-online", json={"force": True})

        assert response.status_code == 202
        assert response.json()["data"]["job_id"]
        assert len(service.configs) == 1


def test_gate_downloads_when_the_calendar_cannot_answer(tmp_path: Path) -> None:
    """没有日历、或覆盖声明没做到今天：判不出今天是不是交易日，就必须照旧下载。"""

    for label, calendar_through in (("none", None), ("stale", _PREVIOUS)):
        with _gate_client(tmp_path / label, calendar_through=calendar_through) as (
            client,
            store,
            service,
        ):
            _stamp(store, _TODAY)
            _stamp(store, _PREVIOUS)

            assert client.post("/api/v1/market-data/sync-online", json={}).status_code == 202, label
            assert len(service.configs) == 1


def test_gate_downloads_for_a_scope_that_was_never_synced(tmp_path: Path) -> None:
    """记号按范围分开：全市场下完了，不代表「只下深圳」或「只下主板」也可以跳过。"""

    with _gate_client(tmp_path) as (client, store, service):
        # 记号按范围只留一份、后写的生效：先写昨天再写今天，发布时点前后都够用。
        _stamp(store, _PREVIOUS)
        _stamp(store, _TODAY)

        shenzhen = client.post("/api/v1/market-data/sync-online", json={"universe": "sz"})
        assert shenzhen.status_code == 202
        _settle(client, shenzhen.json()["data"]["job_id"])

        main_board = client.post(
            "/api/v1/market-data/sync-online",
            json={"universe": "all", "boards": ["main"]},
        )
        assert main_board.status_code == 202
        _settle(client, main_board.json()["data"]["job_id"])
        assert len(service.configs) == 2

        # 全市场这个范围早先就盖过章，第二次点仍然被挡掉。
        assert client.post("/api/v1/market-data/sync-online", json={}).status_code == 200
        assert len(service.configs) == 2


def test_gate_downloads_when_the_last_run_had_failures(tmp_path: Path) -> None:
    """上一次有标的失败过就不算下完了：漏掉的那几只正是用户重点一次想补的东西。"""

    with _gate_client(tmp_path) as (client, store, service):
        _stamp(store, _PREVIOUS, failed=3)
        _stamp(store, _TODAY, failed=3)

        assert client.post("/api/v1/market-data/sync-online", json={}).status_code == 202
        assert len(service.configs) == 1


def test_gate_downloads_when_the_store_lost_the_session(tmp_path: Path) -> None:
    """库里的行情没到判据那一天时，记号再新也拦不住——数据被动过是真实发生过的情况。"""

    with _gate_client(tmp_path, stored_through=_TODAY - timedelta(days=2)) as (
        client,
        store,
        service,
    ):
        # 记号按范围只留一份、后写的生效：先写昨天再写今天，发布时点前后都够用。
        _stamp(store, _PREVIOUS)
        _stamp(store, _TODAY)

        assert client.post("/api/v1/market-data/sync-online", json={}).status_code == 202
        assert len(service.configs) == 1


def test_gate_downloads_when_only_one_symbol_is_behind(tmp_path: Path) -> None:
    """范围下过章但有一只标的当天没数据（停牌后复牌、新入库）：闸门不该替它做主。

    记号里同时存着当时的库里标的数，标的数一变就放行；这里断言的是「放行」，而不是它
    具体拉了哪一段——区间由下载计划里的逐标的缺口决定。
    """

    with _gate_client(tmp_path) as (client, store, service):
        # 记号按范围只留一份、后写的生效：先写昨天再写今天，发布时点前后都够用。
        _stamp(store, _PREVIOUS)
        _stamp(store, _TODAY)
        # 代码必须是上海的合法号段：行情库按市场+代码判板块，认不出就整批拒收。
        store.upsert_online_bars(_bars("600519", [_TODAY - timedelta(days=2)]))

        assert client.post("/api/v1/market-data/sync-online", json={}).status_code == 202
        assert len(service.configs) == 1
