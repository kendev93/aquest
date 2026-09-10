from __future__ import annotations

import struct
import time
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from selector_app.market_data.day_importer import LocalDayImporter
from selector_app.market_data.service import (
    LocalInstrument,
    LocalInstrumentPage,
    LocalMarketChart,
)
from selector_app.market_data.store import DuckDbMarketDataStore
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


def _write_day(path: Path, *dates: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"".join(
            struct.pack(
                "<IIIIIfII",
                value,
                1000,
                1100,
                900,
                1050,
                100000.0,
                100000,
                0,
            )
            for value in dates
        )
    )


def _wait_for_job(client: TestClient, job_id: str) -> dict[str, object]:
    for _ in range(100):
        response = client.get(f"/api/v1/market-data/jobs/{job_id}")
        assert response.status_code == 200
        state = response.json()["data"]
        if state["status"] in {"completed", "failed"}:
            return state
        time.sleep(0.01)
    raise AssertionError("market data job did not finish")


def test_local_import_job_populates_duckdb_and_market_routes_read_it(tmp_path: Path) -> None:
    vipdoc = tmp_path / "vipdoc"
    source = vipdoc / "sh" / "lday" / "sh600000.day"
    _write_day(source, 20240102, 20240103)
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    importer = LocalDayImporter(store)

    with TestClient(create_app(market_data_store=store, local_day_importer=importer)) as client:
        response = client.post(
            "/api/v1/market-data/import-local",
            json={"vipdoc_path": str(vipdoc), "universe": "all"},
        )
        assert response.status_code == 202
        state = _wait_for_job(client, response.json()["data"]["job_id"])
        instruments = client.get(
            "/api/v1/market-data/local/instruments",
            params={"market": "SH", "keyword": "600000"},
        )
        status = client.get("/api/v1/market-data/store")

    assert state["status"] == "completed"
    assert state["result"]["indicator_precompute"]["total_candidates"] == 1
    assert state["result"]["indicator_precompute"]["skipped"] == 0
    assert instruments.status_code == 200
    assert instruments.json()["data"][0]["code"] == "600000"
    assert status.status_code == 200
    assert status.json()["data"]["bar_count"] == 2


def test_app_startup_auto_imports_configured_vipdoc(tmp_path: Path, monkeypatch) -> None:
    vipdoc = tmp_path / "vipdoc"
    _write_day(vipdoc / "sh" / "lday" / "sh600000.day", 20240102)
    monkeypatch.setenv("SELECTOR_VIPDOC_PATH", str(vipdoc))
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    app = create_app(market_data_store=store)

    with TestClient(app) as client:
        job_id = app.state.startup_import_job_id
        state = _wait_for_job(client, job_id)

    assert state["status"] == "completed"
    assert state["description"] == "本地行情自动导入"
    assert state["result"]["imported_files"] == 1
    assert store.status().bar_count == 1
