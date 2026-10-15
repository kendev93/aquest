from aquest.market_data.store import DuckDbMarketDataStore
from aquest.strategy_pool.store import DuckDbStrategyPoolStore
from aquest.strategy_pool.tracking import account_summary
from aquest.strategy_pool.tracking_store import PoolTrackingStore


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
