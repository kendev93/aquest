from __future__ import annotations

import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi.testclient import TestClient

from aquest.adapters.market_sync import MarketSyncConfig, MarketSyncReport
from aquest.indicators.cache import IndicatorCache
from aquest.market_data.service import (
    LocalInstrument,
    LocalInstrumentPage,
    LocalMarketChart,
)
from aquest.market_data.store import DuckDbMarketDataStore
from aquest.web.app import create_app


def _bars(length: int = 80, base: float = 1.0) -> pd.DataFrame:
    close = base + np.arange(length, dtype=float)
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=length, freq="D"),
            "open": close - 0.2,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.full(length, 100_000.0),
            "amount": close * 100_000.0,
            "bar_status": "completed",
        }
    )


class FakeLocalMarketDataService:
    def list_instruments(self, vipdoc_path, *, market, keyword, page, page_size):
        assert vipdoc_path == "/data/vipdoc"
        assert market == "SH"
        assert keyword == "600"
        assert page == 1
        assert page_size == 20
        return LocalInstrumentPage(
            items=(
                LocalInstrument(
                    market="SH",
                    code="600000",
                    bars=300,
                    data_start="2020-01-01",
                    data_end="2024-12-31",
                    last_close=12.5,
                ),
            ),
            total=1,
            page=1,
            page_size=20,
            pages=1,
        )

    def get_chart(self, vipdoc_path, market, code, *, period, start_date, end_date):
        assert vipdoc_path == "/data/vipdoc"
        assert market == "SH"
        assert code == "600000"
        assert period == "monthly"
        assert start_date == date(2024, 1, 1)
        assert end_date == date(2024, 12, 31)
        return LocalMarketChart(
            market="SH",
            code="600000",
            period="monthly",
            total_daily_bars=300,
            bars=12,
            available_data_start="2020-01-01",
            available_data_end="2024-12-31",
            data_start="2024-01-31",
            data_end="2024-12-31",
            candles=(
                {
                    "date": "2024-01-31",
                    "open": 10.0,
                    "high": 12.0,
                    "low": 9.0,
                    "close": 11.0,
                    "volume": 1000.0,
                    "amount": 11000.0,
                    "ma": {"ma5": None, "ma10": None, "ma20": None, "ma60": None},
                    "rsi14": None,
                    "macd": None,
                    "macd_signal": None,
                    "macd_histogram": None,
                },
            ),
        )


def test_local_instrument_list_and_chart_endpoints() -> None:
    app = create_app(local_market_data_service=FakeLocalMarketDataService())
    with TestClient(app) as client:
        instruments = client.get(
            "/api/v1/market-data/local/instruments",
            params={
                "vipdoc_path": "/data/vipdoc",
                "market": "SH",
                "keyword": "600",
                "page_size": 20,
            },
        )
        chart = client.get(
            "/api/v1/market-data/local/SH/600000/bars",
            params={
                "vipdoc_path": "/data/vipdoc",
                "period": "monthly",
                "start_date": "2024-01-01",
                "end_date": "2024-12-31",
            },
        )

    assert instruments.status_code == 200
    assert instruments.json()["data"][0]["code"] == "600000"
    assert instruments.json()["meta"] == {
        "total": 1,
        "page": 1,
        "page_size": 20,
        "pages": 1,
    }
    assert chart.status_code == 200
    assert chart.json()["data"]["period"] == "monthly"
    assert chart.json()["data"]["candles"][0]["ma"]["ma5"] is None


def test_local_market_bars_rejects_invalid_code() -> None:
    app = create_app(local_market_data_service=FakeLocalMarketDataService())
    with TestClient(app) as client:
        response = client.get("/api/v1/market-data/local/SH/not-a-code/bars")

    assert response.status_code == 422


def test_local_instrument_endpoint_searches_chinese_stock_names(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars(40, base=10.0), name="浦发银行")
    app = create_app(market_data_store=store, auto_sync_enabled=False)

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/market-data/local/instruments",
            params={"keyword": "浦发"},
        )

    assert response.status_code == 200, response.text
    assert response.json()["meta"]["total"] == 1
    assert response.json()["data"][0]["code"] == "600000"
    assert response.json()["data"][0]["name"] == "浦发银行"


def test_bar_delete_endpoint_rejects_an_unconfirmed_request() -> None:
    """删除日线不可撤销，没有 confirm 的请求必须被挡下。"""

    with TestClient(create_app(auto_sync_enabled=False)) as client:
        response = client.post(
            "/api/v1/market-data/bars/delete",
            json={"source": "local", "universe": "all"},
        )

    assert response.status_code == 422


def test_bar_delete_drops_the_in_process_indicator_cache(tmp_path: Path) -> None:
    """删掉行情后指标缓存必须跟着失效。

    缓存键里带着标的级版本号，而版本号在进程内是缓存住的。行情一变它必然
    变化，但缓存里的副本不会——于是「日期不变、数值变了」的重写会命中旧
    条目，回测拿到上一版行情算出来的指标，且不报错。
    """

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars())
    cache = IndicatorCache(store)
    cache.get_or_compute(store.read_bars("SH", "600000"), ("price",), market="SH", code="600000")
    assert cache._revisions, "预热之后版本号应当已经缓存住"

    with TestClient(
        create_app(market_data_store=store, indicator_cache=cache, auto_sync_enabled=False)
    ) as client:
        response = client.post(
            "/api/v1/market-data/bars/delete",
            json={"source": "local", "universe": "all", "confirm": True},
        )

    assert response.status_code == 200
    assert response.json()["data"]["deleted_bars"] > 0
    assert cache._revisions == {}
    assert cache.status()["memory_entries"] == 0


def test_local_import_endpoint_is_removed() -> None:
    with TestClient(create_app(auto_sync_enabled=False)) as client:
        response = client.post(
            "/api/v1/market-data/import-local",
            json={"vipdoc_path": "/data/vipdoc", "universe": "all"},
        )

    assert response.status_code == 404


def _wait_for_market_job(client: TestClient, job_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/market-data/jobs/{job_id}")
        assert response.status_code == 200
        state = response.json()["data"]
        if state["status"] in {"completed", "failed", "cancelled"}:
            return state
        time.sleep(0.05)
    raise AssertionError(f"行情任务 {job_id} 没有结束")


class FakeMarketSyncService:
    """记录修复时的同步调用，用来钉住「先删后下」这个顺序。"""

    def __init__(self, store: DuckDbMarketDataStore) -> None:
        self._store = store
        self.configs: list[MarketSyncConfig] = []
        self.bars_when_called: list[int] = []

    def sync(
        self,
        config: MarketSyncConfig,
        progress_callback: object = None,
        *,
        detail_callback: object = None,
        abort_check: object = None,
    ) -> MarketSyncReport:
        self.configs.append(config)
        self.bars_when_called.append(self._store.status().bar_count)
        codes = len(config.codes)
        return MarketSyncReport(
            total_candidates=codes,
            processed=codes,
            updated_files=codes,
            unchanged_files=0,
            written_bars=60,
            errors=0,
            failure_reasons={},
        )


def test_health_check_job_returns_the_scan(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars())
    frame = _bars()
    store.replace_local_bars("SZ", "000001", "stock", frame.drop(index=[10, 11, 12]))

    with TestClient(create_app(market_data_store=store, auto_sync_enabled=False)) as client:
        response = client.post("/api/v1/market-data/health/check")
        assert response.status_code == 202
        state = _wait_for_market_job(client, response.json()["data"]["job_id"])

    assert state["status"] == "completed"
    result = state["result"]
    assert result["instrument_count"] == 2
    gap = next(item for item in result["checks"] if item["key"] == "gap")
    assert gap["severity"] == "error"
    assert gap["occurrences"] == 3


def test_health_repair_requires_confirmation() -> None:
    """修复会先删掉全部历史，没有 confirm 的请求必须被挡下。"""

    with TestClient(create_app(auto_sync_enabled=False)) as client:
        response = client.post(
            "/api/v1/market-data/health/repair",
            json={"checks": ["gap"]},
        )

    assert response.status_code == 422


def test_health_repair_deletes_before_redownloading(tmp_path: Path) -> None:
    """顺序不能反：不先删，在线行写不进本地行的位置，全量同步也覆盖不到中间那段。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars())
    store.replace_local_bars("SZ", "000001", "stock", _bars().drop(index=[10, 11, 12]))
    service = FakeMarketSyncService(store)
    app = create_app(market_data_store=store, market_sync=service, auto_sync_enabled=False)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/market-data/health/repair",
            json={"checks": ["gap"], "confirm": True},
        )
        assert response.status_code == 202
        state = _wait_for_market_job(client, response.json()["data"]["job_id"])

    assert state["status"] == "completed"
    result = state["result"]
    assert result["requested_codes"] == 1
    assert result["deleted_bars"] == 77
    assert result["written_bars"] == 60
    # 假的同步什么都没写回来，所以这只票必须被点名——真实场景下就是"在线已无数据"。
    assert result["empty_after_repair"] == ["000001"]
    assert service.configs[0].codes == ("000001",)
    assert service.configs[0].full_history is True
    # 调用同步时 000001 的历史已经被删掉了，只剩 600000 的 80 根。
    assert service.bars_when_called == [80]


def test_health_repair_without_candidates_does_not_call_the_provider(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars())
    service = FakeMarketSyncService(store)
    with TestClient(
        create_app(market_data_store=store, market_sync=service, auto_sync_enabled=False)
    ) as client:
        response = client.post(
            "/api/v1/market-data/health/repair",
            json={"checks": ["gap"], "confirm": True},
        )
        state = _wait_for_market_job(client, response.json()["data"]["job_id"])

    assert state["status"] == "completed"
    assert state["result"]["requested_codes"] == 0
    assert service.configs == []
