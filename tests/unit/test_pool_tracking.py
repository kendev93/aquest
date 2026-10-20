import numpy as np
import pandas as pd

from aquest.market_data.store import DuckDbMarketDataStore
from aquest.strategy_pool.store import DuckDbStrategyPoolStore
from aquest.strategy_pool.tracking import account_summary
from aquest.strategy_pool.tracking_store import (
    PoolTrackingStore,
    launch_data_date,
    launch_data_dates,
)


def _bars(count: int, start: str = "2020-01-01") -> pd.DataFrame:
    close = np.linspace(10.0, 12.0, count)
    return pd.DataFrame(
        {
            "date": pd.date_range(start, periods=count, freq="D"),
            "open": close,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": np.full(count, 1_000.0),
            "amount": close * 1_000,
            "bar_status": "completed",
        }
    )


def _store_with_bars(tmp_path):  # noqa: ANN001, ANN202
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars(20), name="浦发银行")
    store.replace_local_bars("SZ", "000001", "stock", _bars(12, "2020-02-01"), name="平安银行")
    return store


def test_versions_ignore_candidate_ids_metrics_and_dates_but_change_rules(tmp_path):
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    pool = DuckDbStrategyPoolStore(store)
    candidate = {
        "candidate_id": "a",
        "buy_rule": {"signals": ["technical.macd"]},
        "sell_rule": {"signals": []},
        "config": {"stop_loss_pct": 0.1},
    }

    def save(item):
        pool.upsert_automatic(
            market="SH",
            code="600000",
            name="test",
            annual_candidate=item,
            sharpe_candidate=item,
            data_fingerprint="x",
            config_fingerprint="y",
            source_job_id="job",
        )

    save(candidate)
    tracks = PoolTrackingStore(store)
    first = tracks.versions("SH", "600000", "annual_return")[0]
    save(
        {
            **candidate,
            "candidate_id": "b",
            "metrics": {"total_return": 0.3},
            "config": {**candidate["config"], "end_date": "2026-10-03"},
        }
    )
    assert tracks.versions("SH", "600000", "annual_return")[0]["version_id"] == first["version_id"]
    save({**candidate, "config": {"stop_loss_pct": 0.2}})
    versions = tracks.versions("SH", "600000", "annual_return")
    assert len(versions) == 2
    assert versions[1]["stopped_at"]
    pool.persist_worker_entry({"reset": True}, source_job_id="reset")
    assert not tracks.current_versions()
    assert len(tracks.versions("SH", "600000", "annual_return")) == 2


def test_buy_blockades_report_only_blocked_symbols_for_the_queried_objective(tmp_path):
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    pool = DuckDbStrategyPoolStore(store)
    pool.upsert_automatic(
        market="SH",
        code="600000",
        name="test",
        annual_candidate={
            "candidate_id": "a",
            "buy_rule": {"signals": ["technical.macd"]},
            "sell_rule": {"signals": []},
            "config": {"stop_loss_pct": 0.1},
        },
        sharpe_candidate=None,
        data_fingerprint="x",
        config_fingerprint="y",
        source_job_id="job",
    )
    tracks = PoolTrackingStore(store)
    version = tracks.current_versions()[0]
    # 没有阻断原因的版本不该出现在结果里，否则会把正常买入信号一起吃掉。
    clean = {"summary": {"as_of_date": "2026-10-08"}, "checkpoint": {}}
    assert tracks.save_snapshot(version["version_id"], version["signature"], clean)
    assert tracks.buy_blockades("annual_return") == {}
    blocked = {
        "summary": {"as_of_date": "2026-10-08", "buy_blocked_reason": "cooldown"},
        "checkpoint": {},
    }
    assert tracks.save_snapshot(version["version_id"], version["signature"], blocked)
    assert tracks.buy_blockades("annual_return") == {("SH", "600000"): ("cooldown", "2026-10-08")}
    assert tracks.buy_blockades("sharpe") == {}


def test_account_summary_merges_risk_lines_from_current_state():
    """三档风控线、挂单出处与持有到期只存在于 current_state，投影时按标的并回持仓。

    净值快照（states）会序列化进每个检查点 blob，把展示字段塞那里等于给每只标的
    乘上几千行；所以它们只在回放尾部算一次，由 account_summary 合到展示行上。
    没传 current_state 就留空，绝不编一条线或填 0。
    """

    state_holdings = [
        {
            "market": "SH",
            "code": "600000",
            "entry_date": "2026-09-01",
            "entry_price": 10.0,
            "close": 9.4,
        }
    ]
    checkpoint = {
        "equity": [
            {"date": "2026-09-01", "total": 1000},
            {"date": "2026-09-02", "total": 1000},
        ],
        "states": [{"date": "2026-09-02", "holdings": state_holdings}],
        "trades": [{"date": "2026-09-01", "direction": "BUY", "equity_before": 1000}],
    }
    current_state = {
        "holdings": [
            {
                "market": "SH",
                "code": "600000",
                "stop_loss_price": 9.0,
                "trailing_stop_price": None,
                "take_profit_price": None,
                "risk_level_as_of": "2026-09-02",
                "highest_price": 10.5,
                "pending_exit_reason": "止损",
                "pending_exit_signal_date": "2026-09-02",
                "pending_exit_risk_trigger": "stop_loss",
                "max_hold_days": 20,
                "max_hold_days_left": 3,
                "max_hold_expiry_date": "2026-09-05",
            }
        ]
    }

    merged = account_summary(checkpoint, "2026-09-02", 1000, current_state)["holding"][0]
    assert merged["stop_loss_price"] == 9.0
    assert merged["trailing_stop_price"] is None
    assert merged["pending_exit_risk_trigger"] == "stop_loss"
    assert merged["max_hold_days_left"] == 3
    assert merged["max_hold_expiry_date"] == "2026-09-05"
    assert merged["unrealized_pnl_pct"] == 9.4 / 10.0 - 1.0
    # 注入必须是拷贝：检查点 blob 不能因为这些展示字段被改写。
    assert "stop_loss_price" not in state_holdings[0]

    plain = account_summary(checkpoint, "2026-09-02", 1000)["holding"][0]
    assert "stop_loss_price" not in plain
    assert "pending_exit_risk_trigger" not in plain


def test_inherited_trade_includes_full_gain_and_separate_post_launch_return():
    checkpoint = {
        "equity": [
            {"date": "2026-09-01", "total": 1000},
            {"date": "2026-09-02", "total": 1200},
            {"date": "2026-09-03", "total": 1500},
        ],
        "states": [{"date": "2026-09-02", "holdings": [{"entry_date": "2026-09-01"}]}],
        "trades": [{"date": "2026-09-01", "direction": "BUY", "equity_before": 1000}],
    }
    summary = account_summary(checkpoint, "2026-09-02", 1000)
    assert summary["created_return"] == 0.5
    assert summary["post_launch_return"] == 0.25
    assert summary["baseline_date"] == "2026-09-01"
    assert summary["inherited_position"] is True


def test_current_versions_can_skip_the_candidate_column(tmp_path):
    """列表那条热路径可以只要摘要，不必把每份候选读出来再丢掉。

    ``_query`` 默认带 candidate（跟踪 runner 与详情页确实要用），列表接口显式关掉：
    2026-10-19 实测一次刷新要解析 4439 份 candidate_json，而投影时整份丢弃。
    """

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    pool = DuckDbStrategyPoolStore(store)
    pool.upsert_automatic(
        market="SH",
        code="600000",
        name="test",
        annual_candidate={
            "candidate_id": "a",
            "buy_rule": {"signals": ["technical.macd"]},
            "sell_rule": {"signals": []},
            "config": {"stop_loss_pct": 0.1},
        },
        sharpe_candidate=None,
        data_fingerprint="x",
        config_fingerprint="y",
        source_job_id="job",
    )
    tracks = PoolTrackingStore(store)

    full = tracks.current_versions()[0]
    light = tracks.current_versions(with_candidate=False)[0]

    assert full["candidate"]["candidate_id"] == "a"
    assert "candidate" not in light
    assert "candidate_json" not in light
    # 摘要、签名与上线时间这些列表/投影还要读的东西一个都不能少。
    for key in ("version_id", "signature", "objective", "source", "summary", "status"):
        assert light[key] == full[key]


def test_launch_data_date_is_the_last_completed_bar_and_the_batch_agrees(tmp_path) -> None:
    """上线首日口径：封顶在今天的最后一根已完成日线；批量与逐只必须一模一样。"""

    store = _store_with_bars(tmp_path)

    assert launch_data_date(store, "SH", "600000") == "2020-01-20"
    assert launch_data_date(store, "SZ", "000001") == "2020-02-12"
    # 没有行情的标的不能编一个日期出来，否则「上线后窗口」会拿假首日去算收益。
    assert launch_data_date(store, "SH", "688888") is None
    assert launch_data_dates(
        store, [("SH", "600000"), ("SZ", "000001"), ("SH", "688888"), ("SH", "600999")]
    ) == {
        ("SH", "600000"): "2020-01-20",
        ("SZ", "000001"): "2020-02-12",
    }


def test_bootstrap_stamps_every_version_with_its_own_launch_date(tmp_path) -> None:
    """bootstrap 走批量查询：几千只标的各扫一遍日线的写法会把整库排他锁举到读者超时。"""

    store = _store_with_bars(tmp_path)
    pool = DuckDbStrategyPoolStore(store)
    candidate = {
        "buy_rule": {"signals": ["technical.macd"]},
        "sell_rule": {"signals": ["technical.rsi14"]},
        "config": {"stop_loss_pct": 0.1},
    }
    for market, code in (("SH", "600000"), ("SZ", "000001")):
        pool.upsert_automatic(
            market=market,
            code=code,
            name=code,
            annual_candidate=candidate,
            sharpe_candidate=candidate,
            data_fingerprint="x",
            config_fingerprint="y",
            source_job_id="job",
        )
    with store.write_transaction() as connection:
        connection.execute("DELETE FROM pool_tracking_versions")

    tracks = PoolTrackingStore(store)
    tracks.bootstrap()

    stamps = {
        (item["market"], item["code"]): item["launch_data_date"]
        for item in tracks.current_versions()
    }
    assert stamps == {
        ("SH", "600000"): "2020-01-20",
        ("SZ", "000001"): "2020-02-12",
    }


def test_save_snapshots_batches_with_the_same_guards_as_single_writes(tmp_path):
    """一次扫描把几千只快照合成批次写：口径必须和逐只写完全一致。

    逐只写实测 15.8 ms，4,409 只就是约 70 秒的串行写库；批量摊到 0.14 ms/行。这里钉住
    三件事：签名不符不写、日期不许倒退（同批内也按顺序判定）、有效条目真落库。
    """

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    pool = DuckDbStrategyPoolStore(store)
    for code in ("600000", "600001"):
        pool.upsert_automatic(
            market="SH",
            code=code,
            name="test",
            annual_candidate={
                "candidate_id": "a",
                "buy_rule": {"signals": ["technical.macd"]},
                "sell_rule": {"signals": []},
                "config": {"stop_loss_pct": 0.1},
            },
            sharpe_candidate=None,
            data_fingerprint="x",
            config_fingerprint="y",
            source_job_id="job",
        )
    tracks = PoolTrackingStore(store)
    versions = {v["code"]: v for v in tracks.current_versions()}

    def snapshot(day: str) -> dict:
        return {"summary": {"as_of_date": day}, "checkpoint": {"positions": {}}}

    first, second = versions["600000"], versions["600001"]
    saved = tracks.save_snapshots(
        [
            (first["version_id"], first["signature"], snapshot("2026-10-19")),
            (second["version_id"], "not-the-signature", snapshot("2026-10-19")),
            (first["version_id"], first["signature"], snapshot("2026-10-18")),
        ]
    )

    assert saved == [True, False, False]
    assert tracks.snapshot(first["version_id"])["summary"]["as_of_date"] == "2026-10-19"
    assert tracks.snapshot(second["version_id"]) is None
    assert next(v for v in tracks.current_versions() if v["code"] == "600000")["status"] == "ready"

    # 同批重复版本：后一条更新的照常写入，旧的不许覆盖新的
    saved = tracks.save_snapshots(
        [
            (first["version_id"], first["signature"], snapshot("2026-10-17")),
            (first["version_id"], first["signature"], snapshot("2026-10-20")),
        ]
    )
    assert saved == [False, True]
    assert tracks.snapshot(first["version_id"])["summary"]["as_of_date"] == "2026-10-20"
