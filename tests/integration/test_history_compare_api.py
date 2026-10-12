"""The history comparison endpoints: what the page can select and what it gets back."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from aquest.market_data.store import DuckDbMarketDataStore
from aquest.web.app import create_app


def _portfolio_result(total_return: float = 0.1) -> dict[str, object]:
    return {
        "universe": "all",
        "bars": 3,
        "start_date": "2024-01-02",
        "end_date": "2024-01-04",
        "processed": 40,
        "performance": {
            "start_cash": 100_000.0,
            "total_return": total_return,
            "annual_return": total_return / 2,
            "max_drawdown": 0.12,
            "sharpe": 0.8,
            "win_rate": 0.55,
            "total_trades": 8,
        },
        "equity_curve": [
            {"date": "2024-01-02", "total": 100_000.0},
            {"date": "2024-01-03", "total": 105_000.0},
            {"date": "2024-01-04", "total": 100_000.0 + total_return * 100_000.0},
        ],
    }


def _factor_result() -> dict[str, object]:
    return {
        "dates": ["2024-01-02", "2024-01-03", "2024-01-04"],
        "date_range": {"start": "2024-01-02", "end": "2024-01-04", "days": 3},
        "scanned_stocks": 300,
        "significant_count": 1,
        "factors": [
            {
                "value_id": "technical.rsi14",
                "display_name": "RSI14",
                "ic_mean": 0.031,
                "significant": True,
                "long_short": {
                    "mean_spread": 0.001,
                    "win_rate": 0.58,
                    "cumulative": [0.0, 0.001, 0.002],
                },
            }
        ],
    }


def _seed(store: DuckDbMarketDataStore) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    records = [
        ("portfolio-a", "portfolio", _portfolio_result(0.1)),
        ("portfolio-b", "portfolio", _portfolio_result(-0.05)),
        ("factor-a", "factor_test", _factor_result()),
    ]
    for index, (history_id, backtest_type, result) in enumerate(records):
        started = base + timedelta(minutes=index)
        store.save_backtest_history(
            history_id,
            backtest_type=backtest_type,
            request={},
            result=result,
            started_at=started,
            finished_at=started + timedelta(seconds=30),
        )


def test_candidates_are_listed_across_types(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    _seed(store)
    with TestClient(create_app(market_data_store=store)) as client:
        response = client.get("/api/v1/history-compare/candidates")

    assert response.status_code == 200
    payload = response.json()["data"]
    assert {item["backtest_type"] for item in payload["items"]} == {"portfolio", "factor_test"}
    kinds = {item["history_id"]: item["curve_kind"] for item in payload["items"]}
    assert kinds["portfolio-a"] == "equity"
    assert kinds["factor-a"] == "factor_long_short"
    assert payload["max_items"] == 5


def test_compare_two_portfolios_plots_them(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    _seed(store)
    with TestClient(create_app(market_data_store=store)) as client:
        response = client.post(
            "/api/v1/history-compare", json={"history_ids": ["portfolio-a", "portfolio-b"]}
        )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["curve_group"]["plotted"] is True
    assert len(payload["items"]) == 2
    first = payload["items"][0]
    assert first["curve"]["points"][0]["value"] == 1.0
    assert first["curve"]["normalize"] == "capital"
    assert first["metrics"][0]["value"] == 0.1
    assert payload["warnings"] == []


def test_compare_mixed_types_still_returns_but_refuses_to_plot(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    _seed(store)
    with TestClient(create_app(market_data_store=store)) as client:
        response = client.post(
            "/api/v1/history-compare", json={"history_ids": ["portfolio-a", "factor-a"]}
        )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["curve_group"]["plotted"] is False
    assert payload["curve_group"]["excluded"]
    assert any("跨类型对比" in warning for warning in payload["warnings"])


def test_compare_rejects_bad_selections(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    _seed(store)
    with TestClient(create_app(market_data_store=store)) as client:
        assert (
            client.post(
                "/api/v1/history-compare", json={"history_ids": ["portfolio-a"]}
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/v1/history-compare",
                json={"history_ids": ["portfolio-a", "portfolio-a"]},
            ).status_code
            == 422
        )
        unknown = client.post(
            "/api/v1/history-compare", json={"history_ids": ["portfolio-a", "nope"]}
        )
        assert unknown.status_code == 422
        assert "nope" in unknown.text


def test_compare_accepts_first_value_normalization_and_index_axis(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    _seed(store)
    with TestClient(create_app(market_data_store=store)) as client:
        response = client.post(
            "/api/v1/history-compare",
            json={
                "history_ids": ["portfolio-a", "portfolio-b"],
                "normalize": "first",
                "x_axis": "index",
            },
        )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["normalize"] == "first"
    assert payload["x_axis"] == "index"
    assert payload["items"][0]["curve"]["normalize"] == "first"
