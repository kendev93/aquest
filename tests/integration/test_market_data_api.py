from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi.testclient import TestClient

from selector_app.indicators.cache import IndicatorCache
from selector_app.market_data.service import (
    LocalInstrument,
    LocalInstrumentPage,
    LocalMarketChart,
)
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.web.app import create_app


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
    cache.get_or_compute(
        store.read_bars("SH", "600000"), ("price",), market="SH", code="600000"
    )
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
