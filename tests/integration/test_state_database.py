"""状态表与行情分库：迁移只拷贝，之后的写入落在状态库，导出按表分派。

池条目、跟踪版本、任务这些写得最频繁又和日线合并没有关系的表拆到同目录的
``state.duckdb``；日线、指标缓存和各类历史留在行情库。行情库里的旧表原样保留，
所以删掉 ``state.duckdb`` 就能退回单库（看到的是拆分前的数据）。
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from aquest.market_data.store import DuckDbMarketDataStore
from aquest.strategy_discovery.duckdb_job_store import DuckDbStrategyJobStore
from aquest.strategy_pool.store import DuckDbStrategyPoolStore
from aquest.strategy_pool.tracking_store import PoolTrackingStore


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
