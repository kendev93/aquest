"""The workbench endpoints: readings, single-stock backtest, condition re-test."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi.testclient import TestClient

from selector_app.backtest.service import BacktestService
from selector_app.instrument_lab.service import InstrumentLabService
from selector_app.market_data.models import StockRef
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.web.app import create_app

_SAW = np.array([0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 4.0, 3.0, 2.0, 1.0])


def make_frame(length: int = 240) -> pd.DataFrame:
    positions = np.arange(length)
    close = 100.0 + 0.05 * positions + _SAW[positions % 10]
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=length, freq="D"),
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.full(length, 100_000.0),
            "amount": close * 100_000.0,
        }
    )


class FakeAdapter:
    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame

    def stock_ref(self, vipdoc_path: str | Path, market: str, code: str) -> StockRef:
        return StockRef(
            market=market,  # type: ignore[arg-type]
            code=code,
            path=Path(vipdoc_path) / f"{market.lower()}{code}.day",
            name="测试股份",
        )

    def read_stock(self, ref: StockRef) -> pd.DataFrame:
        return self.frame.copy(deep=True)


def _client(store: DuckDbMarketDataStore | None = None) -> TestClient:
    return TestClient(
        create_app(
            market_data_store=store,
            instrument_lab_service=InstrumentLabService(
                store,
                backtest_service=BacktestService(adapter=FakeAdapter(make_frame())),
            ),
        )
    )


def _condition_history(store: DuckDbMarketDataStore) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = {
        "candidates": [
            {
                "candidate_id": "c1",
                "side": "buy",
                "condition": {
                    "left_value": "technical.rsi14",
                    "operator": "lte",
                    "right_value": None,
                    "right_constant": 30.0,
                },
                "mining": {
                    "lift": 1.8,
                    "permutation_pvalue": 0.01,
                    "significant": True,
                    "support_count": 300,
                    "instrument_count": 120,
                },
            },
            {
                "candidate_id": "c2",
                "side": "sell",
                "condition": {
                    "left_value": "technical.wr14",
                    "operator": "gte",
                    "right_value": None,
                    "right_constant": 80.0,
                },
                "mining": {
                    "lift": 2.4,
                    "permutation_pvalue": 0.2,
                    "significant": False,
                },
            },
        ]
    }
    store.save_backtest_history(
        "research-1",
        backtest_type="condition_research",
        request={},
        result=result,
        started_at=base,
        finished_at=base + timedelta(seconds=30),
    )


def test_values_endpoint_returns_readings_and_percentile(tmp_path: Path) -> None:
    with _client() as client:
        response = client.post(
            "/api/v1/instrument-lab/values",
            json={
                "market": "SH",
                "code": "600000",
                "value_ids": ["rsi14", "ma20"],
            },
        )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["bars"] == 240
    assert [item["value_id"] for item in payload["series"]] == [
        "technical.rsi14",
        "technical.ma20",
    ]
    assert payload["series"][0]["percentile"] is not None
    assert any("自身历史分位" in note for note in payload["notes"])


def test_values_endpoint_rejects_unknown_indicator(tmp_path: Path) -> None:
    with _client() as client:
        response = client.post(
            "/api/v1/instrument-lab/values",
            json={"market": "SH", "code": "600000", "value_ids": ["nope"]},
        )

    assert response.status_code == 422


def test_condition_library_only_returns_significant_conditions(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    _condition_history(store)
    with _client(store) as client:
        response = client.get("/api/v1/instrument-lab/conditions/library")

    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["key"] == "technical.rsi14|lte||30"
    assert items[0]["lift"] == 1.8
    assert items[0]["side"] == "buy"


def test_condition_check_endpoint_runs_the_permutation(tmp_path: Path) -> None:
    with _client() as client:
        response = client.post(
            "/api/v1/instrument-lab/conditions/check",
            json={
                "market": "SH",
                "code": "600000",
                "conditions": [
                    {
                        "left_value": "technical.rsi14",
                        "operator": "gte",
                        "right_constant": 50.0,
                    }
                ],
                "horizon": 5,
                "permutations": 50,
            },
        )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["horizon"] == 5
    assert payload["sample_days"] > 0
    assert payload["permutations"] == 50
    assert len(payload["items"]) == 1
    assert payload["items"][0]["hit_count"] >= 0
    assert payload["items"][0]["base_mean_return"] is not None


def test_condition_check_endpoint_rejects_malformed_conditions(tmp_path: Path) -> None:
    with _client() as client:
        both_sides = client.post(
            "/api/v1/instrument-lab/conditions/check",
            json={
                "market": "SH",
                "code": "600000",
                "conditions": [
                    {
                        "left_value": "technical.rsi14",
                        "operator": "gt",
                        "right_value": "technical.ma20",
                        "right_constant": 1.0,
                    }
                ],
            },
        )
        duplicated = client.post(
            "/api/v1/instrument-lab/conditions/check",
            json={
                "market": "SH",
                "code": "600000",
                "conditions": [
                    {
                        "left_value": "technical.rsi14",
                        "operator": "gt",
                        "right_constant": 50.0,
                    },
                    {
                        "left_value": "technical.rsi14",
                        "operator": "gt",
                        "right_constant": 50.0,
                    },
                ],
            },
        )
        bad_code = client.post(
            "/api/v1/instrument-lab/conditions/check",
            json={
                "market": "SH",
                "code": "600",
                "conditions": [
                    {
                        "left_value": "technical.rsi14",
                        "operator": "gt",
                        "right_constant": 50.0,
                    }
                ],
            },
        )

    assert both_sides.status_code == 422
    assert duplicated.status_code == 422
    assert bad_code.status_code == 422


def test_backtest_endpoint_runs_the_shared_engine(tmp_path: Path) -> None:
    with _client() as client:
        response = client.post(
            "/api/v1/instrument-lab/backtest",
            json={
                "market": "SH",
                "code": "600000",
                "vipdoc_path": "/tmp/vipdoc",
                "buy_signal": "custom.buy",
                "sell_signal": "custom.sell",
                "formula_text": "BUY:C=101; SELL:C=103;",
                "date_mode": "custom",
                "start_date": "2024-01-01",
                "end_date": "2024-06-01",
                "initial_cash": 10_000.0,
            },
        )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["code"] == "600000"
    assert payload["bars"] > 0
    assert isinstance(payload["trades"], list)
    assert "total_return" in payload["performance"]


def test_backtest_endpoint_rejects_empty_rules(tmp_path: Path) -> None:
    with _client() as client:
        response = client.post(
            "/api/v1/instrument-lab/backtest",
            json={
                "market": "SH",
                "code": "600000",
                "date_mode": "custom",
                "start_date": "2024-01-01",
                "end_date": "2024-06-01",
            },
        )

    assert response.status_code == 422


def test_values_endpoint_rejects_reversed_window(tmp_path: Path) -> None:
    with _client() as client:
        response = client.post(
            "/api/v1/instrument-lab/values",
            json={
                "market": "SH",
                "code": "600000",
                "value_ids": ["rsi14"],
                "start_date": "2024-06-01",
                "end_date": "2024-01-01",
            },
        )

    assert response.status_code == 422


def test_window_outside_data_is_reported(tmp_path: Path) -> None:
    with _client() as client:
        response = client.post(
            "/api/v1/instrument-lab/values",
            json={
                "market": "SH",
                "code": "600000",
                "value_ids": ["rsi14"],
                "start_date": date(2030, 1, 1).isoformat(),
                "end_date": date(2030, 6, 1).isoformat(),
            },
        )

    assert response.status_code == 422
    assert "没有可用日线数据" in response.text
