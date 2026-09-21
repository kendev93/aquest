"""候选库端点：入库 → 表态 → 显式发布（且发布后仍是离线的）。

集成层只盯契约：状态码、以及「发布出来的策略没有上线」这条承诺在真实 HTTP +
真实 duckdb 下依然成立。
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from selector_app.candidate_library.service import CandidateLibraryService
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.web.app import create_app

_CONDITION = {"left_value": "technical.bias20", "operator": "lte", "right_constant": -5.0}

_RESEARCH_REQUEST: dict[str, Any] = {
    "universe": "all",
    "boards": ["main"],
    "instrument_types": ["stock"],
    "buy_rule": None,
    "sell_rule": None,
    "date_mode": "custom",
    "start_date": "2024-01-02",
    "end_date": "2026-09-10",
    "initial_cash": 100_000.0,
    "commission": 0.0001,
    "min_commission": 0.1,
    "stamp_tax": 0.001,
    "slippage": 0.0,
    "execution": "next_open",
    "position_mode": "full",
    "fixed_size": None,
    "ranking_value": "technical.rsrs_zscore",
    "rank_order": "desc",
    "ranking_top_n": 5,
    "min_trades": 20,
    "significance_permutations": 200,
    "formula_text": None,
    "formula_parameters": {},
}

_CANDIDATE: dict[str, Any] = {
    "candidate_id": "buy-1",
    "side": "buy",
    "condition": _CONDITION,
    "mining": {
        "lift": 1.8,
        "support_count": 320,
        "instrument_count": 120,
        "outcome_rate": 0.62,
        "baseline_rate": 0.5,
        "support_ratio": 0.08,
        "permutation_pvalue": 0.02,
        "significant": True,
    },
    "sensitivity": {"median_retention": 0.7},
    "validation": {"pooled_trades": {"trade_count": 320, "win_rate": 0.62}},
    "holdout": None,
    "consistency": None,
    "passed_constraints": True,
    "constraint_failures": [],
}


def _store(tmp_path) -> DuckDbMarketDataStore:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.save_backtest_history(
        "study-1",
        backtest_type="condition_research",
        request=_RESEARCH_REQUEST,
        result={"candidates": [_CANDIDATE], "processed": 5045},
        started_at="2026-09-20 10:00:00",
        finished_at="2026-09-20 10:30:00",
    )
    return store


def _client(store: DuckDbMarketDataStore) -> TestClient:
    return TestClient(
        create_app(
            market_data_store=store,
            candidate_library_service=CandidateLibraryService(store),
        )
    )


def _candidate_id(client: TestClient) -> str:
    response = client.post(
        "/api/v1/candidate-library/import",
        json={"history_id": "study-1", "candidate_ids": ["buy-1"]},
    )
    assert response.status_code == 201
    return str(response.json()["data"]["added"][0]["candidate_id"])


def test_import_then_list_then_read(tmp_path) -> None:
    client = _client(_store(tmp_path))
    candidate_id = _candidate_id(client)

    listed = client.get("/api/v1/candidate-library")
    assert listed.status_code == 200
    items = listed.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["status"] == "pending"
    assert items[0]["evidence"]["lift"] == 1.8
    assert items[0]["evidence"]["study"]["scanned_stocks"] == 5045

    read = client.get(f"/api/v1/candidate-library/{candidate_id}")
    assert read.status_code == 200
    assert read.json()["data"]["condition"]["left_value"] == "technical.bias20"

    filtered = client.get("/api/v1/candidate-library", params={"status": "adopted"})
    assert filtered.json()["data"]["items"] == []


def test_import_is_idempotent_over_http(tmp_path) -> None:
    client = _client(_store(tmp_path))
    _candidate_id(client)

    again = client.post(
        "/api/v1/candidate-library/import",
        json={"history_id": "study-1", "candidate_ids": ["buy-1"]},
    )

    assert again.status_code == 201
    assert again.json()["data"]["added"] == []
    assert again.json()["data"]["skipped"]


def test_importing_from_a_forgotten_study_is_409(tmp_path) -> None:
    client = _client(_store(tmp_path))

    response = client.post(
        "/api/v1/candidate-library/import",
        json={"history_id": "gone", "candidate_ids": ["buy-1"]},
    )

    assert response.status_code == 409
    assert "不在历史列表" in response.json()["error"]["message"]


def test_publishing_before_adopting_is_409(tmp_path) -> None:
    client = _client(_store(tmp_path))
    candidate_id = _candidate_id(client)

    response = client.post(
        f"/api/v1/candidate-library/{candidate_id}/publish", json={"name": "甲策略"}
    )

    assert response.status_code == 409
    assert "先采纳" in response.json()["error"]["message"]


def test_preview_then_publish_leaves_the_strategy_offline(tmp_path) -> None:
    client = _client(_store(tmp_path))
    candidate_id = _candidate_id(client)
    assert (
        client.patch(
            f"/api/v1/candidate-library/{candidate_id}", json={"status": "adopted"}
        ).status_code
        == 200
    )

    preview = client.get(f"/api/v1/candidate-library/{candidate_id}/publish-preview")
    assert preview.status_code == 200
    body = preview.json()["data"]
    # 这次研究的卖出基准是「持有到期末」，组合回测不设离场就永远不卖：预览必须
    # 先把它拦下来，而不是替人填一个止损比例。
    assert body["blocked"] is True
    assert body["ready"] is False
    assert any("离场" in item for item in body["blockers"])
    assert body["request"]["ranking_value"] == "technical.rsrs_zscore"

    published = client.post(
        f"/api/v1/candidate-library/{candidate_id}/publish",
        json={"name": "甲策略", "stop_loss_pct": 0.1},
    )
    assert published.status_code == 201
    outcome = published.json()["data"]
    assert outcome["online"] is False
    assert outcome["candidate"]["status"] == "published"

    strategy_id = outcome["strategy"]["strategy_id"]
    strategies = client.get("/api/v1/strategies")
    record = next(item for item in strategies.json()["data"] if item["strategy_id"] == strategy_id)
    assert record["summary"]["universe"] == "all"
    assert "performance" not in record["summary"]


def test_withdrawing_a_publication_deletes_the_strategy(tmp_path) -> None:
    client = _client(_store(tmp_path))
    candidate_id = _candidate_id(client)
    client.patch(f"/api/v1/candidate-library/{candidate_id}", json={"status": "adopted"})
    strategy_id = client.post(
        f"/api/v1/candidate-library/{candidate_id}/publish",
        json={"name": "甲策略", "stop_loss_pct": 0.1},
    ).json()["data"]["strategy"]["strategy_id"]

    withdrawn = client.patch(
        f"/api/v1/candidate-library/{candidate_id}", json={"status": "adopted"}
    )
    assert withdrawn.status_code == 200
    assert withdrawn.json()["data"]["published_strategy_id"] is None

    assert client.get(f"/api/v1/strategies/{strategy_id}").status_code == 404


def test_deleting_a_published_candidate_is_409(tmp_path) -> None:
    client = _client(_store(tmp_path))
    candidate_id = _candidate_id(client)
    client.patch(f"/api/v1/candidate-library/{candidate_id}", json={"status": "adopted"})
    client.post(
        f"/api/v1/candidate-library/{candidate_id}/publish",
        json={"name": "甲策略", "stop_loss_pct": 0.1},
    )

    response = client.delete(f"/api/v1/candidate-library/{candidate_id}")

    assert response.status_code == 409
    assert "先撤回" in response.json()["error"]["message"]


def test_unknown_candidate_is_404(tmp_path) -> None:
    client = _client(_store(tmp_path))

    assert client.get("/api/v1/candidate-library/nope").status_code == 404
    assert client.delete("/api/v1/candidate-library/nope").status_code == 404
    assert (
        client.patch("/api/v1/candidate-library/nope", json={"status": "adopted"}).status_code
        == 404
    )
