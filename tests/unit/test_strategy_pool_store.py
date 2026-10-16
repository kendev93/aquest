from __future__ import annotations

import duckdb
import pytest

from aquest.market_data.store import DuckDbMarketDataStore
from aquest.strategy_pool.store import (
    FOCUS_LIMIT,
    FOCUS_NAME_MAX_LENGTH,
    DuckDbStrategyPoolStore,
)
from aquest.strategy_pool.tracking_store import ENGINE_VERSION, PoolTrackingStore, signature


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


def test_insufficient_history_clears_stale_automatic_candidates_and_keeps_fingerprints(
    tmp_path,
) -> None:
    market_store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    pool = DuckDbStrategyPoolStore(market_store)
    pool.initialize()
    pool.upsert_automatic(
        market="SH",
        code="600000",
        name="浦发银行",
        annual_candidate={"candidate_id": "stale-annual"},
        sharpe_candidate={"candidate_id": "stale-sharpe"},
        data_fingerprint="old-data",
        market_fingerprint="old-market",
        config_fingerprint="old-config",
        source_job_id="old-job",
    )
    pool.set_manual_override("SH", "600000", "annual_return", {"candidate_id": "manual"})

    pool.persist_worker_entry(
        {
            "market": "SH",
            "code": "600000",
            "error": "历史样本不足：无法切出三段",
            "status": "insufficient_history",
            "data_fingerprint": "new-data",
            "market_fingerprint": "new-market",
            "config_fingerprint": "new-config",
        },
        source_job_id="new-job",
    )

    item = pool.get("SH", "600000")
    assert item is not None
    assert item["status"] == "insufficient_history"
    assert item["last_error"] == "历史样本不足：无法切出三段"
    assert item["data_fingerprint"] == "new-data"
    assert item["market_fingerprint"] == "new-market"
    assert item["config_fingerprint"] == "new-config"
    assert item["automatic_candidates"] == {"annual_return": None, "sharpe": None}
    assert item["manual_overrides"]["annual_return"]["candidate_id"] == "manual"


def test_failure_under_new_config_invalidates_stale_automatic_candidates(tmp_path) -> None:
    market_store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    pool = DuckDbStrategyPoolStore(market_store)
    pool.initialize()
    pool.upsert_automatic(
        market="SZ",
        code="000001",
        name="平安银行",
        annual_candidate={"candidate_id": "stale-annual"},
        sharpe_candidate={"candidate_id": "stale-sharpe"},
        data_fingerprint="old-data",
        market_fingerprint="old-market",
        config_fingerprint="old-config",
        source_job_id="old-job",
    )
    pool.set_manual_override("SZ", "000001", "sharpe", {"candidate_id": "manual"})

    pool.persist_worker_entry(
        {
            "market": "SZ",
            "code": "000001",
            "error": "临时行情读取失败",
            "config_fingerprint": "new-config",
        },
        source_job_id="new-job",
    )

    item = pool.get("SZ", "000001")
    assert item is not None
    assert item["status"] == "failed"
    assert item["automatic_candidates"] == {"annual_return": None, "sharpe": None}
    assert item["manual_overrides"]["sharpe"]["candidate_id"] == "manual"
    assert item["config_fingerprint"] == "new-config"


def test_pool_signature_includes_max_hold_days_but_preserves_unlimited_hash() -> None:
    base = {
        "buy_rule": {"signals": ["technical.macd"]},
        "sell_rule": {"signals": ["technical.rsi14"]},
        "config": {"stop_loss_pct": 0.08},
    }
    explicit_none = {**base, "config": {**base["config"], "max_hold_days": None}}
    hold_5 = {**base, "config": {**base["config"], "max_hold_days": 5}}
    hold_60 = {**base, "config": {**base["config"], "max_hold_days": 60}}

    assert signature(base) == signature(explicit_none)
    assert signature(hold_5) != signature(hold_60)
    assert ENGINE_VERSION == "pool-account-v3"


def test_legacy_max_hold_signature_upgrades_in_place_and_changed_limit_reversions(tmp_path) -> None:
    market_store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    pool = DuckDbStrategyPoolStore(market_store)
    pool.initialize()
    hold_5 = {
        "candidate_id": "max-hold",
        "buy_rule": {"signals": ["technical.macd"]},
        "sell_rule": {"signals": ["technical.rsi14"]},
        "config": {"stop_loss_pct": 0.08, "max_hold_days": 5},
    }
    pool.upsert_automatic(
        market="SH",
        code="600000",
        name="测试",
        annual_candidate=hold_5,
        sharpe_candidate=None,
        data_fingerprint="bars",
        config_fingerprint="config",
        source_job_id="job-1",
    )
    tracks = PoolTrackingStore(market_store)
    original = next(v for v in tracks.current_versions() if v["objective"] == "annual_return")
    legacy_candidate = {
        **hold_5,
        "config": {key: value for key, value in hold_5["config"].items() if key != "max_hold_days"},
    }
    created_at = "2025-01-02T03:04:05+00:00"
    launch_data_date = "2025-01-01"
    with market_store._connect() as connection:
        connection.execute(
            "UPDATE pool_tracking_versions SET signature=?,created_at=?,launch_data_date=? "
            "WHERE version_id=?",
            [signature(legacy_candidate), created_at, launch_data_date, original["version_id"]],
        )

    tracks.bootstrap()
    upgraded = next(
        v
        for v in tracks.current_versions()
        if v["objective"] == "annual_return" and v["source"] == "automatic"
    )
    assert upgraded["version_id"] == original["version_id"]
    assert upgraded["signature"] == signature(hold_5)
    assert upgraded["created_at"] == created_at
    assert upgraded["launch_data_date"] == launch_data_date

    hold_60 = {**hold_5, "config": {**hold_5["config"], "max_hold_days": 60}}
    pool.upsert_automatic(
        market="SH",
        code="600000",
        name="测试",
        annual_candidate=hold_60,
        sharpe_candidate=None,
        data_fingerprint="bars",
        config_fingerprint="config",
        source_job_id="job-2",
    )
    current = next(
        v
        for v in tracks.current_versions()
        if v["objective"] == "annual_return" and v["source"] == "automatic"
    )
    assert current["version_id"] != original["version_id"]
    assert current["signature"] == signature(hold_60)

    pool.upsert_automatic(
        market="SH",
        code="600001",
        name="测试",
        annual_candidate=hold_5,
        sharpe_candidate=None,
        data_fingerprint="bars",
        config_fingerprint="config",
        source_job_id="job-3",
    )
    legacy_five = next(
        v for v in tracks.current_versions() if v["code"] == "600001" and v["source"] == "automatic"
    )
    legacy_five_created_at = "2025-03-02T03:04:05+00:00"
    legacy_five_launch_date = "2025-03-01"
    with market_store._connect() as connection:
        connection.execute(
            "UPDATE pool_tracking_versions SET signature=?,created_at=?,launch_data_date=? "
            "WHERE version_id=?",
            [
                signature(legacy_candidate),
                legacy_five_created_at,
                legacy_five_launch_date,
                legacy_five["version_id"],
            ],
        )

    # A max-hold change arriving before any same-config signature upgrade must archive the old
    # account. The legacy hash alone cannot make stored config=5 equivalent to incoming config=60.
    pool.upsert_automatic(
        market="SH",
        code="600001",
        name="测试",
        annual_candidate=hold_60,
        sharpe_candidate=None,
        data_fingerprint="bars",
        config_fingerprint="config",
        source_job_id="job-4",
    )
    versions = tracks.versions("SH", "600001", "annual_return")
    archived_five = next(v for v in versions if v["version_id"] == legacy_five["version_id"])
    current_sixty = next(v for v in versions if v["is_current"])
    assert archived_five["is_current"] is False
    assert archived_five["stopped_at"] is not None
    assert archived_five["created_at"] == legacy_five_created_at
    assert archived_five["launch_data_date"] == legacy_five_launch_date
    assert current_sixty["version_id"] != legacy_five["version_id"]
    assert current_sixty["signature"] == signature(hold_60)
    assert current_sixty["candidate"]["config"]["max_hold_days"] == 60


def test_focus_round_trip_is_idempotent_per_symbol(tmp_path) -> None:
    pool = DuckDbStrategyPoolStore(DuckDbMarketDataStore(tmp_path / "market.duckdb"))
    pool.initialize()

    assert pool.read_focus() == []
    added = pool.add_focus("SH", "600000", "浦发银行")
    assert added == [{"market": "SH", "code": "600000", "name": "浦发银行"}]

    # 连点或两个标签页同时提交同一只标的，不会挤出第二条记录。
    assert pool.add_focus("SH", "600000", "浦发银行") == added

    assert pool.add_focus("SZ", "000001", "平安银行") == [
        {"market": "SH", "code": "600000", "name": "浦发银行"},
        {"market": "SZ", "code": "000001", "name": "平安银行"},
    ]
    assert pool.remove_focus("SH", "600000") == [
        {"market": "SZ", "code": "000001", "name": "平安银行"}
    ]
    # 取消没有关注过的代码同样幂等。
    assert pool.remove_focus("SH", "600000") == [
        {"market": "SZ", "code": "000001", "name": "平安银行"}
    ]


def test_focus_validates_symbol_and_truncates_name(tmp_path) -> None:
    pool = DuckDbStrategyPoolStore(DuckDbMarketDataStore(tmp_path / "market.duckdb"))
    pool.initialize()

    with pytest.raises(ValueError):
        pool.add_focus("BJ", "600000")
    with pytest.raises(ValueError):
        pool.add_focus("SH", "60000")

    long_name = "名称" * 40
    assert pool.add_focus("SH", "600000", long_name)[0]["name"] == long_name[:FOCUS_NAME_MAX_LENGTH]


def test_focus_limit_rejects_the_next_symbol(tmp_path) -> None:
    pool = DuckDbStrategyPoolStore(DuckDbMarketDataStore(tmp_path / "market.duckdb"))
    pool.initialize()
    for index in range(FOCUS_LIMIT):
        pool.add_focus("SH", f"{index:06d}")

    with pytest.raises(ValueError, match="特别关注最多"):
        pool.add_focus("SH", "999999")
    # 被拒的写入不会把已有的关注名单弄脏。
    assert len(pool.read_focus()) == FOCUS_LIMIT
    assert pool.remove_focus("SH", "000000") and len(pool.read_focus()) == FOCUS_LIMIT - 1


def test_focus_survives_full_pool_reset(tmp_path) -> None:
    """清空重建会删空 entries；关注是用户的选择，不能跟着一起丢。"""

    pool = DuckDbStrategyPoolStore(DuckDbMarketDataStore(tmp_path / "market.duckdb"))
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
    pool.add_focus("SH", "600000", "浦发银行")

    pool.persist_worker_entry({"reset": True}, source_job_id="job-2")

    assert pool.list_entries() == []
    assert pool.read_focus() == [{"market": "SH", "code": "600000", "name": "浦发银行"}]


def test_pool_page_reads_prefer_the_read_only_fast_path(tmp_path, monkeypatch) -> None:
    """研究进行中页面还得能读：读优先走只读打开，成功就不该再去要独占连接。

    DuckDB 的读写打开是独占的：研究进程只要持有一条连接，后端的读写打开就会失败并
    重试约 3.8 秒（_CONNECT_RETRY_DELAYS）。UI 每秒轮询好几个池接口，这些等待会占满
    请求线程池，整站跟着超时——页面看起来就是「卡在某个阶段不动」。
    """

    opened: list[bool] = []
    connected: list[bool] = []
    original_open = DuckDbMarketDataStore._open
    original_connect = DuckDbMarketDataStore._connect

    def spy_open(self, *, read_only):  # noqa: ANN001
        opened.append(bool(read_only))
        return original_open(self, read_only=read_only)

    def spy_connect(self, *, read_only=None):  # noqa: ANN001
        resolved = self.read_only if read_only is None else read_only
        connected.append(bool(resolved))
        return original_connect(self, read_only=read_only)

    monkeypatch.setattr(DuckDbMarketDataStore, "_open", spy_open)
    monkeypatch.setattr(DuckDbMarketDataStore, "_connect", spy_connect)
    market_store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    pool = DuckDbStrategyPoolStore(market_store)
    pool.initialize()
    opened.clear()
    connected.clear()

    assert pool.list_entries() == []
    assert pool.read_focus() == []
    assert pool.read_research_settings() is None
    assert pool.get("SH", "600000") is None
    assert opened and set(opened) == {True}
    assert connected == []

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
    assert connected[0] is False


def test_pool_reads_fall_back_when_read_only_open_conflicts(tmp_path, monkeypatch) -> None:
    """同进程已有写连接时只读打开会被自己拒绝（DuckDB 报 PID 0）：立刻退回读写路径。

    研究 worker 就是同一个进程里既读又写这张表，只读打开在那里必然冲突；不退回
    去就等于把整条全市场任务拖死——2026-10-16 上线只读快路径后 4 分钟整条任务
    失败就是这么来的。退回时不额外等待，页面侧本来也用不到这条分支。
    """

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
    original_open = DuckDbMarketDataStore._open

    def refuse_read_only(self, *, read_only):  # noqa: ANN001
        if read_only:
            raise duckdb.Error("Could not set lock: Conflicting lock is held in PID 0")
        return original_open(self, read_only=read_only)

    monkeypatch.setattr(DuckDbMarketDataStore, "_open", refuse_read_only)

    assert [row["code"] for row in pool.list_entries()] == ["600000"]
    assert pool.read_focus() == []
