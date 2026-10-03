from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.strategy_pool.store import DuckDbStrategyPoolStore
from selector_app.strategy_pool.tracking import account_summary
from selector_app.strategy_pool.tracking_store import PoolTrackingStore


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
