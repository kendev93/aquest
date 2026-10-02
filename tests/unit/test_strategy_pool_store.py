from __future__ import annotations

from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.strategy_pool.store import DuckDbStrategyPoolStore


def test_automatic_refresh_preserves_objective_specific_manual_overrides(tmp_path) -> None:
    market_store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    pool = DuckDbStrategyPoolStore(market_store)
    pool.initialize()
    pool.upsert_automatic(
        market="SH",
        code="600000",
        name="浦发银行",
        annual_candidate={"candidate_id": "auto-a"},
        sharpe_candidate={"candidate_id": "auto-s"},
        data_fingerprint="bars-1",
        config_fingerprint="config-1",
        source_job_id="job-1",
    )
    pool.set_manual_override(
        "SH",
        "600000",
        "annual_return",
        {"candidate_id": "manual-a"},
        benchmark={"full_period": {"total_return": 0.4}},
    )
    pool.upsert_automatic(
        market="SH",
        code="600000",
        name="浦发银行",
        annual_candidate={"candidate_id": "auto-a2"},
        sharpe_candidate={"candidate_id": "auto-s2"},
        data_fingerprint="bars-2",
        config_fingerprint="config-2",
        source_job_id="job-2",
        benchmark={"full_period": {"total_return": 0.4}},
    )

    item = pool.get("SH", "600000")

    assert item is not None
    assert item["automatic_candidates"]["annual_return"]["candidate_id"] == "auto-a2"
    assert item["automatic_candidates"]["sharpe"]["candidate_id"] == "auto-s2"
    assert item["manual_overrides"]["annual_return"]["candidate_id"] == "manual-a"
    assert item["manual_overrides"]["sharpe"] is None
    assert item["benchmark"]["full_period"]["total_return"] == 0.4
    assert pool.active_candidate("SH", "600000", "annual_return")["candidate_id"] == "manual-a"
    assert pool.active_candidate("SH", "600000", "sharpe")["candidate_id"] == "auto-s2"


def test_benchmark_update_preserves_candidates_and_manual_overrides(tmp_path) -> None:
    market_store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    pool = DuckDbStrategyPoolStore(market_store)
    pool.initialize()
    pool.upsert_automatic(
        market="SH",
        code="600000",
        name="浦发银行",
        annual_candidate={"candidate_id": "auto-a"},
        sharpe_candidate=None,
        data_fingerprint="bars-1",
        config_fingerprint="config-1",
        source_job_id="job-1",
    )
    pool.set_manual_override("SH", "600000", "annual_return", {"candidate_id": "manual-a"})

    pool.update_benchmark(
        "SH",
        "600000",
        {
            "full_period": {"total_return": 0.4},
            "validation": {"total_return": 0.1},
            "holdout": {"total_return": -0.05},
        },
    )

    item = pool.get("SH", "600000")
    assert item is not None
    assert item["benchmark"]["validation"]["total_return"] == 0.1
    assert item["automatic_candidates"]["annual_return"]["candidate_id"] == "auto-a"
    assert item["manual_overrides"]["annual_return"]["candidate_id"] == "manual-a"


def test_initialize_adds_benchmark_column_to_an_existing_pool_table(tmp_path) -> None:
    market_store = DuckDbMarketDataStore(tmp_path / "legacy.duckdb")
    pool = DuckDbStrategyPoolStore(market_store)
    pool.initialize()
    with market_store._connect() as connection:
        connection.execute("ALTER TABLE strategy_pool_entries DROP COLUMN benchmark_json")

    pool.initialize()

    with market_store._connect() as connection:
        columns = {
            row[0] for row in connection.execute("DESCRIBE strategy_pool_entries").fetchall()
        }
    assert "benchmark_json" in columns


def test_pool_store_marks_no_strategy_without_erasing_existing_manual_choice(tmp_path) -> None:
    market_store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    pool = DuckDbStrategyPoolStore(market_store)
    pool.initialize()
    pool.upsert_automatic(
        market="SZ",
        code="000001",
        name="平安银行",
        annual_candidate={"candidate_id": "old"},
        sharpe_candidate=None,
        data_fingerprint="bars-1",
        config_fingerprint="config-1",
        source_job_id="job-1",
    )
    pool.set_manual_override("SZ", "000001", "sharpe", {"candidate_id": "manual-s"})

    pool.record_symbol_error("SZ", "000001", "无可计算年化策略", source_job_id="job-2")

    item = pool.get("SZ", "000001")
    assert item is not None
    assert item["status"] == "failed"
    assert item["manual_overrides"]["sharpe"]["candidate_id"] == "manual-s"
    assert pool.active_candidate("SZ", "000001", "sharpe")["candidate_id"] == "manual-s"
