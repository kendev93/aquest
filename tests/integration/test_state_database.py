"""状态表与行情分库：迁移只拷贝，之后的写入落在状态库，导出按表分派。

池条目、跟踪版本、任务这些写得最频繁又和日线合并没有关系的表拆到同目录的
``state.duckdb``；日线、指标缓存和各类历史留在行情库。行情库里的旧表原样保留，
所以删掉 ``state.duckdb`` 就能退回单库（看到的是拆分前的数据）。
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from aquest.market_data import store_base as store_base_module
from aquest.market_data.store import DuckDbMarketDataStore
from aquest.strategy_discovery.duckdb_job_store import DuckDbStrategyJobStore
from aquest.strategy_pool.store import DuckDbStrategyPoolStore
from aquest.strategy_pool.tracking_store import PoolTrackingStore


def _bars() -> pd.DataFrame:
    close = [10.0, 12.0, 15.0]
    return pd.DataFrame(
        {
            "date": pd.date_range("2026-09-01", periods=3, freq="D"),
            "open": close,
            "high": [11.0, 13.0, 16.0],
            "low": [9.0, 11.0, 13.0],
            "close": close,
            "volume": [1000.0] * 3,
            "amount": [10000.0] * 3,
            "bar_status": ["completed"] * 3,
        }
    )


def _count(path: Path, table: str, where: str = "") -> int:
    with duckdb.connect(str(path), read_only=True) as connection:
        row = connection.execute(f"SELECT COUNT(*) FROM {table} {where}").fetchone()
    return int(row[0]) if row is not None else 0


def test_state_tables_move_to_their_own_file(tmp_path: Path) -> None:
    market_path = tmp_path / "market.duckdb"
    state_path = tmp_path / "state.duckdb"
    store = DuckDbMarketDataStore(market_path)
    jobs = DuckDbStrategyJobStore(store)
    pool = DuckDbStrategyPoolStore(store)
    pool.initialize()

    jobs.create(
        "job-before",
        operation="market_strategy_pool",
        payload={"mode": "full"},
        description="拆分前",
    )
    pool.record_symbol_error("SH", "600000", "行情不足", source_job_id="job-before")

    # 还没拆库：一切都在行情库里，行为和以前一模一样。
    assert store.has_state_database() is False
    assert not state_path.exists()

    copied = store.ensure_state_database()

    assert copied["strategy_jobs"] == 1
    assert copied["strategy_pool_entries"] == 1
    assert state_path.exists()
    assert store.has_state_database() is True

    # 拆分之后的新写入只进状态库；行情库里的旧表原样留着（只拷贝不删除）。
    jobs.create(
        "job-after",
        operation="market_strategy_pool",
        payload={"mode": "full"},
        description="拆分后",
    )
    pool.record_symbol_error("SZ", "000001", "行情不足", source_job_id="job-after")
    assert _count(state_path, "strategy_jobs", "WHERE job_id='job-after'") == 1
    assert _count(market_path, "strategy_jobs", "WHERE job_id='job-after'") == 0
    assert _count(market_path, "strategy_jobs") == 1
    assert _count(state_path, "strategy_pool_entries", "WHERE code='000001'") == 1
    assert _count(market_path, "strategy_pool_entries", "WHERE code='000001'") == 0

    # 迁移幂等：再跑一次什么都不做，也不会把旧数据又盖回来。
    assert store.ensure_state_database() == {}

    # 导出按表分派：拆到状态库的池条目照样能完整导出来（拆分前后写的都要在）。
    exported = {row["code"] for row in store.iter_history_rows("strategy_pool_entries")}
    assert exported == {"600000", "000001"}

    # 池跟踪的读写也落在状态库：bootstrap 从状态库读池条目、再写跟踪版本。
    PoolTrackingStore(store).bootstrap()
    assert PoolTrackingStore(store).current_versions() == []
    assert _count(state_path, "pool_tracking_versions") == 0


def test_old_state_database_focus_is_preserved_and_exportable(tmp_path: Path) -> None:
    from aquest.market_data.transfer import export_user_assets

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    pool = DuckDbStrategyPoolStore(store)
    pool.initialize()
    pool.add_focus("SH", "600000", "浦发银行")
    store.ensure_state_database()
    with duckdb.connect(str(tmp_path / "state.duckdb")) as connection:
        connection.execute("DROP TABLE strategy_pool_focus_members")
        connection.execute("DROP TABLE strategy_pool_focus_groups")
    reopened = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    result = export_user_assets(reopened, tmp_path / "old-state.zip")
    assert result.counts["strategy_pool_focus"] == 1
    reopened_pool = DuckDbStrategyPoolStore(reopened)
    reopened_pool.initialize()
    reopened_pool.initialize()
    assert reopened_pool.read_focus_groups()[0]["items"][0]["code"] == "600000"


def test_the_market_file_keeps_working_without_a_state_database(tmp_path: Path) -> None:
    """删掉 state.duckdb 就退回单库：读写走行情库，不报错。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    jobs = DuckDbStrategyJobStore(store)
    jobs.create("job-1", operation="market_strategy_pool", payload={}, description="单库")

    (tmp_path / "state.duckdb").unlink(missing_ok=True)

    assert store.has_state_database() is False
    assert jobs.read_state("job-1") is not None


def test_read_and_write_connections_can_coexist(tmp_path: Path) -> None:
    """同一个状态库里读会话与写事务可以同时开。

    混着只读/读写打开同一个文件，DuckDB 会报 "different configuration"；状态库上
    worker 一直在写任务状态，撞上就是 claim 500（2026-10-25 实测一半请求失败）。
    """

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    DuckDbStrategyJobStore(store).create(
        "job-1", operation="market_strategy_pool", payload={}, description="建库"
    )
    store.ensure_state_database()

    with store.state_read_session() as reader:
        row = reader.execute("SELECT COUNT(*) FROM strategy_jobs").fetchone()
        assert row is not None
        with store.state_write_transaction() as writer:
            assert writer.execute("SELECT 1").fetchone() is not None


def test_a_transient_cross_process_lock_is_waited_out(monkeypatch, tmp_path: Path) -> None:
    """worker 读状态库、backend 同时写时，DuckDB 会报 Conflicting lock；等一下再来即可。

    两个容器在同一个卷上看到的是两个进程，跨 PID 命名空间拿不到对方 PID，报错里显示
    「held in PID 0」（2026-10-25 实测）。
    """

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    DuckDbStrategyJobStore(store).create(
        "job-1", operation="market_strategy_pool", payload={}, description="建库"
    )
    store.ensure_state_database()

    real = store_base_module.open_database
    failures = {"left": 1}

    def flaky(path, *, read_only):
        if str(path).endswith("state.duckdb") and failures["left"]:
            failures["left"] -= 1
            raise duckdb.IOException(
                "Could not set lock on file: Conflicting lock is held in PID 0"
            )
        return real(path, read_only=read_only)

    monkeypatch.setattr(store_base_module, "open_database", flaky)

    with store.state_read_session() as reader:
        assert reader.execute("SELECT COUNT(*) FROM strategy_jobs").fetchone() is not None
    assert failures["left"] == 0


def test_a_persistent_lock_conflict_surfaces_as_a_store_error(monkeypatch, tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    DuckDbStrategyJobStore(store).create(
        "job-1", operation="market_strategy_pool", payload={}, description="建库"
    )
    store.ensure_state_database()
    monkeypatch.setattr(store_base_module, "connect_retry_delays", lambda: (0.0, 0.0))
    monkeypatch.setattr(
        store_base_module,
        "open_database",
        lambda path, *, read_only: (_ for _ in ()).throw(duckdb.IOException("Conflicting lock")),
    )

    with pytest.raises(store_base_module.MarketDataStoreError, match="状态库当前被其它进程占用"):
        store.state_read_session().__enter__()


def test_pool_tracking_throttle_sees_jobs_written_after_the_split(tmp_path: Path) -> None:
    """调度器要在状态库里查「今天已经尝试过几次」，否则 30 分钟下限永远不生效。

    行情库里的 strategy_jobs 是拆分时留下的旧副本：查它得到的最近一次尝试永远是几天前
    的记录，于是一轮扫完就再扫一轮，全市场每 60 秒被重扫一次（2026-10-28 实测）。
    """

    from aquest.strategy_pool.tracking_scheduler import PoolTrackingScheduler

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars())
    store.ensure_state_database()
    assert store.has_state_database() is True
    with store.state_write_transaction() as connection:
        connection.execute(
            """
            INSERT INTO pool_tracking_versions (
                version_id, market, code, objective, source, signature,
                candidate_json, created_at, is_current, summary_json, snapshot,
                status, last_error, updated_at
            ) VALUES ('v1', 'SH', '600000', 'annual_return', 'automatic', 'sig',
                      '{}', '2026-09-01T00:00:00', TRUE, ?, NULL, 'ready', '',
                      '2026-09-02T00:00:00')
            """,
            [json.dumps({"as_of_date": "2026-09-02", "holding": [], "full_period": {}})],
        )
    DuckDbStrategyJobStore(store).create(
        "scan-1",
        operation="market_strategy_pool_tracking",
        payload={"mode": "signals", "as_of_date": "2026-09-03"},
        description="每日策略池跟踪",
    )

    class Backend:
        def __init__(self) -> None:
            self.submitted: list[dict[str, object]] = []

        def active_snapshots(self) -> list[dict[str, object]]:
            return []

        def submit(self, _operation, payload, **_kwargs) -> str:  # noqa: ANN001, ANN003
            self.submitted.append(dict(payload))
            return "scan-2"

    class Runner:
        def has_active_jobs(self, _descriptions) -> bool:  # noqa: ANN001
            return False

    backend = Backend()
    scheduler = PoolTrackingScheduler(store, backend, Runner())

    assert scheduler.submit_if_due() is None
    assert backend.submitted == []


def test_tracking_runner_reads_pool_entries_from_the_state_database(tmp_path: Path) -> None:
    """跟踪回放要读到拆分后写进状态库的基准，否则质量判定会退回研究口径（或缺失）。"""

    from aquest.strategy_discovery.evaluation_budget import EvaluationWorkerBudget
    from aquest.strategy_pool.tracking_runner import PoolTrackingRunner

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars())
    store.ensure_state_database()
    assert store.has_state_database() is True
    DuckDbStrategyPoolStore(store).upsert_automatic(
        market="SH",
        code="600000",
        name="test",
        annual_candidate={"candidate_id": "c", "buy_rule": {}, "sell_rule": {}},
        sharpe_candidate=None,
        data_fingerprint="a",
        config_fingerprint="b",
        source_job_id="job",
        benchmark={"full_period": {"total_return": 0.2}},
    )
    runner = PoolTrackingRunner(store, EvaluationWorkerBudget(1))

    benchmarks, _ = runner._entry_context([{"market": "SH", "code": "600000"}])  # noqa: SLF001

    assert benchmarks[("SH", "600000")] == 0.2
