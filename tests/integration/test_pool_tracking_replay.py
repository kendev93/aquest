import time

import pandas as pd
import pytest

from aquest.market_data.adapter import DuckDbMarketDataAdapter
from aquest.market_data.adjustment import CorporateAction, compute_factor_ratio
from aquest.market_data.models import StockRef
from aquest.market_data.store import DuckDbMarketDataStore
from aquest.market_data.store_base import RESEARCH_START
from aquest.portfolio_backtest.service import PortfolioBacktestService
from aquest.strategy_pool.store import DuckDbStrategyPoolStore
from aquest.strategy_pool.tracking import replay_account
from aquest.strategy_pool.tracking_store import PoolTrackingStore, signature


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
    # v2 盐是「风控当日按触发价平仓」的口径，成交日一改整批账户必须重建：留着它
    # 就等于让新代码在旧口径的账户上续跑，混出口一份回测两种成交日。
    recalibrated = replay_account(service, ref, version, {**before, "engine": "pool-account-v2"})
    assert recalibrated["summary"]["mode"] == "rebuilt"
    assert recalibrated["checkpoint"] == before["checkpoint"]
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


def test_summary_reports_post_launch_days_and_benchmark(tmp_path):
    """回放摘要带上线后窗口字段，供「上线后衰减」判定（2026-10-09）。

    上线日 09-02：上线后只有 09-03 一根 bar；基准 = 09-02 收盘到最新收盘。
    没有上线时间的版本没有窗口，两字段为 0 / None。
    """

    store = DuckDbMarketDataStore(tmp_path / "post-launch.duckdb")
    store.replace_local_bars("SH", "600000", "stock", bars(3))
    adapter = DuckDbMarketDataAdapter(store)
    service = PortfolioBacktestService(adapter=adapter)
    ref = adapter.stock_ref("", "SH", "600000")
    value = candidate()
    value["config"].update(initial_cash=1_000_000, stop_loss_pct=None)
    version = dict(
        candidate=value,
        signature=signature(value),
        launch_data_date="2026-09-02",
        created_at="2026-09-02T16:00:00+00:00",
    )
    summary = replay_account(service, ref, version)["summary"]
    assert summary["post_launch_days"] == 1
    assert summary["post_launch_benchmark_return"] == pytest.approx(0.25)

    legacy = dict(version, launch_data_date=None)
    legacy_summary = replay_account(service, ref, legacy)["summary"]
    assert legacy_summary["post_launch_days"] == 0
    assert legacy_summary["post_launch_benchmark_return"] is None


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


def test_risk_exit_stays_open_on_the_trigger_day_and_closes_the_next(tmp_path):
    """触发当日仍持仓、次日开盘成交——跟踪面板只能显示用户做得出的动作。

    当日 OHLC 要收盘后才算得出来，盘中那一下触碰既不知道也来不及挂单，所以模拟
    里也不许按触发价平仓。冷却同样锚在成交日，不是触碰日。
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

    # 回放到 09-04（当日最低 7.0 跌破 8.0 的止损线）就停：只剩挂单，没有成交。
    trigger = replay_account(service, ref, dict(version, target_data_date="2026-09-04"))
    assert trigger["summary"]["as_of_date"] == "2026-09-04"
    held = trigger["summary"]["holding"]
    assert [row["code"] for row in held] == ["600000"]
    assert held[0]["pending_exit_reason"] == "止损"
    assert held[0]["pending_exit_risk_trigger"] == "stop_loss"
    assert held[0]["pending_exit_signal_date"] == "2026-09-04"
    assert held[0]["stop_loss_price"] == pytest.approx(8.0)
    assert held[0]["risk_level_as_of"] == "2026-09-04"
    assert held[0]["max_hold_days"] is None
    assert held[0]["take_profit_price"] is None
    assert trigger["summary"]["risk_exits"] == []
    assert trigger["checkpoint"]["cooldown_until"] == {}
    pending = trigger["checkpoint"]["pending_sells"]["SH600000"]
    assert pending["reason"] == "止损"
    assert pending["risk_trigger"] == "stop_loss"
    assert pending["cooldown_days"] == 2
    assert pending["signal_date"] == 20260904

    # 补上 09-05：开盘成交，冷却从这一根的 bar 序号起算。
    closed = replay_account(service, ref, version)
    assert closed["summary"]["holding"] == []
    sells = [row for row in closed["checkpoint"]["trades"] if row["direction"] == "SELL"]
    assert len(sells) == 1
    assert sells[0]["signal_date"] == "2026-09-04"
    assert sells[0]["date"] == "2026-09-05"
    assert sells[0]["price"] == 10.0
    assert sells[0]["risk_trigger"] == "stop_loss"
    assert sells[0]["cooldown_days"] == 2
    assert closed["summary"]["risk_exits"][0]["signal_date"] == "2026-09-04"
    # 成交日是第 5 根（下标 4），冷却 2 根 → 到 4 + 2 + 1 = 7 才解禁。
    assert closed["checkpoint"]["cooldown_until"] == {"SH600000": 7}


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


def test_tracking_runner_batches_snapshots_and_falls_back_when_the_batch_fails(tmp_path):
    """扫描里的快照按批上报；整批被拒的那一只从 completed 退回 skipped。

    逐只写库实测 15.8 ms，4,409 只需要约 70 秒串行写库；批量上报让后端一个事务写完。
    这里钉住：一次扫描只发一批、被拒的条目计数不重复、批量通道失败时退回逐只写。
    """

    from aquest.strategy_discovery.evaluation_budget import EvaluationWorkerBudget
    from aquest.strategy_pool.tracking_runner import PoolTrackingRunner

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    codes = ("600000", "600001", "600002")
    for code in codes:
        store.replace_local_bars("SH", code, "stock", bars(3))
    pool = DuckDbStrategyPoolStore(store)
    for code in codes:
        pool.upsert_automatic(
            market="SH",
            code=code,
            name="test",
            annual_candidate=candidate(),
            sharpe_candidate=None,
            data_fingerprint="a",
            config_fingerprint="b",
            source_job_id="job",
        )
    tracks = PoolTrackingStore(store)

    def payload() -> dict:
        return {"objective": "annual_return", "as_of_date": "2026-09-03"}

    batches: list[list[str]] = []

    def batch_write(items):
        batches.append([item["version_id"] for item in items])
        results = []
        for index, item in enumerate(items):
            signature = "stale-signature" if index == 1 else item["signature"]
            results.append(
                {
                    "saved": tracks.save_snapshots(
                        [(item["version_id"], signature, item["snapshot"])]
                    )[0]
                }
            )
        return results

    result = PoolTrackingRunner(store, EvaluationWorkerBudget(2)).run(
        payload(),
        progress=lambda *args: None,
        abort_check=lambda: False,
        writer=lambda item: (_ for _ in ()).throw(AssertionError("不该逐只写")),
        batch_writer=batch_write,
    )

    assert len(batches) == 1 and len(batches[0]) == 3
    assert result["completed_symbols"] == 2
    assert result["skipped_symbols"] == 1
    assert result["failed_symbols"] == 0
    rejected = batches[0][1]
    assert tracks.snapshot(rejected) is None
    saved = [tracks.snapshot(v) for v in batches[0] if v != rejected]
    assert all(item is not None for item in saved)

    # 批量通道整体失败：退回逐只写，结果一只能不少。
    # 先修正同一天的数据，让修订号变化，避免命中「行情没变就复用快照」的短路。
    corrected = bars(3)
    corrected.loc[2, ["close", "high", "low"]] = [16.0, 17.0, 13.0]
    for code in codes:
        store.replace_local_bars("SH", code, "stock", corrected)
    fallback: list[str] = []

    def broken_batch(_items):
        raise RuntimeError("backend hiccup")

    def write_one(item):
        fallback.append(item["version_id"])
        return {
            "saved": tracks.save_snapshots(
                [(item["version_id"], item["signature"], item["snapshot"])]
            )[0]
        }

    result = PoolTrackingRunner(store, EvaluationWorkerBudget(2)).run(
        payload(),
        progress=lambda *args: None,
        abort_check=lambda: False,
        writer=write_one,
        batch_writer=broken_batch,
    )
    assert len(fallback) == 3
    assert result["completed_symbols"] == 3
    assert result["skipped_symbols"] == 0


def test_tracking_runner_keeps_version_status_when_snapshot_transport_is_transient(tmp_path):
    """后端繁忙/网络抖动只是快照没落库：本轮信号照常产出，版本状态不能被标成 failed。"""

    from aquest.strategy_discovery.evaluation_budget import EvaluationWorkerBudget
    from aquest.strategy_discovery.worker_api_client import TransientWorkerApiError
    from aquest.strategy_pool.tracking_runner import PoolTrackingRunner

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
    tracks = PoolTrackingStore(store)
    snapshot_attempts: list[dict] = []
    errors: list[dict] = []

    def write_one(item):
        if "error" in item:
            errors.append(item)
            return {"saved": False}
        snapshot_attempts.append(item)
        raise TransientWorkerApiError("策略 worker API 响应超时或暂不可用")

    result = PoolTrackingRunner(store, EvaluationWorkerBudget(2)).run(
        {"objective": "annual_return", "as_of_date": "2026-09-03"},
        progress=lambda *args: None,
        abort_check=lambda: False,
        writer=write_one,
    )

    assert len(snapshot_attempts) == 1
    assert errors == []
    assert result["failed_symbols"] == 0
    assert result["skipped_symbols"] == 1
    assert result["holding_snapshot_complete"] is True
    assert any(row["symbol"] == "SH600000" for row in result["states"])
    assert tracks.current_versions()[0]["status"] == "pending"


def test_tracking_runner_reuses_snapshot_until_market_data_changes(tmp_path):
    """同一行情日、同一份数据不重放；同日数据被修正（修订号变化）必须重放。"""

    from aquest.strategy_discovery.evaluation_budget import EvaluationWorkerBudget
    from aquest.strategy_pool.tracking_runner import PoolTrackingRunner

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
    tracks = PoolTrackingStore(store)
    writes: list[dict] = []

    def save_one(item):
        writes.append(item)
        saved = tracks.save_snapshot(item["version_id"], item["signature"], item["snapshot"])
        return {"saved": saved}

    def run_once(writer) -> dict:
        return PoolTrackingRunner(store, EvaluationWorkerBudget(2)).run(
            {"objective": "annual_return", "as_of_date": "2026-09-03"},
            progress=lambda *args: None,
            abort_check=lambda: False,
            writer=writer,
        )

    first = run_once(save_one)
    assert first["completed_symbols"] == 1
    assert len(writes) == 1
    version = tracks.current_versions()[0]
    assert version["summary"]["engine_version"] == "pool-account-v5"
    assert version["summary"]["data_revision"]
    assert version["summary"]["as_of_date"] == "2026-09-03"

    # 数据没变：第二轮直接复用上一份快照，不再逐只回放/落库。
    writes.clear()
    second = run_once(save_one)
    assert second["completed_symbols"] == 1
    assert second["skipped_symbols"] == 0
    assert second["failed_symbols"] == 0
    assert writes == []
    assert any(row["symbol"] == "SH600000" for row in second["states"])

    # 同一天的数据被修正：修订号变了，必须重放并写回新快照。
    corrected = bars(3)
    corrected.loc[2, ["close", "high", "low"]] = [16.0, 17.0, 13.0]
    store.replace_local_bars("SH", "600000", "stock", corrected)
    writes.clear()
    # 回放要读检查点，但按窗口成批取，不再一只一条连接。
    single_reads = {"count": 0}
    single_snapshot = tracks.snapshot

    def counting_snapshot(version_id: str) -> dict:
        single_reads["count"] += 1
        return single_snapshot(version_id)

    tracks.snapshot = counting_snapshot  # type: ignore[method-assign]
    third = run_once(save_one)
    assert single_reads["count"] == 0
    assert third["completed_symbols"] == 1
    assert len(writes) == 1
    assert (
        tracks.current_versions()[0]["summary"]["data_revision"]
        != version["summary"]["data_revision"]
    )


def test_batch_snapshot_read_matches_single_reads(tmp_path):
    """成批取检查点与逐只取结果一致，不存在的 id 直接漏掉。"""

    import json
    import zlib

    from aquest.strategy_pool.tracking_store import PoolTrackingStore

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    tracks = PoolTrackingStore(store)
    payloads = {
        "v1": {"summary": {"as_of_date": "2026-09-03"}, "checkpoint": {"cash": 1.0}},
        "v2": {"summary": {"as_of_date": "2026-09-03"}, "checkpoint": {"cash": 2.0}},
    }
    with store.state_write_transaction() as connection:
        for version_id, payload in payloads.items():
            connection.execute(
                """
                INSERT INTO pool_tracking_versions (
                    version_id, market, code, objective, source, signature,
                    candidate_json, created_at, is_current, summary_json, snapshot,
                    status, last_error, updated_at
                ) VALUES (?, 'SH', '600000', 'annual_return', 'automatic', 'sig',
                          '{}', '2026-09-01T00:00:00', TRUE, '{}', ?, 'ready', '',
                          '2026-09-03T00:00:00')
                """,
                [version_id, zlib.compress(json.dumps(payload).encode())],
            )

    batched = tracks.snapshots(["v1", "v2", "missing"])

    assert set(batched) == {"v1", "v2"}
    assert batched["v1"] == tracks.snapshot("v1")
    assert batched["v2"] == payloads["v2"]
    assert tracks.snapshots([]) == {}


def test_a_fully_reused_signal_scan_still_projects_every_symbol(tmp_path):
    """行情没变、整轮都命中复用快照时，仍要把每只标的都写进结果。

    复用路径不提交任何后台任务；以前「没有在跑的任务」被当成「扫完了」，只扫到第一组
    就停，周末的信号面板于是只剩一只标的、其余全部显示「信号待刷新」（2026-10-25 实测）。
    """

    from aquest.strategy_discovery.evaluation_budget import EvaluationWorkerBudget
    from aquest.strategy_pool.tracking_runner import PoolTrackingRunner

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    pool = DuckDbStrategyPoolStore(store)
    tracks = PoolTrackingStore(store)
    for market, code in (("SH", "600000"), ("SZ", "000001")):
        store.replace_local_bars(market, code, "stock", bars(3))
        pool.upsert_automatic(
            market=market,
            code=code,
            name=code,
            annual_candidate=candidate(),
            sharpe_candidate=None,
            data_fingerprint="a",
            config_fingerprint="b",
            source_job_id="job",
        )

    def save_one(item):
        return {
            "saved": tracks.save_snapshot(item["version_id"], item["signature"], item["snapshot"])
        }

    def run_once() -> dict:
        return PoolTrackingRunner(store, EvaluationWorkerBudget(2)).run(
            {"objective": "annual_return", "as_of_date": "2026-09-03"},
            progress=lambda *args: None,
            abort_check=lambda: False,
            writer=save_one,
        )

    first = run_once()
    assert first["completed_symbols"] == 2

    # 数据没变：第二轮整轮复用上一份快照，也要把两只都投影出来，并且不再读检查点
    # （摘要里已经有上一轮算好的字段，解压一条检查点要 10 ms，全市场就是 40 秒）。
    reads = {"count": 0}
    snapshot = tracks.snapshot

    def counting(version_id: str) -> dict:
        reads["count"] += 1
        return snapshot(version_id)

    tracks.snapshot = counting  # type: ignore[method-assign]
    second = run_once()
    assert reads["count"] == 0
    assert second["completed_symbols"] == 2
    assert {row["symbol"] for row in second["states"]} == {"SH600000", "SZ000001"}
    assert second["states"] == first["states"]


def test_scheduler_trading_day_dedup_and_new_version_initialization(tmp_path):
    from types import SimpleNamespace

    from aquest.strategy_discovery.duckdb_job_store import DuckDbStrategyJobStore
    from aquest.strategy_discovery.job_backend import DuckDbStrategyJobBackend
    from aquest.strategy_pool.tracking_scheduler import PoolTrackingScheduler

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
        store,
        backend,
        SimpleNamespace(has_active_jobs=lambda *args: False),
        # 这个用例验证「按交易日去重」与「新版本触发初始化」，不看最短间隔，所以关掉它；
        # 间隔本身由下一个用例单独钉住。
        min_interval_seconds=0,
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


def test_scheduler_does_not_rescan_the_same_day_more_often_than_the_interval(tmp_path):
    """研究跑着的时候每完成一只都会产生新版本，不能因此每几分钟就重扫一遍全市场。

    2026-10-19 实测：研究期间自动扫描每 4 分钟一次、每次 53–164 秒扫 4,409 只，一天 94 次，
    跟踪通道被占满，手动「刷新当天信号」只能排队。研究期间的新版本汇总到下一个半小时窗口
    再扫就够；手动刷新走接口，不受这个下限限制。
    """

    from types import SimpleNamespace

    from aquest.strategy_discovery.duckdb_job_store import DuckDbStrategyJobStore
    from aquest.strategy_discovery.job_backend import DuckDbStrategyJobBackend
    from aquest.strategy_pool.tracking_scheduler import PoolTrackingScheduler

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
    assert scheduler.submit_if_due()

    # 研究又完成一只：新版本出现，但离上一次扫描还不到半小时，先不扫。
    newer = candidate()
    newer["config"]["stop_loss_pct"] = 0.1
    pool.upsert_automatic(
        market="SH",
        code="600001",
        name="新标的",
        annual_candidate=newer,
        sharpe_candidate=newer,
        data_fingerprint="a",
        config_fingerprint="b",
        source_job_id="job-2",
    )
    assert scheduler.submit_if_due() is None


def test_detail_api_is_readonly_and_trade_pages_are_owned_by_symbol(tmp_path):
    from fastapi.testclient import TestClient

    from aquest.web.app import create_app

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


def test_trade_pages_default_to_the_latest_settlement_first(tmp_path):
    """详情页的历史交易默认最近成交在前：打开详情关心的是刚发生的买卖，不是第一笔。

    分页由后端按 offset 切片，所以翻转必须发生在切片之前；前端只反转返回的
    items 会让第 2 页变成「次旧的 50 笔倒过来」，全局顺序就错了。
    """
    from fastapi.testclient import TestClient

    from aquest.web.app import create_app

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", bars(3))
    app = create_app(
        market_data_store=store, auto_sync_enabled=False, strategy_worker_token="test-token"
    )
    app.state.strategy_pool_store.upsert_automatic(
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
    version = tracks.current_versions()[0]
    stored = [
        {"date": "2026-01-05", "direction": "BUY", "price": 10.0, "size": 100},
        {"date": "2026-03-11", "direction": "SELL", "price": 12.0, "size": 100},
        {"date": "2026-03-11", "direction": "BUY", "price": 13.0, "size": 50},
        {"date": "2026-05-20", "direction": "BUY", "price": 14.0, "size": 80},
    ]
    snapshot = {
        "summary": {
            "created_return": 0.2,
            "as_of_date": "2026-05-20",
            "baseline_date": "2026-01-01",
        },
        "checkpoint": {"trades": stored, "equity": [], "positions": {}},
    }
    assert tracks.save_snapshot(version["version_id"], version["signature"], snapshot)
    url = f"/api/v1/strategy-pool/entries/SH/600000/versions/{version['version_id']}/trades"

    with TestClient(app) as client:
        newest_first = client.get(f"{url}?scope=full").json()["data"]
        oldest_first = client.get(f"{url}?scope=full&order=asc").json()["data"]
        second_page = client.get(f"{url}?scope=full&limit=1&offset=1").json()["data"]

    assert [t["date"] for t in newest_first["items"]] == [
        "2026-05-20",
        "2026-03-11",
        "2026-03-11",
        "2026-01-05",
    ]
    # 同一天的成交不颠倒：稳定排序保留落库时的成交先后（先平仓、后开新仓）。
    assert [t["direction"] for t in newest_first["items"][1:3]] == ["SELL", "BUY"]
    assert [t["date"] for t in oldest_first["items"]] == [
        "2026-01-05",
        "2026-03-11",
        "2026-03-11",
        "2026-05-20",
    ]
    assert oldest_first["items"][1]["direction"] == "SELL"
    assert second_page["total"] == 4
    assert [t["date"] for t in second_page["items"]] == ["2026-03-11"]


def test_entries_project_the_post_launch_window_for_the_leaderboard(tmp_path):
    """/entries 把上线后窗口逐字段投影到每一行，收益榜才不用为一次排名解压每版的快照。

    榜和详情页必须读同一份 ``summary_json``：数字对不上就没有比较的意义。另一个方向
    同样重要——还没跑过跟踪的标的必须留空而不是给 0，否则「没数据」会被排成「平盘」。
    """
    from fastapi.testclient import TestClient

    from aquest.web.app import create_app

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", bars(3))
    store.replace_local_bars("SZ", "000001", "stock", bars(2))
    app = create_app(
        market_data_store=store, auto_sync_enabled=False, strategy_worker_token="test-token"
    )
    pool = app.state.strategy_pool_store
    for market, code in (("SH", "600000"), ("SZ", "000001")):
        pool.upsert_automatic(
            market=market,
            code=code,
            name="test",
            annual_candidate=candidate(),
            sharpe_candidate=candidate(),
            data_fingerprint="a",
            config_fingerprint="b",
            source_job_id="job",
        )
    tracks = PoolTrackingStore(store)
    version = next(v for v in tracks.current_versions() if v["code"] == "600000")
    assert tracks.save_snapshot(
        version["version_id"],
        version["signature"],
        {
            "summary": {
                "created_return": 0.4,
                "post_launch_return": 0.25,
                "post_launch_benchmark_return": 0.05,
                "post_launch_days": 12,
                "has_post_launch_data": True,
                "as_of_date": "2026-09-03",
            },
            "checkpoint": {"trades": [], "equity": [], "positions": {}},
        },
    )

    with TestClient(app) as client:
        items = client.get("/api/v1/strategy-pool/entries?objective=annual_return").json()["data"][
            "items"
        ]

    tracked = next(item for item in items if item["code"] == "600000")
    assert tracked["created_return"] == 0.4
    assert tracked["post_launch_return"] == 0.25
    assert tracked["post_launch_benchmark_return"] == 0.05
    assert tracked["post_launch_days"] == 12
    assert tracked["post_launch_has_data"] is True
    assert tracked["tracking_as_of_date"] == "2026-09-03"
    # 现价来自行情本身，不依赖跟踪摘要：空仓、没有跟踪版本的行也得有同一个数。
    assert tracked["latest_close"] == 15.0
    assert tracked["latest_close_date"] == "2026-09-03"

    # 还没有跟踪快照的标的：收益字段留空（不能凭空给 0），但现价照样有，取它自己的末棒。
    fresh = next(item for item in items if item["code"] == "000001")
    assert fresh["post_launch_return"] is None
    assert fresh["post_launch_has_data"] is None
    assert fresh["latest_close"] == 12.0
    assert fresh["latest_close_date"] == "2026-09-02"


def test_tracking_runner_deduplicates_goals_and_uses_shared_budget(tmp_path):
    from aquest.strategy_discovery.evaluation_budget import EvaluationWorkerBudget
    from aquest.strategy_pool.tracking_runner import PoolTrackingRunner

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


def test_weak_strategy_signals_and_holdings_are_filtered_in_tracking_projection(tmp_path):
    """弱策略（全区间样本不足 / 跑输基准）的信号与持仓行不进三个列表。

    回归：2026-10-09 用户决策——买入 / 卖出 / 持仓三个面板只展示「值得按其
    行动」的策略；跟踪投影与信号扫描两条路径口径必须一致（quality 模块）。
    """
    from aquest.strategy_discovery.evaluation_budget import EvaluationWorkerBudget
    from aquest.strategy_pool.tracking_runner import PoolTrackingRunner

    store = DuckDbMarketDataStore(tmp_path / "weak-filter.duckdb")
    store.replace_local_bars("SH", "600000", "stock", bars(3), name="浦发银行")
    store.replace_local_bars("SH", "600001", "stock", bars(3), name="强策略股")
    pool = DuckDbStrategyPoolStore(store)

    def candidate(candidate_id: str, closed_trades: int, total_return: float):
        return {
            "candidate_id": candidate_id,
            "buy_rule": {
                "signals": [],
                "conditions": [dict(left_value="price.close", operator="gt", right_constant=0)],
            },
            "sell_rule": {
                "signals": [],
                "conditions": [dict(left_value="price.close", operator="lt", right_constant=0)],
            },
            "config": {
                "initial_cash": 1_000_000,
                "commission": 0,
                "min_commission": 0,
                "stamp_tax": 0,
                "slippage": 0,
                "execution": "next_open",
                "stop_loss_pct": None,
                "trailing_stop_pct": None,
                "stop_loss_cooldown_days": 0,
            },
            "metrics": {
                "full_period": {"closed_trades": closed_trades, "total_return": total_return}
            },
        }

    pool.upsert_automatic(
        market="SH",
        code="600000",
        name="浦发银行",
        annual_candidate=candidate("weak", 5, 0.9),  # 全区间 5 笔：样本不足
        sharpe_candidate=None,
        data_fingerprint="a",
        config_fingerprint="b",
        source_job_id="job",
        benchmark={"full_period": {"total_return": 0.5}},
    )
    pool.upsert_automatic(
        market="SH",
        code="600001",
        name="强策略股",
        annual_candidate=candidate("strong", 30, 0.8),  # 30 笔且跑赢基准
        sharpe_candidate=None,
        data_fingerprint="a",
        config_fingerprint="b",
        source_job_id="job",
        benchmark={"full_period": {"total_return": 0.5}},
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
    by_symbol = {row["symbol"]: row for row in annual["states"]}
    # 弱策略：买入信号隐藏、状态退回 held/flat，持仓行不出现
    assert by_symbol["SH600000"]["status"] in {"flat", "held"}
    assert all(row["symbol"] != "SH600000" for row in annual["buy_signals"])
    assert all(h["code"] != "600000" for h in annual["model_portfolio"]["holdings"])
    # 健康策略照常展示：买入信号 + 持仓行
    assert [row["symbol"] for row in annual["buy_signals"]] == ["SH600001"]
    assert [h["code"] for h in annual["model_portfolio"]["holdings"]] == ["600001"]


def test_tracking_both_rules_firing_show_in_both_panels(tmp_path):
    """买卖规则同时为真 → 两个面板各自展示规则事实，状态为「买卖均触发」。

    时序构造：买入 close > 0 恒真，首日触发、次日开盘成交；卖出 close > 13
    只在第三根为真（第二根为假，保证卖出不会在第三根开盘抢先平仓）。于是
    as_of 第三根：持仓中、买卖规则同时成立 → 状态「买卖均触发」，两个面板
    都有它，且当天仍列持仓中（动作都按次一交易日执行）。
    """
    from aquest.strategy_discovery.evaluation_budget import EvaluationWorkerBudget
    from aquest.strategy_pool.tracking_runner import PoolTrackingRunner

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
    from aquest.strategy_discovery.evaluation_budget import EvaluationWorkerBudget
    from aquest.strategy_pool.tracking_runner import PoolTrackingRunner

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

    # 修正同一天的数据，逼出一次真正的回放；否则会被「行情没变就复用快照」短路，
    # 测不到 writer 拒绝时「不投影」的那条分支。
    corrected = bars(3)
    corrected.loc[2, ["close", "high", "low"]] = [16.0, 17.0, 13.0]
    store.replace_local_bars("SH", "600000", "stock", corrected)
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
    from aquest.strategy_discovery.evaluation_budget import EvaluationWorkerBudget
    from aquest.strategy_pool.tracking_runner import PoolTrackingRunner

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
    results = result["results_by_objective"]
    successful_objective = versions[0]["objective"]
    failed_objective = versions[1]["objective"]
    assert results[successful_objective]["holding_snapshot_complete"] is True
    assert results[successful_objective]["model_portfolio"]["failed_symbols"] == 0
    assert results[successful_objective]["model_portfolio"]["holdings"]
    assert results[failed_objective]["holding_snapshot_complete"] is False
    assert results[failed_objective]["model_portfolio"]["failed_symbols"] == 1
    assert progress_events[-1][0:2] == (2, 2)
    assert budget.available_workers == 1
    assert "无法记录策略追踪失败状态" in caplog.text


def test_tracking_empty_selection_does_not_lease_worker(tmp_path):
    from aquest.strategy_discovery.evaluation_budget import EvaluationWorkerBudget
    from aquest.strategy_pool.tracking_runner import PoolTrackingRunner

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


def test_shadowed_automatic_failure_keeps_manual_holdings_complete(tmp_path):
    from aquest.strategy_discovery.evaluation_budget import EvaluationWorkerBudget
    from aquest.strategy_pool.tracking_runner import PoolTrackingRunner

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
        sharpe_candidate=None,
        data_fingerprint="a",
        config_fingerprint="b",
        source_job_id="job",
    )
    pool.set_manual_override("SH", "600000", "annual_return", manual)
    tracks = PoolTrackingStore(store)
    versions = tracks.current_versions()
    automatic_version = next(v for v in versions if v["source"] == "automatic")
    manual_version = next(v for v in versions if v["source"] == "manual")

    def write(item):
        if item.get("error"):
            return None
        if item["version_id"] == automatic_version["version_id"]:
            raise RuntimeError("shadowed automatic snapshot write failed")
        return {
            "saved": tracks.save_snapshot(item["version_id"], item["signature"], item["snapshot"])
        }

    result = PoolTrackingRunner(store, EvaluationWorkerBudget(1)).run(
        {"objective": "annual_return"},
        progress=lambda *args: None,
        abort_check=lambda: False,
        writer=write,
    )
    annual = result["results_by_objective"]["annual_return"]

    assert result["failed_symbols"] == 1
    assert annual["holding_snapshot_complete"] is True
    assert annual["model_portfolio"]["failed_symbols"] == 0
    assert len(annual["model_portfolio"]["holdings"]) == 1
    assert annual["states"][0]["candidate_id"] == manual_version["candidate"]["candidate_id"]
    assert annual["model_portfolio"]["holdings"][0]["code"] == "600000"


def test_tracking_runner_marks_failed_selected_version_incomplete(tmp_path):
    from aquest.strategy_discovery.evaluation_budget import EvaluationWorkerBudget
    from aquest.strategy_pool.tracking_runner import PoolTrackingRunner

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
    assert result["results_by_objective"]["annual_return"]["model_portfolio"]["failed_symbols"] == 1
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


def test_replay_window_skips_late_listed_stocks_own_warmup(tmp_path):
    """次新股的回放从自己的第 269 根开始：前 268 根只作指标预热。"""

    import numpy as np

    store = DuckDbMarketDataStore(tmp_path / "late-listed.duckdb")
    dates = pd.date_range("2020-01-01", periods=600, freq="B")
    close = 10.0 + np.arange(600) / 100
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
    value["config"].update(initial_cash=100_000, stop_loss_pct=None)
    version = dict(
        candidate=value,
        signature=signature(value),
        launch_data_date=dates[-1].date().isoformat(),
        created_at="2026-10-09T16:00:00+00:00",
    )

    result = replay_account(service, ref, version)

    start_text = dates[268].strftime("%Y-%m-%d")
    equity_dates = [str(row["date"]) for row in result["checkpoint"]["equity"]]
    assert equity_dates[0] == start_text
    assert len(equity_dates) == 600 - 268
    trade_dates = [str(row["date"]) for row in result["checkpoint"]["trades"]]
    assert trade_dates
    assert all(day >= start_text for day in trade_dates)


def test_replay_window_yields_its_own_268_bars_for_old_listed_stocks_too(tmp_path):
    """老票也一样：2015 全年只有 244 个交易日，凑不满 268，差额继续从 2016 年里让。

    研究侧同一口径（``_apply_warmup_window``），两边不一致的话回放会比研究窗口多出一截
    没热透的指标交易（2026-10-23 全市场统一）。历史短到一个研究样本都不剩的旧版本
    保持原窗口，等重新研究后再对齐。
    """

    import numpy as np

    store = DuckDbMarketDataStore(tmp_path / "old-listed-warmup.duckdb")
    dates = pd.date_range("2015-01-05", periods=600, freq="B")
    close = 10.0 + np.arange(600) / 100
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
    value["config"].update(initial_cash=100_000, stop_loss_pct=None)
    version = dict(
        candidate=value,
        signature=signature(value),
        launch_data_date=dates[-1].date().isoformat(),
        created_at="2026-10-09T16:00:00+00:00",
    )

    result = replay_account(service, ref, version)

    # 统一口径就一句话：回放第一根永远是这只标的自己的第 269 根。
    start_text = dates[268].strftime("%Y-%m-%d")
    equity_dates = [str(row["date"]) for row in result["checkpoint"]["equity"]]
    assert equity_dates[0] == start_text
    assert len(equity_dates) == 600 - 268

    short = DuckDbMarketDataStore(tmp_path / "short-history.duckdb")
    short_dates = pd.date_range("2015-01-05", periods=300, freq="B")
    short.replace_local_bars(
        "SH",
        "600000",
        "stock",
        pd.DataFrame(
            dict(
                date=short_dates,
                open=close[:300],
                close=close[:300],
                high=close[:300] + 0.1,
                low=close[:300] - 0.1,
                volume=1000.0,
                amount=close[:300] * 1000,
                bar_status="completed",
            )
        ),
    )
    short_service = PortfolioBacktestService(adapter=DuckDbMarketDataAdapter(short))
    kept = replay_account(
        short_service,
        DuckDbMarketDataAdapter(short).stock_ref("", "SH", "600000"),
        {**version, "launch_data_date": short_dates[-1].date().isoformat()},
    )
    # 让完就剩 32 根，连一个完整交易年的样本都不到：保持原窗口，不剪成空。
    assert [str(row["date"]) for row in kept["checkpoint"]["equity"]][0] == "2016-01-01"


def test_cancelled_initialization_is_not_requeued_on_unchanged_input(tmp_path):
    from types import SimpleNamespace

    from aquest.strategy_discovery.duckdb_job_store import DuckDbStrategyJobStore
    from aquest.strategy_discovery.job_backend import DuckDbStrategyJobBackend
    from aquest.strategy_pool.tracking_scheduler import PoolTrackingScheduler

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


def test_pool_curve_reports_benchmark_and_excess_over_the_viewed_window(tmp_path):
    """详情页的「持有基准 / 超额收益」按所看区间给；区间外就宁可不给。

    此前接口 payload 里根本没有这两个键——数值其实躺在 `benchmark.performance` 里，
    没人抬上来，页面两处只能显示空占位。修好后必须与同一窗口的策略收益可比：
    「创建以来」用整段基准收益会把基线日之前的涨跌算进来，那是另一段行情。
    """

    from fastapi.testclient import TestClient

    from aquest.web.app import create_app

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", bars(4))
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
    version = tracks.current_versions()[0]
    service = PortfolioBacktestService(adapter=DuckDbMarketDataAdapter(store))
    snapshot = replay_account(service, service._adapter.stock_ref("", "SH", "600000"), version)
    assert tracks.save_snapshot(version["version_id"], version["signature"], snapshot)

    url = f"/api/v1/strategy-pool/entries/SH/600000/versions/{version['version_id']}"
    with TestClient(app) as client:
        full = client.get(url + "/curve?scope=full").json()["data"]["backtest"]
        created = client.get(url + "/curve?scope=created").json()["data"]["backtest"]

    curve = full["benchmark"]
    assert curve is not None
    dates, totals = curve["dates"], curve["totals"]
    assert full["benchmark_return"] == pytest.approx(totals[-1] / totals[0] - 1)
    assert full["excess_return"] == pytest.approx(
        full["performance"]["total_return"] - full["benchmark_return"]
    )
    # 「创建以来」必须按基线日重算，且基线日在行情之外时宁缺勿假：不能退回整段基准
    # 收益充数，否则策略收益是一段窗口、基准是另一段窗口。
    baseline = int(str(created["baseline_date"]).replace("-", ""))
    window = [value for day, value in zip(dates, totals, strict=True) if day >= baseline]
    expected = window[-1] / window[0] - 1 if window else None
    assert created["benchmark_return"] == (
        pytest.approx(expected) if expected is not None else None
    )
    assert created["benchmark"]["total_return"] == created["benchmark_return"]
    assert created["excess_return"] == (
        pytest.approx(created["performance"]["total_return"] - expected)
        if expected is not None
        else None
    )


def test_pool_entry_list_keeps_holding_risk_lines_but_not_the_snapshot(tmp_path):
    """列表要留持仓风控线，但别把跟踪版本里那整套候选一起拖着。

    表格与「特别关注」靠 ``tracking.summary.holding`` 显示止损/移动止盈/止盈和到期提示；
    而版本里再带一份 candidate 是详情页 ``/detail`` 会另外取来的同一份内容，全市场一次
    就多传 8 MB。收益曲线的逐日金额与检查点摘要同理：它们只在详情页用得上。
    """
    from fastapi.testclient import TestClient

    from aquest.web.app import create_app

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", bars(3), name="浦发银行")
    app = create_app(
        market_data_store=store, auto_sync_enabled=False, strategy_worker_token="test-token"
    )
    pool = app.state.strategy_pool_store
    pool.upsert_automatic(
        market="SH",
        code="600000",
        name="浦发银行",
        annual_candidate=candidate(),
        sharpe_candidate=candidate(),
        data_fingerprint="a",
        config_fingerprint="b",
        source_job_id="job",
    )
    tracks = PoolTrackingStore(store)
    version = next(v for v in tracks.current_versions() if v["code"] == "600000")
    assert tracks.save_snapshot(
        version["version_id"],
        version["signature"],
        {
            "summary": {
                "as_of_date": "2026-09-03",
                "created_return": 0.4,
                "latest_close": 12.5,
                "latest_close_date": "2026-09-03",
                "checkpoint_hash": "x" * 64,
                "equity": [1_000_000.0, 1_100_000.0, 1_400_000.0],
                "holding": [
                    {
                        "market": "SH",
                        "code": "600000",
                        "close": 12.5,
                        "stop_loss_price": 11.2,
                        "trailing_stop_price": 12.9,
                        "take_profit_price": 14.0,
                        "risk_level_as_of": "2026-09-03",
                    }
                ],
            },
            "checkpoint": {"trades": [], "equity": [], "positions": {}},
        },
    )

    with TestClient(app) as client:
        row = next(
            item
            for item in client.get("/api/v1/strategy-pool/entries").json()["data"]["items"]
            if item["code"] == "600000"
        )

    tracking = row["tracking"]
    assert tracking["version_id"] == version["version_id"]
    assert "candidate" not in tracking
    assert "signature" not in tracking
    summary = tracking["summary"]
    assert summary["holding"][0]["trailing_stop_price"] == 12.9
    assert "checkpoint_hash" not in summary
    assert "equity" not in summary
    # 「最新收盘」只认行情末棒（bars(3) 收在 15.0）。摘要里那个 12.5 是旧口径留下的字段：
    # 它只有跑过全量回放的版本才有，空仓和没有跟踪版本的行拿不到，整列就没法横向比，
    # 所以现在既不再投影它，也不让它盖住真实的行情末棒。
    assert row["latest_close"] == 15.0
    assert row["latest_close_date"] == "2026-09-03"
    assert "latest_close" not in summary
    assert row["tracking_as_of_date"] == "2026-09-03"
