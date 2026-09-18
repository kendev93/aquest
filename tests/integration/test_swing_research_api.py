"""Swing overview endpoint over a real DuckDB store."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from fastapi.testclient import TestClient

from selector_app.market_data.adapter import DuckDbMarketDataAdapter
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.swing_research.service import SwingResearchService
from selector_app.web.app import create_app


def _bars(market: str, code: str, closes: list[float]) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=len(closes), freq="D")
    return pd.DataFrame(
        [
            {
                "market": market,
                "code": code,
                "instrument_type": "stock",
                "date": day,
                "open": close,
                "high": close * 1.002,
                "low": close * 0.998,
                "close": close,
                "volume": 1_000.0,
                "amount": close * 1_000,
                "source": "local",
                "price_basis": "raw",
                "bar_status": "completed",
            }
            for day, close in zip(dates, closes, strict=True)
        ]
    )


def _store(tmp_path: Path) -> DuckDbMarketDataStore:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    peak = 10.0 * (1.01**30)
    closes = [10.0 * (1.01**index) for index in range(31)]
    closes += [peak * (0.99**index) for index in range(1, 20)]
    store.replace_local_bars("SH", "600000", "stock", _bars("SH", "600000", closes))
    return store


def test_overview_reports_one_scenario_per_threshold(tmp_path: Path) -> None:
    store = _store(tmp_path)
    app = create_app(
        market_data_store=store,
        auto_sync_enabled=False,
        swing_research_service=SwingResearchService(
            adapter=DuckDbMarketDataAdapter(store),
        ),
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/swing-research/overview",
            json={"reversal_multiples": [2.0, 3.0], "min_bars": 1},
        )

    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    assert payload["stock_count"] == 1
    assert payload["scanned_stocks"] == 1
    assert [item["reversal_multiple"] for item in payload["scenarios"]] == [2.0, 3.0]
    assert payload["date_range"]["start"] == "2024-01-01"


def test_overview_rejects_unusable_thresholds(tmp_path: Path) -> None:
    store = _store(tmp_path)
    app = create_app(
        market_data_store=store,
        auto_sync_enabled=False,
        swing_research_service=SwingResearchService(
            adapter=DuckDbMarketDataAdapter(store),
        ),
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/swing-research/overview",
            json={"reversal_multiples": [0.0]},
        )

    assert response.status_code == 400
