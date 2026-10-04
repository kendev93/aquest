"""Swing overview endpoint over a real DuckDB store."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
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
                "price_basis": "hfq",
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
    store.replace_local_bars(
        "SH", "600000", "stock", _bars("SH", "600000", closes), price_basis="raw"
    )
    return store


def _wide_store(tmp_path: Path) -> DuckDbMarketDataStore:
    """Four independent walkers, enough for a two-name cross-section."""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    rng = np.random.default_rng(9)
    for index in range(4):
        code = f"60000{index}"
        closes = [10.0]
        for _ in range(79):
            closes.append(closes[-1] * (1 + float(rng.normal(0.001, 0.02))))
        store.replace_local_bars("SH", code, "stock", _bars("SH", code, closes), price_basis="raw")
    return store


def _app(store: DuckDbMarketDataStore):
    return create_app(
        market_data_store=store,
        auto_sync_enabled=False,
        swing_research_service=SwingResearchService(adapter=DuckDbMarketDataAdapter(store)),
    )


def _moment(offset_minutes: int) -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=offset_minutes)


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


def test_indicator_study_scores_every_pair(tmp_path: Path) -> None:
    store = _wide_store(tmp_path)

    with TestClient(_app(store)) as client:
        response = client.post(
            "/api/v1/swing-research/indicator-study",
            json={
                "min_cross_section": 2,
                "permutations": 10,
                "horizon": 5,
                "labels": ["up_onset_ahead", "down_onset_ahead"],
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    assert payload["scanned_stocks"] == 4
    # Every dimensionless technical output is a candidate; the price-scaled ones
    # are excluded because they cannot be compared across stocks.
    assert payload["value_count"] >= 20
    assert len(payload["scores"]) == payload["value_count"] * 2
    assert {item["label"] for item in payload["scores"]} == {
        "up_onset_ahead",
        "down_onset_ahead",
    }
    for item in payload["scores"]:
        assert isinstance(item["threshold_hint"], float | None)
        assert item["note"]


def test_indicator_study_rejects_foreign_indicators(tmp_path: Path) -> None:
    store = _wide_store(tmp_path)

    with TestClient(_app(store)) as client:
        response = client.post(
            "/api/v1/swing-research/indicator-study",
            json={"value_ids": ["indicator_one.var1"]},
        )

    assert response.status_code == 400
    assert "不能用于波段研究" in response.json()["error"]["message"]


def test_indicator_study_rejects_out_of_range_horizon(tmp_path: Path) -> None:
    store = _wide_store(tmp_path)

    with TestClient(_app(store)) as client:
        response = client.post("/api/v1/swing-research/indicator-study", json={"horizon": 0})

    assert response.status_code == 422


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


def _sse_events(body: str) -> list[dict[str, object]]:
    import json as _json

    return [
        _json.loads(line[len("data: ") :])
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


def test_indicator_study_stream_emits_progress_before_the_result(tmp_path: Path) -> None:
    """The whole point of the stream: the bar moves while the work happens."""

    store = _wide_store(tmp_path)

    with TestClient(_app(store)) as client:
        with client.stream(
            "POST",
            "/api/v1/swing-research/indicator-study/stream",
            json={
                "min_cross_section": 2,
                "permutations": 10,
                "horizon": 5,
                "labels": ["up_onset_ahead", "down_onset_ahead"],
            },
        ) as response:
            assert response.status_code == 200, response.text
            assert response.headers["content-type"].startswith("text/event-stream")
            # Without this nginx would buffer every event until the run ended.
            assert response.headers.get("x-accel-buffering") == "no"
            body = "".join(response.iter_text())

    events = _sse_events(body)
    assert events, body
    kinds = [item["type"] for item in events]
    assert kinds[-1] == "result"
    assert kinds.count("result") == 1

    progress = [item for item in events if item["type"] == "progress"]
    assert progress, "进度条会一直停在 0"
    steps = [int(str(item["current"])) for item in progress]
    assert steps == sorted(steps)
    assert steps[-1] == progress[-1]["total"], "结束时必须报到满格"
    assert {item["stage"] for item in progress} & {
        "正在扫描交易日",
        "正在计算指标与切分波段",
        "正在逐日横截面打分",
    }
    assert events[-1]["data"]["scanned_stocks"] == 4


def test_indicator_study_is_saved_with_step_one_and_can_be_reloaded(tmp_path: Path) -> None:
    """A study is minutes of work - it must survive a reload, both steps of it."""

    store = _wide_store(tmp_path)

    with TestClient(_app(store)) as client:
        response = client.post(
            "/api/v1/swing-research/indicator-study",
            json={
                "min_cross_section": 2,
                "permutations": 10,
                "horizon": 5,
                "labels": ["up_onset_ahead", "down_onset_ahead"],
                # 第一步只活在浏览器里，不一起存档就还原不回来。
                "overview_request": {"multiplesText": "2, 3", "limitStocks": 4},
                "overview_result": {"stock_count": 4},
            },
        )
        assert response.status_code == 200, response.text
        history_id = response.json()["history_id"]
        assert history_id

        listed = client.get("/api/v1/swing-research/history").json()["data"]
        assert [item["history_id"] for item in listed] == [history_id]
        summary = listed[0]["summary"]
        assert summary["scanned_stocks"] == 4
        assert summary["horizon"] == 5
        assert summary["significant_count"] >= 0
        assert "top_indicator" in summary

        detail = client.get(f"/api/v1/swing-research/history/{history_id}").json()["data"]
        assert detail["backtest_type"] == "swing_research"
        assert detail["request"]["overview_request"] == {
            "multiplesText": "2, 3",
            "limitStocks": 4,
        }
        assert detail["request"]["overview_result"] == {"stock_count": 4}
        assert detail["result"]["scanned_stocks"] == 4

        deleted = client.delete(f"/api/v1/swing-research/history/{history_id}").json()["data"]
        assert deleted["deleted"] is True
        assert client.get("/api/v1/swing-research/history").json()["data"] == []


def test_indicator_study_stream_hands_back_the_history_id(tmp_path: Path) -> None:
    """The UI only ever uses the stream, so the id has to ride the result event."""

    store = _wide_store(tmp_path)

    with TestClient(_app(store)) as client:
        with client.stream(
            "POST",
            "/api/v1/swing-research/indicator-study/stream",
            json={
                "min_cross_section": 2,
                "permutations": 10,
                "horizon": 5,
                "labels": ["up_onset_ahead", "down_onset_ahead"],
            },
        ) as response:
            assert response.status_code == 200
            body = "".join(response.iter_text())
        events = _sse_events(body)
        history_id = events[-1].get("history_id")
        assert history_id, "流式结果不带历史 id，页面就没法刷新列表"
        listed = client.get("/api/v1/swing-research/history").json()["data"]
        assert [item["history_id"] for item in listed] == [history_id]


def test_swing_history_endpoints_do_not_touch_other_types(tmp_path: Path) -> None:
    """Research histories share one table with backtests - deleting must check."""

    store = _wide_store(tmp_path)
    store.save_backtest_history(
        "portfolio-1",
        backtest_type="portfolio",
        request={"universe": "all"},
        result={"performance": {"total_return": 0.1}},
        started_at=_moment(1),
        finished_at=_moment(2),
    )

    with TestClient(_app(store)) as client:
        assert client.get("/api/v1/swing-research/history").json()["data"] == []
        assert client.get("/api/v1/swing-research/history/portfolio-1").status_code == 404
        assert client.delete("/api/v1/swing-research/history/portfolio-1").json() == {
            "data": {"deleted": False}
        }
        assert client.get("/api/v1/swing-research/history/missing").status_code == 404
        # 别人的历史还好好的。
        assert [item["history_id"] for item in store.list_backtest_history("portfolio")] == [
            "portfolio-1"
        ]


def test_indicator_study_stream_reports_errors_as_events(tmp_path: Path) -> None:
    """The stream already answered 200, so a failure has to arrive as an event."""

    store = _wide_store(tmp_path)

    with TestClient(_app(store)) as client:
        with client.stream(
            "POST",
            "/api/v1/swing-research/indicator-study/stream",
            json={"value_ids": ["indicator_one.var1"]},
        ) as response:
            assert response.status_code == 200
            body = "".join(response.iter_text())

    events = _sse_events(body)
    assert events[-1]["type"] == "error"
    assert "不能用于波段研究" in str(events[-1]["detail"])
