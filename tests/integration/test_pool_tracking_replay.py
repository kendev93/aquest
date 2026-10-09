import time

import pandas as pd
import pytest

from selector_app.market_data.adapter import DuckDbMarketDataAdapter
from selector_app.market_data.adjustment import CorporateAction, compute_factor_ratio
from selector_app.market_data.models import StockRef
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.market_data.store_base import RESEARCH_START
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
    legacy_checkpoint = {
        **before["checkpoint"],
        "version": 1,
        "positions": {
            symbol: {
                key: value for key, value in position.items() if key != "applied_corporate_actions"
            }
            for symbol, position in before["checkpoint"]["positions"].items()
        },
    }
    legacy_snapshot = {
        **before,
        "engine": "pool-account-v1",
        "checkpoint": legacy_checkpoint,
    }
    legacy_engine = replay_account(service, ref, version, legacy_snapshot)
    assert legacy_engine["summary"]["mode"] == "rebuilt"
    assert legacy_engine["summary"]["recomputed_at"]
    for identity_key in (
        "baseline_date",
        "baseline_equity",
        "launch_equity",
        "as_of_date",
        "created_return",
        "post_launch_return",
    ):
        assert legacy_engine["summary"][identity_key] == before["summary"][identity_key]
    assert legacy_engine["checkpoint"] == before["checkpoint"]
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


def test_replay_account_refreshes_actions_between_runs_on_a_reused_service(tmp_path):
    dates = pd.date_range("2026-09-01", periods=5, freq="D")
    frame = pd.DataFrame(
        dict(
            date=dates,
            open=[20.0, 20.0, 10.0, 10.0, 10.0],
            close=[20.0, 20.0, 10.0, 10.0, 10.0],
            high=[20.0, 20.0, 10.0, 10.0, 10.0],
            low=[20.0, 20.0, 10.0, 10.0, 10.0],
            volume=[1000.0] * 5,
            amount=[20_000.0, 20_000.0, 10_000.0, 10_000.0, 10_000.0],
            bar_status=["completed"] * 5,
        )
    )

    class MutableActionAdapter:
        actions: tuple[CorporateAction, ...] = ()

        def read_stock(self, ref: StockRef) -> pd.DataFrame:
            return frame.copy(deep=True)

        def read_corporate_actions(self, ref: StockRef):
            return self.actions

    adapter = MutableActionAdapter()
    ref = StockRef(
        market="SH",
        code="600000",
        path=tmp_path / "sh600000.day",
        instrument_type="stock",
        board="main",
    )
    service = PortfolioBacktestService(adapter=adapter)
    selected = candidate()
    selected["config"].update(initial_cash=1_000_000, stop_loss_pct=None)
    version = dict(
        candidate=selected,
        signature=signature(selected),
        launch_data_date="2026-09-01",
        created_at="2026-09-01T16:00:00+00:00",
    )

    before = replay_account(service, ref, version)
    adapter.actions = (
        CorporateAction(
            market="SH",
            code="600000",
            ex_date=dates[2].date(),
            share_ratio=1.0,
            cash_per_share=0.0,
            factor_ratio=compute_factor_ratio(
                prev_close=20.0,
                share_ratio=1.0,
                cash_per_share=0.0,
            ),
            action_type="stock_dividend",
        ),
    )

    refreshed = replay_account(service, ref, version)
    fresh = replay_account(PortfolioBacktestService(adapter=adapter), ref, version)

    assert before["summary"]["equity"] == pytest.approx(500_000.0)
    assert refreshed["summary"]["equity"] == pytest.approx(1_000_000.0)
    assert refreshed["checkpoint"] == fresh["checkpoint"]


def test_buy_rule_stays_true_but_is_reported_blocked_during_risk_cooldown(tmp_path):
    """冷却期内买入规则仍成立，但执行层已否决——必须报出阻断原因。

    回归：2026-10-08 中信证券（SH600030）止损后进入 3 日冷却，面板照旧显示买入
    信号却没有挂单也没有持仓，看起来像"信号丢了"。
    """

    dates = pd.date_range("2026-09-01", periods=5, freq="D")
    frame = pd.DataFrame(
        dict(
            date=dates,
            open=[10.0, 10.0, 10.0, 10.0, 10.0],
            close=[10.0, 10.0, 10.0, 7.0, 7.0],
            high=[11.0, 11.0, 11.0, 11.0, 11.0],
            low=[9.0, 9.0, 9.0, 7.0, 7.0],
            volume=[1000.0] * 5,
            amount=[10_000.0, 10_000.0, 10_000.0, 7_000.0, 7_000.0],
            bar_status=["completed"] * 5,
        )
    )
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", frame)
    adapter = DuckDbMarketDataAdapter(store)
    service = PortfolioBacktestService(adapter=adapter)
    value = candidate()
    value["config"].update(initial_cash=1000, stop_loss_pct=0.2, stop_loss_cooldown_days=2)
    version = dict(
        candidate=value,
        signature=signature(value),
        launch_data_date="2026-09-01",
        created_at="2026-09-01T16:00:00+00:00",
    )
    ref = adapter.stock_ref("", "SH", "600000")
    result = replay_account(service, ref, version)
    summary = result["summary"]
    assert summary["signal_buy"] is True
    assert summary["buy_blocked_reason"] == "cooldown"
    assert summary["holding"] == []
    assert result["checkpoint"]["pending_buys"] == []


def test_held_position_keeps_its_buy_signal_unblocked(tmp_path):
    """持仓中标的的买入信号不算阻断：那只是不加仓，规则事实仍然有效。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", bars(2))
    adapter = DuckDbMarketDataAdapter(store)
    service = PortfolioBacktestService(adapter=adapter)
    value = candidate()
    value["config"].update(initial_cash=1000, stop_loss_pct=None)
    version = dict(
        candidate=value,
        signature=signature(value),
        launch_data_date="2026-09-02",
        created_at="2026-09-02T16:00:00+00:00",
    )
    ref = adapter.stock_ref("", "SH", "600000")
    summary = replay_account(service, ref, version)["summary"]
    assert summary["holding"]
    assert summary["signal_buy"] is True
    assert summary["buy_blocked_reason"] is None


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
    store.replace_local_bars("SH", "600000", "stock", bars(3), name="浦发银行")
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
    annual = result["results_by_objective"]["annual_return"]
    assert len(annual["states"]) == 1
    # 信号行的中文名：买入/卖出行是用跟踪版本字典拼的（只有 market/code），
    # 名字必须从行情库 instruments 表补。states 与 buy/sell 行出自同一个 dict，
    # 覆盖 states 就覆盖了全部三处消费方。
    assert annual["states"][0]["name"] == "浦发银行"
    # 信号面板是规则事实，与持仓互不覆盖：买入规则 close > 0 恒真，首日触发、
    # 次日开盘成交——as_of 当天既持仓、规则又为真，状态仍是「触发买入」，
    # 持仓事实由持仓面板与条目表的持仓中徽章表达（两者并排、不冲突）。
    assert annual["states"][0]["status"] == "buy"
    assert annual["buy_signals"][0]["name"] == "浦发银行"
    # 跟踪路径的持仓需带前端读取的 holding_return_pct（与信号扫描路径口径一致）
    holdings = annual["model_portfolio"]["holdings"]
    assert holdings and isinstance(holdings[0].get("holding_return_pct"), float)
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


def test_tracking_both_rules_firing_show_in_both_panels(tmp_path):
    """买卖规则同时为真 → 两个面板各自展示规则事实，状态为「买卖均触发」。

    时序构造：买入 close > 0 恒真，首日触发、次日开盘成交；卖出 close > 13
    只在第三根为真（第二根为假，保证卖出不会在第三根开盘抢先平仓）。于是
    as_of 第三根：持仓中、买卖规则同时成立 → 状态「买卖均触发」，两个面板
    都有它，且当天仍列持仓中（动作都按次一交易日执行）。
    """
    from selector_app.strategy_discovery.evaluation_budget import EvaluationWorkerBudget
    from selector_app.strategy_pool.tracking_runner import PoolTrackingRunner

    store = DuckDbMarketDataStore(tmp_path / "sell-gate.duckdb")
    store.replace_local_bars("SH", "600000", "stock", bars(3), name="浦发银行")
    pool = DuckDbStrategyPoolStore(store)
    both = candidate()
    both["sell_rule"] = dict(
        signals=[], conditions=[dict(left_value="price.close", operator="gt", right_constant=13)]
    )
    pool.upsert_automatic(
        market="SH",
        code="600000",
        name="test",
        annual_candidate=both,
        sharpe_candidate={"candidate_ref": "annual_return"},
        data_fingerprint="a",
        config_fingerprint="b",
        source_job_id="job",
    )
    tracks = PoolTrackingStore(store)

    def write(item):
        if item.get("error"):
            raise AssertionError(item["error"])
        return {
            "saved": tracks.save_snapshot(item["version_id"], item["signature"], item["snapshot"])
        }

    result = PoolTrackingRunner(store, EvaluationWorkerBudget(2)).run(
        {"objective": "annual_return", "as_of_date": "2026-09-03"},
        progress=lambda *args: None,
        abort_check=lambda: False,
        writer=write,
    )
    annual = result["results_by_objective"]["annual_return"]
    assert annual["states"][0]["status"] == "buy_sell"
    assert [row["symbol"] for row in annual["sell_signals"]] == ["SH600000"]
    assert [row["symbol"] for row in annual["buy_signals"]] == ["SH600000"]
    # 两个动作都按次一交易日执行，当天仍列持仓中。
    assert [f"{h['market']}{h['code']}" for h in annual["model_portfolio"]["holdings"]] == [
        "SH600000"
    ]


def test_partial_tracking_projects_only_applied_versions_for_both_objectives(tmp_path):
    from selector_app.strategy_discovery.evaluation_budget import EvaluationWorkerBudget
    from selector_app.strategy_pool.tracking_runner import PoolTrackingRunner

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", bars(3))
    pool = DuckDbStrategyPoolStore(store)
    automatic = candidate()
    manual = candidate()
    manual["candidate_id"] = "manual"
    pool.upsert_automatic(
        market="SH",
        code="600000",
        name="test",
        annual_candidate=automatic,
        sharpe_candidate=automatic,
        data_fingerprint="a",
        config_fingerprint="b",
        source_job_id="job",
    )
    pool.set_manual_override("SH", "600000", "annual_return", manual)
    tracks = PoolTrackingStore(store)
    versions = tracks.current_versions()
    manual_annual = next(
        v for v in versions if v["objective"] == "annual_return" and v["source"] == "manual"
    )
    automatic_annual = next(
        v for v in versions if v["objective"] == "annual_return" and v["source"] == "automatic"
    )
    automatic_sharpe = next(
        v for v in versions if v["objective"] == "sharpe" and v["source"] == "automatic"
    )

    def save_existing(v, label: str) -> None:
        snapshot = {
            "summary": {
                "as_of_date": "2026-09-03",
                "holding": [
                    {
                        "market": "SH",
                        "code": "600000",
                        "symbol": "SH600000",
                        "entry_date": "2026-09-02",
                        "entry_price": 12.0,
                        "close": 12.0,
                        "label": label,
                    }
                ],
                "signal_buy": False,
                "signal_sell": False,
                "risk_exits": [],
            },
            "checkpoint": {},
        }
        assert tracks.save_snapshot(v["version_id"], v["signature"], snapshot)

    save_existing(automatic_annual, "stale automatic annual")
    save_existing(automatic_sharpe, "automatic sharpe")
    save_existing(manual_annual, "manual annual")

    def write(item):
        if item.get("error"):
            raise AssertionError(item["error"])
        return {
            "saved": tracks.save_snapshot(item["version_id"], item["signature"], item["snapshot"])
        }

    progress_events = []
    result = PoolTrackingRunner(store, EvaluationWorkerBudget(2)).run(
        {
            "objective": "annual_return",
            "as_of_date": "2026-09-03",
            "version_ids": [manual_annual["version_id"]],
        },
        progress=lambda *args: progress_events.append(args),
        abort_check=lambda: False,
        writer=write,
    )
    by_objective = result["results_by_objective"]
    annual = by_objective["annual_return"]
    sharpe = by_objective["sharpe"]

    assert result["completed_symbols"] == 1
    assert result["failed_symbols"] == 0
    assert result["total_symbols"] == 1
    assert result["pool_total_symbols"] == 3
    assert progress_events[-1][0:2] == (1, 1)
    assert len(annual["states"]) == 1
    assert annual["states"][0]["tracking_version_id"] == manual_annual["version_id"]
    assert len(annual["model_portfolio"]["holdings"]) == 1
    assert annual["model_portfolio"]["holdings"][0]["code"] == "600000"
    assert len(sharpe["states"]) == 1
    assert sharpe["states"][0]["tracking_version_id"] == automatic_sharpe["version_id"]
    assert len(sharpe["model_portfolio"]["holdings"]) == 1
    assert sharpe["model_portfolio"]["holdings"][0]["label"] == "automatic sharpe"

    skipped = PoolTrackingRunner(store, EvaluationWorkerBudget(2)).run(
        {
            "objective": "annual_return",
            "as_of_date": "2026-09-03",
            "version_ids": [manual_annual["version_id"]],
        },
        progress=lambda *args: None,
        abort_check=lambda: False,
        writer=lambda item: {"saved": False},
    )
    assert skipped["completed_symbols"] == 0
    assert skipped["skipped_symbols"] == 1
    assert skipped["failed_symbols"] == 0
    assert skipped["total_symbols"] == 1
    assert skipped["pool_total_symbols"] == 3
    assert skipped["results_by_objective"]["annual_return"]["states"] == []
    assert len(skipped["results_by_objective"]["sharpe"]["states"]) == 1


def test_tracking_group_accounts_each_version_when_one_writer_call_fails(tmp_path, caplog):
    from selector_app.strategy_discovery.evaluation_budget import EvaluationWorkerBudget
    from selector_app.strategy_pool.tracking_runner import PoolTrackingRunner

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", bars(3))
    pool = DuckDbStrategyPoolStore(store)
    item = candidate()
    pool.upsert_automatic(
        market="SH",
        code="600000",
        name="test",
        annual_candidate=item,
        sharpe_candidate=item,
        data_fingerprint="a",
        config_fingerprint="b",
        source_job_id="job",
    )
    tracks = PoolTrackingStore(store)
    versions = tracks.current_versions()
    assert len(versions) == 2
    progress_events = []
    calls = []

    def writer(value):
        calls.append(value)
        if value.get("error"):
            raise OSError("failure-state acknowledgement unavailable")
        if len([item for item in calls if "snapshot" in item]) == 2:
            raise RuntimeError("second acknowledgement failed")
        return {"saved": True}

    budget = EvaluationWorkerBudget(1)
    result = PoolTrackingRunner(store, budget).run(
        {"version_ids": [version["version_id"] for version in versions]},
        progress=lambda *args: progress_events.append(args),
        abort_check=lambda: False,
        writer=writer,
    )

    assert result["total_symbols"] == 2
    assert result["pool_total_symbols"] == 2
    assert result["completed_symbols"] == 1
    assert result["failed_symbols"] == 1
    assert result["skipped_symbols"] == 0
    assert progress_events[-1][0:2] == (2, 2)
    assert budget.available_workers == 1
    assert "无法记录策略追踪失败状态" in caplog.text


def test_tracking_empty_selection_does_not_lease_worker(tmp_path):
    from selector_app.strategy_discovery.evaluation_budget import EvaluationWorkerBudget
    from selector_app.strategy_pool.tracking_runner import PoolTrackingRunner

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    pool = DuckDbStrategyPoolStore(store)
    item = candidate()
    pool.upsert_automatic(
        market="SH",
        code="600000",
        name="test",
        annual_candidate=item,
        sharpe_candidate=item,
        data_fingerprint="a",
        config_fingerprint="b",
        source_job_id="job",
    )
    budget = EvaluationWorkerBudget(1)
    progress_events = []

    result = PoolTrackingRunner(store, budget).run(
        {"version_ids": []},
        progress=lambda *args: progress_events.append(args),
        abort_check=lambda: False,
        writer=lambda _item: {"saved": True},
    )

    assert result["total_symbols"] == 0
    assert result["pool_total_symbols"] == 2
    assert result["completed_symbols"] == 0
    assert result["failed_symbols"] == 0
    assert budget.available_workers == 1
    assert progress_events[-1][0:2] == (0, 0)


def test_tracking_runner_marks_failed_selected_version_incomplete(tmp_path):
    from selector_app.strategy_discovery.evaluation_budget import EvaluationWorkerBudget
    from selector_app.strategy_pool.tracking_runner import PoolTrackingRunner

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
    selected = tracks.current_versions()[0]
    written_errors = []

    def write(item):
        if item.get("error"):
            written_errors.append(item)
            return None
        return {
            "saved": tracks.save_snapshot(item["version_id"], item["signature"], item["snapshot"])
        }

    result = PoolTrackingRunner(store, EvaluationWorkerBudget(1)).run(
        {"objective": "annual_return", "version_ids": [selected["version_id"]]},
        progress=lambda *args: None,
        abort_check=lambda: False,
        writer=write,
    )

    assert result["completed_symbols"] == 0
    assert result["failed_symbols"] == 1
    assert result["results_by_objective"]["annual_return"]["holding_snapshot_complete"] is False
    assert len(written_errors) == 1
    assert written_errors[0]["version_id"] == selected["version_id"]


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
    # 回放窗口从 RESEARCH_START 起跑：跨窗口的历史只统计窗口内根数；
    # 整体早于研究窗口的历史（history_days=120）没有预热可用，保持原窗口。
    window_bars = int((dates[:history_days] >= pd.Timestamp(RESEARCH_START)).sum())
    assert first["summary"]["processed_new_days"] == (window_bars or history_days)
    assert delta["summary"]["processed_new_days"] == 5
    assert delta["checkpoint"] == replay_account(service, ref, v)["checkpoint"]
    print(f"history={history_days} init={init_time:.4f}s append5={delta_time:.4f}s")


def test_replay_window_starts_at_research_start_with_prior_bars_as_warmup(tmp_path):
    """详情页回放窗口与研究对象对齐：RESEARCH_START 起跑，之前只作指标预热。

    回归：2026-10-09 详情页「全历史」曲线从本地首根日线起画且含 2015 年
    「冷指标」交易，而研究侧明确把 2015 当预热段不算样本——同一段行情
    两套口径矛盾。
    """
    import numpy as np

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    dates = pd.date_range("2015-01-05", periods=300, freq="B")  # 2015-01-05 → 2016-03 前后
    close = 10.0 + np.arange(300) / 100
    frame = pd.DataFrame(
        dict(
            date=dates,
            open=close,
            close=close,
            high=close + 0.1,
            low=close - 0.1,
            volume=1000.0,
            amount=close * 1000,
            bar_status="completed",
        )
    )
    store.replace_local_bars("SH", "600000", "stock", frame)
    adapter = DuckDbMarketDataAdapter(store)
    service = PortfolioBacktestService(adapter=adapter)
    ref = adapter.stock_ref("", "SH", "600000")
    value = candidate()
    # 资金必须够买一手（100 股）：窗口首日 open ≈ 12.6，1000 元会被整手规则拒单。
    value["config"].update(initial_cash=100_000, stop_loss_pct=None)
    version = dict(
        candidate=value,
        signature=signature(value),
        launch_data_date=dates[-1].date().isoformat(),
        created_at="2026-10-09T16:00:00+00:00",
    )
    result = replay_account(service, ref, version)
    window_start = next(day for day in dates if day.date() >= RESEARCH_START)
    start_text = window_start.strftime("%Y-%m-%d")
    equity_dates = [str(row["date"]) for row in result["checkpoint"]["equity"]]
    # 曲线恰好从研究窗口首根日线开始，2015 段不进窗口；之后每根都在。
    window_bars = [day for day in dates if day.date() >= RESEARCH_START]
    assert equity_dates[0] == start_text
    assert len(equity_dates) == len(window_bars)
    assert all(day >= start_text for day in equity_dates)
    # 2015 年没有任何交易；买入规则 close>0 恒真，窗口首日即触发、次日成交。
    trade_dates = [str(row["date"]) for row in result["checkpoint"]["trades"]]
    assert trade_dates
    assert all(day >= start_text for day in trade_dates)


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
