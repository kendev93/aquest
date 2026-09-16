from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient

from selector_app.market_data.service import (
    LocalInstrument,
    LocalInstrumentPage,
    LocalMarketChart,
)
from selector_app.web.app import create_app


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


def test_local_import_endpoint_is_removed() -> None:
    with TestClient(create_app(auto_sync_enabled=False)) as client:
        response = client.post(
            "/api/v1/market-data/import-local",
            json={"vipdoc_path": "/data/vipdoc", "universe": "all"},
        )

    assert response.status_code == 404
