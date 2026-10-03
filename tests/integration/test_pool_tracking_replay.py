import time

import pandas as pd
import pytest

from selector_app.market_data.adapter import DuckDbMarketDataAdapter
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.portfolio_backtest.service import PortfolioBacktestService
from selector_app.strategy_pool.store import DuckDbStrategyPoolStore
from selector_app.strategy_pool.tracking import replay_account
from selector_app.strategy_pool.tracking_store import PoolTrackingStore, signature


def bars(count):
    return pd.DataFrame(
        dict(
            date=pd.date_range("2026-09-01", periods=count, freq="D"),
            open=[10, 10, 12, 15][:count],
            close=[10, 12, 15, 14][:count],
            high=[11, 13, 16, 16][:count],
            low=[9, 9, 11, 13][:count],
            volume=[1000] * count,
            amount=[10000] * count,
            bar_status=["completed"] * count,
        )
    )


def candidate():
    return dict(
        candidate_id="first",
        buy_rule=dict(
            signals=[], conditions=[dict(left_value="price.close", operator="gt", right_constant=0)]
        ),
        sell_rule=dict(
            signals=[], conditions=[dict(left_value="price.close", operator="lt", right_constant=0)]
        ),
        config=dict(
            initial_cash=1000,
            commission=0,
            min_commission=0,
            stamp_tax=0,
            slippage=0,
            execution="next_open",
            stop_loss_pct=0.2,
            trailing_stop_pct=None,
            stop_loss_cooldown_days=2,
        ),
    )


def test_verified_append_replays_only_new_dates_and_matches_full_backtest(tmp_path):
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", bars(2))
    adapter = DuckDbMarketDataAdapter(store)
    service = PortfolioBacktestService(adapter=adapter)
    value = candidate()
    version = dict(
        candidate=value,
        signature=signature(value),
        launch_data_date="2026-09-02",
        created_at="2026-09-02T16:00:00+00:00",
    )
    ref = adapter.stock_ref("", "SH", "600000")
    before = replay_account(service, ref, version)
    assert before["summary"]["created_return"] == pytest.approx(0.2)
    store.replace_local_bars("SH", "600000", "stock", bars(3))
    started = time.perf_counter()
    resumed = replay_account(service, ref, version, before)
    duration = time.perf_counter() - started
    full = replay_account(service, ref, version)
    assert resumed["summary"]["mode"] == "incremental"
    assert resumed["checkpoint"] == full["checkpoint"]
    assert resumed["summary"]["created_return"] == pytest.approx(0.5)
    assert resumed["summary"]["post_launch_return"] == pytest.approx(0.25)
    assert duration < 5
    corrected = bars(3).astype({"close": "float64"})
    corrected.loc[0, "close"] = 9.8
    store.replace_local_bars("SH", "600000", "stock", corrected)
    rebuilt = replay_account(service, ref, version, resumed)
    assert rebuilt["summary"]["mode"] == "rebuilt"
    assert rebuilt["summary"]["recomputed_at"]


def test_snapshot_commit_rejects_archived_versions_and_keeps_last_good_on_failure(tmp_path):
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    pool = DuckDbStrategyPoolStore(store)
    pool.upsert_automatic(
        market="SH",
        code="600000",
        name="test",
        annual_candidate=candidate(),
        sharpe_candidate=None,
        data_fingerprint="a",
        config_fingerprint="b",
        source_job_id="job",
    )
    tracks = PoolTrackingStore(store)
    v = tracks.current_versions()[0]
    snap = {"summary": {"created_return": 0.2, "as_of_date": "2026-09-03"}, "checkpoint": {}}
    assert tracks.save_snapshot(v["version_id"], v["signature"], snap)
    tracks.record_error(v["version_id"], "temporary error")
    assert tracks.snapshot(v["version_id"]) == snap
    pool.persist_worker_entry({"reset": True}, source_job_id="reset")
    assert not tracks.save_snapshot(v["version_id"], v["signature"], snap)


@pytest.mark.parametrize(
    "settings",
    [
        {
            "stop_loss_mode": "atr",
            "stop_loss_pct": None,
            "stop_loss_atr_multiplier": 2,
            "stop_loss_cooldown_days": 2,
        },
        {"trailing_stop_pct": 0.15, "trailing_stop_cooldown_days": 2},
        {"take_profit_pct": 0.1, "take_profit_cooldown_days": 2, "execution": "next_close"},
    ],
)
def test_incremental_preserves_exit_state_and_pending_orders(tmp_path, settings):
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", bars(2))
    adapter = DuckDbMarketDataAdapter(store)
    service = PortfolioBacktestService(adapter=adapter)
    value = candidate()
    value["config"].update(settings)
    version = dict(
        candidate=value,
        signature=signature(value),
        launch_data_date="2026-09-02",
        created_at="2026-09-02T16:00:00+00:00",
    )
    ref = adapter.stock_ref("", "SH", "600000")
    before = replay_account(service, ref, version)
    store.replace_local_bars("SH", "600000", "stock", bars(4))
    resumed = replay_account(service, ref, version, before)
    assert resumed["checkpoint"] == replay_account(service, ref, version)["checkpoint"]


def test_scheduler_trading_day_dedup_and_new_version_initialization(tmp_path):
    from types import SimpleNamespace

    from selector_app.strategy_discovery.duckdb_job_store import DuckDbStrategyJobStore
    from selector_app.strategy_discovery.job_backend import DuckDbStrategyJobBackend
    from selector_app.strategy_pool.tracking_scheduler import PoolTrackingScheduler

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", bars(3))
    pool = DuckDbStrategyPoolStore(store)
    pool.upsert_automatic(
        market="SH",
        code="600000",
        name="test",
        annual_candidate=candidate(),
        sharpe_candidate=candidate(),
        data_fingerprint="a",
        config_fingerprint="b",
        source_job_id="job",
    )
    jobs = DuckDbStrategyJobStore(store)
    backend = DuckDbStrategyJobBackend(
        store, jobs, market_data_adapter=DuckDbMarketDataAdapter(store)
    )
    scheduler = PoolTrackingScheduler(
        store, backend, SimpleNamespace(has_active_jobs=lambda *args: False)
    )
    first = scheduler.submit_if_due()
    assert first
    assert scheduler.submit_if_due() is None
    jobs.update(first, status="failed")
    # Simulate attempted initialization failure: never loop automatically on the same input day.
    tracks = PoolTrackingStore(store)
    for v in tracks.current_versions():
        tracks.record_error(v["version_id"], "failed")
    assert scheduler.submit_if_due() is None
    new = candidate()
    new["config"]["stop_loss_pct"] = 0.1
    pool.upsert_automatic(
        market="SH",
        code="600000",
        name="test",
        annual_candidate=new,
        sharpe_candidate=new,
        data_fingerprint="a",
        config_fingerprint="b",
        source_job_id="new",
    )
    initialization = scheduler.submit_if_due()
    assert initialization
    assert jobs.read_request(initialization)["payload"]["initialization"] is True
    assert len(jobs.read_request(initialization)["payload"]["version_ids"]) == 2


def test_detail_api_is_readonly_and_trade_pages_are_owned_by_symbol(tmp_path):
    from fastapi.testclient import TestClient

    from selector_app.web.app import create_app

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", bars(3))
    app = create_app(
        market_data_store=store, auto_sync_enabled=False, strategy_worker_token="test-token"
    )
    pool = app.state.strategy_pool_store
    pool.upsert_automatic(
        market="SH",
        code="600000",
        name="test",
        annual_candidate=candidate(),
        sharpe_candidate=candidate(),
        data_fingerprint="a",
        config_fingerprint="b",
        source_job_id="job",
    )
    tracks = PoolTrackingStore(store)
    v = tracks.current_versions()[0]
    service = PortfolioBacktestService(adapter=DuckDbMarketDataAdapter(store))
    snapshot = replay_account(service, service._adapter.stock_ref("", "SH", "600000"), v)
    assert tracks.save_snapshot(v["version_id"], v["signature"], snapshot)
    with TestClient(app) as client:
        detail = client.get("/api/v1/strategy-pool/entries/SH/600000/detail")
        assert detail.status_code == 200
        assert len(detail.json()["data"]["objectives"]["annual_return"]) == 1
        url = f"/api/v1/strategy-pool/entries/SH/600000/versions/{v['version_id']}"
        assert client.get(url + "/curve").json()["data"]["backtest"]["equity_curve"]
        page = client.get(url + "/trades?limit=1&offset=0").json()["data"]
        assert len(page["items"]) == 1
        assert client.get(url.replace("600000", "600001") + "/trades").status_code == 404
        assert client.get("/api/v1/jobs/active").json()["data"] == []


def test_tracking_runner_deduplicates_goals_and_uses_shared_budget(tmp_path):
    from selector_app.strategy_discovery.evaluation_budget import EvaluationWorkerBudget
    from selector_app.strategy_pool.tracking_runner import PoolTrackingRunner

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", bars(3))
    pool = DuckDbStrategyPoolStore(store)
    pool.upsert_automatic(
        market="SH",
        code="600000",
        name="test",
        annual_candidate=candidate(),
        sharpe_candidate={"candidate_ref": "annual_return"},
        data_fingerprint="a",
        config_fingerprint="b",
        source_job_id="job",
    )
    tracks = PoolTrackingStore(store)
    budget = EvaluationWorkerBudget(2)

    def write(item):
        if item.get("error"):
            raise AssertionError(item["error"])
        return {
            "saved": tracks.save_snapshot(item["version_id"], item["signature"], item["snapshot"])
        }

    result = PoolTrackingRunner(store, budget).run(
        {"objective": "sharpe", "as_of_date": "2026-09-03"},
        progress=lambda *args: None,
        abort_check=lambda: False,
        writer=write,
    )
    assert result["completed_symbols"] == 2
    assert result["failed_symbols"] == 0
    assert len(result["results_by_objective"]["annual_return"]["states"]) == 1
    assert budget.available_workers == 2
    assert (
        len(
            {
                tracks.snapshot(v["version_id"])["summary"]["checkpoint_hash"]
                for v in tracks.current_versions()
            }
        )
        == 1
    )


@pytest.mark.parametrize("history_days", [120, 2000])
def test_initialization_and_daily_append_different_history_lengths(tmp_path, history_days):
    import time

    import numpy as np

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    dates = pd.date_range("2010-01-01", periods=history_days + 5, freq="B")
    close = 10 + np.arange(history_days + 5) / 1000
    frame = pd.DataFrame(
        dict(
            date=dates,
            open=close,
            close=close,
            high=close + 0.2,
            low=close - 0.2,
            volume=1000.0,
            amount=close * 1000,
            bar_status="completed",
        )
    )
    store.replace_local_bars("SH", "600000", "stock", frame.iloc[:history_days])
    service = PortfolioBacktestService(adapter=DuckDbMarketDataAdapter(store))
    ref = service._adapter.stock_ref("", "SH", "600000")
    c = candidate()
    c["config"].update(initial_cash=1000000, stop_loss_pct=None)
    v = dict(
        candidate=c,
        signature=signature(c),
        created_at="2026-10-03T00:00:00+00:00",
        launch_data_date=dates[history_days - 1].date().isoformat(),
    )
    start = time.perf_counter()
    first = replay_account(service, ref, v)
    init_time = time.perf_counter() - start
    store.replace_local_bars("SH", "600000", "stock", frame)
    start = time.perf_counter()
    delta = replay_account(service, ref, v, first)
    delta_time = time.perf_counter() - start
    assert first["summary"]["processed_new_days"] == history_days
    assert delta["summary"]["processed_new_days"] == 5
    assert delta["checkpoint"] == replay_account(service, ref, v)["checkpoint"]
    print(f"history={history_days} init={init_time:.4f}s append5={delta_time:.4f}s")


def test_cancelled_initialization_is_not_requeued_on_unchanged_input(tmp_path):
    from types import SimpleNamespace

    from selector_app.strategy_discovery.duckdb_job_store import DuckDbStrategyJobStore
    from selector_app.strategy_discovery.job_backend import DuckDbStrategyJobBackend
    from selector_app.strategy_pool.tracking_scheduler import PoolTrackingScheduler

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", bars(3))
    pool = DuckDbStrategyPoolStore(store)
    pool.upsert_automatic(
        market="SH",
        code="600000",
        name="test",
        annual_candidate=candidate(),
        sharpe_candidate=None,
        data_fingerprint="a",
        config_fingerprint="b",
        source_job_id="job",
    )
    jobs = DuckDbStrategyJobStore(store)
    backend = DuckDbStrategyJobBackend(
        store, jobs, market_data_adapter=DuckDbMarketDataAdapter(store)
    )
    scheduler = PoolTrackingScheduler(
        store, backend, SimpleNamespace(has_active_jobs=lambda *args: False)
    )
    first = scheduler.submit_if_due()
    jobs.update(first, status="cancelled")
    assert scheduler.submit_if_due() is None
