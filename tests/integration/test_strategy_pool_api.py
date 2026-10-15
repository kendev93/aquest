from __future__ import annotations

import numpy as np
import pandas as pd
from fastapi.testclient import TestClient

from aquest.backtest.service import BacktestService
from aquest.market_data.adapter import DuckDbMarketDataAdapter
from aquest.market_data.store import DuckDbMarketDataStore
from aquest.portfolio_backtest.service import PortfolioBacktestService
from aquest.strategy_pool.service import MarketStrategyPoolService
from aquest.strategy_pool.store import FOCUS_LIMIT, DuckDbStrategyPoolStore
from aquest.web.app import create_app

TOKEN = "pool-worker-test-token"


def _store(
    tmp_path, *, tail: tuple[tuple[float, float, float, float], ...] = ()
) -> DuckDbMarketDataStore:
    """120 根上涨日线，最后一根砸到止损线以下；``tail`` 用来再追加几根。

    风控卖出与规则卖出一样要等下一根开盘成交，所以「触发当日」和「次日成交」是两条
    不同的断言，靠 ``tail`` 把回放终点推到成交那天。
    """

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    close = np.linspace(10.0, 15.0, 120)
    bars = pd.DataFrame(
        {
            "date": pd.date_range("2025-01-01", periods=120, freq="D"),
            "open": close,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": 1_000.0,
            "amount": close * 1_000,
            "bar_status": "completed",
        }
    )
    bars.loc[bars.index[-1], ["open", "high", "low", "close"]] = [10.0, 10.2, 9.5, 10.0]
    if tail:
        dates = pd.date_range("2025-01-01", periods=120 + len(tail), freq="D")[120:]
        bars = pd.concat(
            [
                bars,
                pd.DataFrame(
                    {
                        "date": dates,
                        "open": [row[0] for row in tail],
                        "high": [row[1] for row in tail],
                        "low": [row[2] for row in tail],
                        "close": [row[3] for row in tail],
                        "volume": 1_000.0,
                        "amount": [row[3] * 1_000 for row in tail],
                        "bar_status": "completed",
                    }
                ),
            ],
            ignore_index=True,
        )
    store.replace_local_bars("SH", "600000", "stock", bars)
    return store


def test_pool_job_submission_single_rerun_history_and_cancel(tmp_path) -> None:
    app = create_app(
        market_data_store=_store(tmp_path),
        strategy_worker_token=TOKEN,
        auto_sync_enabled=False,
    )

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/strategy-pool/jobs",
            json={"mode": "single", "market": "SH", "code": "600000"},
        )
        job_id = created.json()["data"]["job_id"]
        detail = client.get(f"/api/v1/strategy-pool/jobs/{job_id}")
        history = client.get("/api/v1/strategy-pool/jobs")
        stats = client.get("/api/v1/strategy-pool/jobs/stats")
        cancelled = client.post(f"/api/v1/strategy-pool/jobs/{job_id}/cancel")
        missing = client.post(
            "/api/v1/strategy-pool/jobs",
            json={"mode": "single", "market": "SH", "code": "999999"},
        )

    assert created.status_code == 202
    assert detail.status_code == 200
    assert detail.json()["data"]["status"] == "queued"
    payload = history.json()["data"]
    item = payload["items"][0]
    assert item["job_id"] == job_id
    # 排队中的任务既没有 started_at 也没有终态时长，页面据此区分「含排队」与「研究中」。
    assert item["mode"] == "single"
    assert item["duration_seconds"] is None
    assert item["duration_includes_queue"] is True
    assert item["newly_researched_symbols"] == 0
    assert item["maximum_parallelism"] is None
    # /jobs/stats 必须注册在 /jobs/{job_id} 之前，否则 stats 会被当成任务 ID 打成 404。
    assert stats.status_code == 200
    stats_payload = stats.json()["data"]
    assert stats_payload["sample_limit"] == 60
    assert all(row["samples"] == 0 for row in stats_payload["modes"].values())
    assert cancelled.json()["data"]["cancelled"] is True
    assert missing.status_code == 422


def test_single_rerun_queues_behind_signal_scan_but_not_another_single(tmp_path) -> None:
    app = create_app(
        market_data_store=_store(tmp_path),
        strategy_worker_token=TOKEN,
        auto_sync_enabled=False,
    )
    app.state.strategy_pool_store.set_manual_override(
        "SH",
        "600000",
        "annual_return",
        {
            "candidate_id": "existing",
            "buy_rule": {"signals": ["technical.macd"]},
            "sell_rule": {"signals": ["technical.rsi14"]},
            "config": {},
        },
    )

    with TestClient(app) as client:
        signal_scan = client.post(
            "/api/v1/strategy-pool/jobs",
            json={"mode": "signals", "objective": "annual_return"},
        )
        queued_rerun = client.post(
            "/api/v1/strategy-pool/jobs",
            json={"mode": "single", "market": "SH", "code": "600000"},
        )
        duplicate_rerun = client.post(
            "/api/v1/strategy-pool/jobs",
            json={"mode": "single", "market": "SH", "code": "600000"},
        )
        active_jobs = client.get("/api/v1/jobs/active")

    assert signal_scan.status_code == 202
    assert queued_rerun.status_code == 202
    assert queued_rerun.json()["data"]["status"] == "queued"
    assert duplicate_rerun.status_code == 409
    assert len(active_jobs.json()["data"]) == 2


def test_strategy_pool_job_history_includes_signal_objective_for_result_restore(tmp_path) -> None:
    app = create_app(
        market_data_store=_store(tmp_path),
        strategy_worker_token=TOKEN,
        auto_sync_enabled=False,
    )
    app.state.strategy_pool_store.set_manual_override(
        "SH",
        "600000",
        "annual_return",
        {
            "candidate_id": "history-signal-candidate",
            "buy_rule": {"signals": ["technical.macd"]},
            "sell_rule": {"signals": ["technical.rsi14"]},
            "config": {},
        },
    )

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/strategy-pool/jobs",
            json={
                "mode": "signals",
                "objective": "annual_return",
                "market": "SH",
                "code": "600000",
            },
        )
        assert created.status_code == 202, created.json()
        job_id = created.json()["data"]["job_id"]
        history = client.get("/api/v1/strategy-pool/jobs")

    assert created.status_code == 202
    summary = next(item for item in history.json()["data"]["items"] if item["job_id"] == job_id)
    assert summary["mode"] == "signals"
    assert summary["objective"] == "annual_return"
    assert summary["market"] == "SH"
    assert summary["code"] == "600000"


def test_single_rerun_remains_blocked_during_full_market_research(tmp_path) -> None:
    app = create_app(
        market_data_store=_store(tmp_path),
        strategy_worker_token=TOKEN,
        auto_sync_enabled=False,
    )
    app.state.strategy_job_backend.submit(
        "market_strategy_pool",
        {"mode": "full", "risk_trials": 3_000},
        description="全市场策略池研究",
        worker_count=1,
        metadata={"mode": "full"},
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/strategy-pool/jobs",
            json={"mode": "single", "market": "SH", "code": "600000"},
        )

    assert response.status_code == 409


def test_pool_manual_overrides_are_separate_by_objective_and_list_active_source(tmp_path) -> None:
    app = create_app(
        market_data_store=_store(tmp_path),
        auto_sync_enabled=False,
    )
    annual = {
        "candidate_id": "annual-manual",
        "buy_rule": {
            "conditions": [
                {"left_value": "technical.rsi14", "operator": "lt", "right_constant": 30}
            ]
        },
        "sell_rule": {"signals": ["technical.macd"]},
        "config": {"stop_loss_pct": 0.08},
    }
    sharpe = {
        "candidate_id": "sharpe-manual",
        "buy_rule": {"signals": ["technical.macd"]},
        "sell_rule": {"signals": ["technical.rsi14"]},
        "config": {"trailing_stop_pct": 0.1},
    }

    with TestClient(app) as client:
        annual_response = client.post(
            "/api/v1/strategy-pool/entries/SH/600000/overrides/annual_return",
            json={
                "candidate": annual,
                "benchmark": {"full_period": {"total_return": 0.2}},
            },
        )
        sharpe_response = client.post(
            "/api/v1/strategy-pool/entries/SH/600000/overrides/sharpe",
            json={"candidate": sharpe},
        )
        annual_list = client.get("/api/v1/strategy-pool/entries?objective=annual_return")
        sharpe_list = client.get("/api/v1/strategy-pool/entries?objective=sharpe")

    assert annual_response.status_code == 200
    assert sharpe_response.status_code == 200
    assert annual_response.json()["data"]["benchmark"]["full_period"]["total_return"] == 0.2
    assert (
        annual_list.json()["data"]["items"][0]["active_candidate"]["candidate_id"]
        == "annual-manual"
    )
    assert annual_list.json()["data"]["items"][0]["active_source"] == "manual"
    assert (
        sharpe_list.json()["data"]["items"][0]["active_candidate"]["candidate_id"]
        == "sharpe-manual"
    )


def test_worker_checkpoints_only_accept_claimed_pool_jobs(tmp_path) -> None:
    app = create_app(
        market_data_store=_store(tmp_path),
        strategy_worker_token=TOKEN,
        auto_sync_enabled=False,
    )
    job_id = app.state.strategy_job_backend.submit(
        "market_strategy_pool",
        {"mode": "full", "risk_trials": 3_000},
        description="全市场策略池研究",
        worker_count=1,
    )
    candidate = {
        "candidate_id": "candidate-1",
        "buy_rule": {"signals": ["technical.macd"]},
        "sell_rule": {"signals": ["technical.rsi14"]},
        "config": {"stop_loss_pct": 0.08},
    }

    with TestClient(app) as client:
        client.post(
            "/internal/strategy-worker/jobs/claim",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"worker_id": "worker-a"},
        )
        wrong_worker = client.post(
            f"/internal/strategy-worker/jobs/{job_id}/pool-entry",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"worker_id": "worker-b", "entry": {}},
        )
        saved = client.post(
            f"/internal/strategy-worker/jobs/{job_id}/pool-entry",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={
                "worker_id": "worker-a",
                "entry": {
                    "market": "SH",
                    "code": "600000",
                    "name": "浦发银行",
                    "annual_candidate": candidate,
                    "sharpe_candidate": None,
                    "benchmark": {
                        "full_period": {"total_return": 0.25},
                        "validation": {"total_return": 0.1},
                        "holdout": {"total_return": 0.05},
                    },
                    "data_fingerprint": "result-fingerprint",
                    "market_fingerprint": "bars-fingerprint",
                    "config_fingerprint": "config-fingerprint",
                },
            },
        )
        benchmark_saved = client.post(
            f"/internal/strategy-worker/jobs/{job_id}/pool-entry",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={
                "worker_id": "worker-a",
                "entry": {
                    "market": "SH",
                    "code": "600000",
                    "benchmark_only": True,
                    "benchmark": {"validation": {"total_return": 0.3}},
                },
            },
        )
        entries = client.get("/api/v1/strategy-pool/entries")

    assert wrong_worker.status_code == 409
    assert saved.status_code == 200
    assert benchmark_saved.status_code == 200
    assert (
        entries.json()["data"]["items"][0]["automatic_candidates"]["annual_return"]["candidate_id"]
        == "candidate-1"
    )
    assert entries.json()["data"]["items"][0]["benchmark"]["validation"]["total_return"] == 0.3


def _signal_scan(tmp_path, *, tail: tuple[tuple[float, float, float, float], ...] = ()) -> dict:
    """跑一次「刷新当天信号」，返回结果。``tail`` 决定 600000 在止损触发日之后还有几根。"""

    market_store = _store(tmp_path, tail=tail)
    extra_close = np.linspace(20.0, 22.0, 120)
    market_store.replace_local_bars(
        "SH",
        "600001",
        "stock",
        pd.DataFrame(
            {
                "date": pd.date_range("2025-01-01", periods=120, freq="D"),
                "open": extra_close,
                "high": extra_close + 0.2,
                "low": extra_close - 0.2,
                "close": extra_close,
                "volume": 1_000.0,
                "amount": extra_close * 1_000,
                "bar_status": "completed",
            }
        ),
    )
    pool = DuckDbStrategyPoolStore(market_store)
    pool.initialize()
    pool.set_manual_override(
        "SH",
        "600000",
        "annual_return",
        {
            "candidate_id": "signal-candidate",
            "buy_rule": {
                "signals": [],
                "conditions": [
                    {"left_value": "price.close", "operator": "gt", "right_constant": 0}
                ],
                "required_conditions": [],
                "combine_mode": "all",
            },
            "sell_rule": {
                "signals": [],
                "conditions": [
                    {"left_value": "price.close", "operator": "lt", "right_constant": 0}
                ],
                "required_conditions": [],
                "combine_mode": "all",
            },
            "metrics": {
                "validation": {
                    "total_return": 0.32,
                    "annual_return": 0.2,
                    "sharpe": 1.0,
                    "win_rate": 0.6,
                    "max_drawdown": -0.1,
                    "total_trades": 4,
                },
                "holdout": {"total_return": -0.05, "total_trades": 2},
                "full_period": {"total_return": 0.45},
            },
            "config": {"stop_loss_pct": 0.05},
        },
    )
    adapter = DuckDbMarketDataAdapter(market_store)
    service = MarketStrategyPoolService(
        store=market_store,
        adapter=adapter,
        individual_service=object(),  # signal mode does not invoke the research service
        backtest_service=BacktestService(adapter=adapter),
        pool_store=pool,
        portfolio_service=PortfolioBacktestService(adapter=adapter),
    )
    return service.run({"mode": "signals", "objective": "annual_return"}, job_id="signals-job")


def test_signal_scan_evaluates_active_rules_on_latest_completed_bar(tmp_path) -> None:
    result = _signal_scan(tmp_path)

    assert result["buy_signals"][0]["code"] == "600000"
    assert result["buy_signals"][0]["total_return"] == 0.32
    assert result["buy_signals"][0]["full_period_return"] == 0.45
    assert result["buy_signals"][0]["holdout_return"] == -0.05
    assert result["buy_signals"][0]["validation_trades"] == 4
    assert result["buy_signals"][0]["holdout_trades"] == 2
    assert result["sell_signals"] == []
    assert result["states"][0]["status"] == "buy"
    assert result["model_portfolio"]["available"] is True
    assert result["model_portfolio"]["holding_scope"] == "per_stock_strategy"
    held = result["model_portfolio"]["holdings"]
    # 最后一根砸破 5% 止损线：当日收盘才算得出来，所以只能登记成挂单，仍算持仓。
    assert [row["code"] for row in held] == ["600000"]
    assert held[0]["pending_exit_risk_trigger"] == "stop_loss"
    assert held[0]["pending_exit_reason"] == "止损"
    assert held[0]["pending_exit_signal_date"] == "2025-04-30"
    assert held[0]["stop_loss_price"] is not None
    assert held[0]["risk_level_as_of"] == "2025-04-30"
    assert result["model_portfolio"]["risk_exit_signals"] == []
    assert result["signal_target"] is None
    assert result["holding_scope"] == "per_stock_strategy"
    assert result["holding_snapshot_complete"] is True
    assert result["total_symbols"] == 1
    assert [state["code"] for state in result["states"]] == ["600000"]
    assert result["market_snapshot"]["data_end"] == "2025-04-30"
    assert result["strategy_snapshot"] == [
        {"market": "SH", "code": "600000", "candidate_id": "signal-candidate"}
    ]


def test_risk_exit_fills_at_the_next_completed_bar_open(tmp_path) -> None:
    result = _signal_scan(tmp_path, tail=((11.9, 12.1, 11.8, 12.0),))

    # 追加一根之后，昨日的止损挂单按那根开盘成交：成交日比触发日晚一根。
    exits = result["model_portfolio"]["risk_exit_signals"]
    assert [row["risk_trigger"] for row in exits] == ["stop_loss"]
    assert exits[0]["signal_date"] == "2025-04-30"
    assert exits[0]["date"] == "2025-05-01"
    assert exits[0]["price"] == 11.9
    assert result["model_portfolio"]["holdings"] == []
    assert result["holding_snapshot_complete"] is True


def test_signal_status_stays_flat_when_simulated_portfolio_holds_the_symbol(tmp_path) -> None:
    market_store = _store(tmp_path)
    pool = DuckDbStrategyPoolStore(market_store)
    pool.initialize()
    pool.set_manual_override(
        "SH",
        "600000",
        "annual_return",
        {
            "candidate_id": "flat-signal-held-candidate",
            "buy_rule": {
                "signals": [],
                "conditions": [
                    {"left_value": "price.close", "operator": "lt", "right_constant": 0}
                ],
                "required_conditions": [],
                "combine_mode": "all",
            },
            "sell_rule": {
                "signals": [],
                "conditions": [
                    {"left_value": "price.close", "operator": "lt", "right_constant": 0}
                ],
                "required_conditions": [],
                "combine_mode": "all",
            },
            "metrics": {"validation": {"annual_return": 0.2}},
            "config": {},
        },
    )
    adapter = DuckDbMarketDataAdapter(market_store)
    service = MarketStrategyPoolService(
        store=market_store,
        adapter=adapter,
        individual_service=object(),
        backtest_service=BacktestService(adapter=adapter),
        pool_store=pool,
        portfolio_service=PortfolioBacktestService(adapter=adapter),
    )

    def simulated_snapshot(*_args, **_kwargs):
        return {
            "available": True,
            "current_state": {"holdings": [{"market": "SH", "code": "600000", "name": "浦发银行"}]},
            "risk_exit_signals": [],
        }

    service._current_strategy_snapshot = simulated_snapshot

    result = service.run(
        {"mode": "signals", "objective": "annual_return", "market": "SH", "code": "600000"},
        job_id="flat-signal-held-job",
    )

    assert result["states"][0]["status"] == "flat"
    assert result["signal_target"] == {"market": "SH", "code": "600000"}
    assert result["model_portfolio"]["holdings"] == [
        {"market": "SH", "code": "600000", "name": "浦发银行"}
    ]


def test_signal_scan_reports_independent_current_holding(tmp_path) -> None:
    market_store = _store(tmp_path)
    pool = DuckDbStrategyPoolStore(market_store)
    pool.initialize()
    pool.set_manual_override(
        "SH",
        "600000",
        "annual_return",
        {
            "candidate_id": "always-held-candidate",
            "buy_rule": {
                "signals": [],
                "conditions": [
                    {"left_value": "price.close", "operator": "gt", "right_constant": 0}
                ],
                "required_conditions": [],
                "combine_mode": "all",
            },
            "sell_rule": {
                "signals": [],
                "conditions": [
                    {"left_value": "price.close", "operator": "lt", "right_constant": 0}
                ],
                "required_conditions": [],
                "combine_mode": "all",
            },
            "metrics": {"validation": {"annual_return": 0.2}},
            "config": {},
        },
    )
    adapter = DuckDbMarketDataAdapter(market_store)
    service = MarketStrategyPoolService(
        store=market_store,
        adapter=adapter,
        individual_service=object(),
        backtest_service=BacktestService(adapter=adapter),
        pool_store=pool,
        portfolio_service=PortfolioBacktestService(adapter=adapter),
    )

    result = service.run(
        {"mode": "signals", "objective": "annual_return"},
        job_id="single-strategy-hold-job",
    )

    holding = result["model_portfolio"]["holdings"][0]
    assert holding["market"] == "SH"
    assert holding["code"] == "600000"
    assert holding["status"] == "held"


def test_rebuild_is_queued_without_clearing_the_pool_and_is_full_only(tmp_path) -> None:
    app = create_app(
        market_data_store=_store(tmp_path), strategy_worker_token=TOKEN, auto_sync_enabled=False
    )
    app.state.strategy_pool_store.set_manual_override(
        "SH",
        "600000",
        "annual_return",
        {"candidate_id": "old"},
    )
    with TestClient(app) as client:
        invalid = client.post(
            "/api/v1/strategy-pool/jobs",
            json={
                "mode": "single",
                "market": "SH",
                "code": "600000",
                "rebuild": True,
            },
        )
        assert invalid.status_code == 422
        created = client.post("/api/v1/strategy-pool/jobs", json={"mode": "full", "rebuild": True})
        assert created.status_code == 202
        assert app.state.strategy_pool_store.get("SH", "600000") is not None
        history = client.get("/api/v1/strategy-pool/jobs").json()["data"]["items"]
        assert history[0]["rebuild"] is True


def test_page_automatic_requests_do_not_create_tracking_jobs(tmp_path) -> None:
    app = create_app(
        market_data_store=_store(tmp_path), strategy_worker_token=TOKEN, auto_sync_enabled=False
    )
    app.state.strategy_pool_store.set_manual_override(
        "SH", "600000", "annual_return", {"candidate_id": "old"}
    )
    with TestClient(app) as client:
        for goal in ["annual_return", "sharpe"]:
            result = client.post(
                "/api/v1/strategy-pool/jobs",
                json={"mode": "signals", "objective": goal, "automatic": True},
            )
            assert result.status_code == 202
            assert result.json()["data"]["skipped"] is True
        assert client.get("/api/v1/jobs/active").json()["data"] == []


def test_automatic_flag_is_only_valid_for_full_signal_scans(tmp_path) -> None:
    app = create_app(
        market_data_store=_store(tmp_path), strategy_worker_token=TOKEN, auto_sync_enabled=False
    )
    with TestClient(app) as client:
        for payload in [
            {"mode": "full", "automatic": True},
            {
                "mode": "signals",
                "objective": "annual_return",
                "market": "SH",
                "code": "600000",
                "automatic": True,
            },
        ]:
            assert client.post("/api/v1/strategy-pool/jobs", json=payload).status_code == 422


def test_manual_signal_refresh_uses_independent_both_objective_channel(tmp_path) -> None:
    app = create_app(
        market_data_store=_store(tmp_path), strategy_worker_token=TOKEN, auto_sync_enabled=False
    )
    app.state.strategy_pool_store.set_manual_override(
        "SH", "600000", "annual_return", {"candidate_id": "old"}
    )
    app.state.strategy_job_backend.submit(
        "market_strategy_pool",
        {"mode": "full"},
        description="long research",
        metadata={"mode": "full"},
    )
    with TestClient(app) as client:
        result = client.post(
            "/api/v1/strategy-pool/jobs", json={"mode": "signals", "objective": "sharpe"}
        )
        assert result.status_code == 202
        job = app.state.strategy_job_store.read_request(result.json()["data"]["job_id"])
        assert job["operation"] == "market_strategy_pool_tracking"
        assert job["payload"]["objective"] == "sharpe"
        assert len(client.get("/api/v1/jobs/active").json()["data"]) == 2


def test_focus_api_round_trip_and_validation(tmp_path) -> None:
    app = create_app(
        market_data_store=_store(tmp_path),
        strategy_worker_token=TOKEN,
        auto_sync_enabled=False,
    )

    with TestClient(app) as client:
        empty = client.get("/api/v1/strategy-pool/focus")
        assert empty.status_code == 200
        assert empty.json()["data"]["items"] == []

        added = client.post(
            "/api/v1/strategy-pool/focus",
            json={"market": "SH", "code": "600000", "name": "浦发银行"},
        )
        expected = [{"market": "SH", "code": "600000", "name": "浦发银行"}]
        assert added.status_code == 200
        assert added.json()["data"]["items"] == expected
        assert (
            client.post(
                "/api/v1/strategy-pool/focus",
                json={"market": "SH", "code": "600000", "name": "浦发银行"},
            ).json()["data"]["items"]
            == expected
        )
        assert client.get("/api/v1/strategy-pool/focus").json()["data"]["items"] == expected

        assert (
            client.post(
                "/api/v1/strategy-pool/focus", json={"market": "SH", "code": "60000"}
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/v1/strategy-pool/focus",
                json={"market": "SH", "code": "600000", "name": "x", "objective": "sharpe"},
            ).status_code
            == 422
        )
        assert client.get("/api/v1/strategy-pool/focus").json()["data"]["items"] == expected

        unchanged = client.delete("/api/v1/strategy-pool/focus/SZ/600000")
        assert unchanged.json()["data"]["items"] == expected
        assert client.delete("/api/v1/strategy-pool/focus/SH/600000").json()["data"]["items"] == []
        assert client.delete("/api/v1/strategy-pool/focus/BJ/600000").status_code == 422


def test_focus_api_rejects_members_beyond_the_limit(tmp_path) -> None:
    app = create_app(
        market_data_store=_store(tmp_path),
        strategy_worker_token=TOKEN,
        auto_sync_enabled=False,
    )
    pool_store = app.state.strategy_pool_store
    for index in range(FOCUS_LIMIT):
        pool_store.add_focus("SH", f"{index:06d}")

    with TestClient(app) as client:
        rejected = client.post(
            "/api/v1/strategy-pool/focus", json={"market": "SH", "code": "999999"}
        )
        assert rejected.status_code == 422
        assert "特别关注最多" in rejected.json()["error"]["message"]
        assert len(client.get("/api/v1/strategy-pool/focus").json()["data"]["items"]) == FOCUS_LIMIT
