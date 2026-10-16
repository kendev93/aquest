"""库连接的重试阶梯：backend 快速失败，研究 worker 给足预算。

共享卷上 DuckDB 的读写打开是独占的，打不开时等多久才放弃，对两种调用方的代价
完全相反：页面下一秒还会再轮询，研究一次失败就废掉整只标的。这两条测试钉住的
就是这个差别，别再把它统一成一个阶梯。
"""

from __future__ import annotations

from collections.abc import Iterator

import duckdb
import pytest

from aquest.market_data import store_base
from aquest.market_data.store import DuckDbMarketDataStore
from aquest.market_data.store_base import (
    _CONNECT_RETRY_DELAYS,
    _RETRY_LADDER_CACHE,
    MarketDataStoreError,
    connect_retry_delays,
)
from aquest.strategy_discovery.duckdb_job_store import DuckDbStrategyJobStore

ENV = "AQUEST_DUCKDB_CONNECT_RETRY_SECONDS"


@pytest.fixture(autouse=True)
def clear_ladder_cache() -> Iterator[None]:
    _RETRY_LADDER_CACHE.clear()
    yield
    _RETRY_LADDER_CACHE.clear()


def test_unset_env_keeps_the_short_ladder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV, raising=False)

    assert connect_retry_delays() == _CONNECT_RETRY_DELAYS
    assert sum(_CONNECT_RETRY_DELAYS) < 4.0


@pytest.mark.parametrize("budget", ["6", "25", "60"])
def test_budget_expands_to_a_doubling_ladder_within_the_budget(
    monkeypatch: pytest.MonkeyPatch, budget: str
) -> None:
    monkeypatch.setenv(ENV, budget)

    delays = connect_retry_delays()

    assert delays and delays[0] == pytest.approx(0.1)
    assert sum(delays) <= float(budget)
    # 越等越长，但单步封顶 5 秒：给对端的写窗口留出空隙，又不会一次睡过分钟级。
    assert max(delays) <= 5.0
    assert len(delays) >= 3


def test_tiny_budget_never_overshoots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, "0.05")

    assert connect_retry_delays() == ()


@pytest.mark.parametrize("raw", ["abc", "-1", "0"])
def test_invalid_budget_is_a_configuration_error(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(ENV, raw)

    with pytest.raises(ValueError, match="AQUEST_DUCKDB_CONNECT_RETRY_SECONDS"):
        connect_retry_delays()


def test_connect_waits_the_configured_budget_before_giving_up(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真打不开的时候，研究进程要等满预算才抛错，而不是 4 秒就废掉一只标的。"""

    monkeypatch.setenv(ENV, "25")
    sleeps: list[float] = []
    monkeypatch.setattr(store_base.time, "sleep", sleeps.append)

    def refuse(*args: object, **kwargs: object) -> None:
        raise duckdb.Error("Could not set lock on file: Conflicting lock is held")

    monkeypatch.setattr(store_base.duckdb, "connect", refuse)
    (tmp_path / "market.duckdb").write_bytes(b"occupied")
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb", read_only=True)

    with pytest.raises(MarketDataStoreError):
        store._connect()

    assert sum(sleeps) > 20


def test_read_session_uses_the_read_only_fast_path(tmp_path, monkeypatch) -> None:
    """没人占库时，读应该只开只读连接，不额外要读写连接。"""

    opened: list[bool] = []
    original_open = DuckDbMarketDataStore._open

    def spy_open(self, *, read_only):  # noqa: ANN001
        opened.append(bool(read_only))
        return original_open(self, read_only=read_only)

    monkeypatch.setattr(DuckDbMarketDataStore, "_open", spy_open)
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store._ensure_initialized()
    opened.clear()

    with store.read_session() as connection:
        assert connection.execute("select 1").fetchone()[0] == 1

    assert opened == [True]


def test_read_session_falls_back_when_the_same_process_holds_a_write_connection(
    tmp_path, monkeypatch
) -> None:
    """同进程已有读写连接时只读打开会被拒绝（PID 0）：立刻退回读写并读到数据。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store._ensure_initialized()
    with store.write_transaction() as writer:
        writer.execute("create table markers (code varchar)")
        writer.execute("insert into markers values ('SH600000')")

    original_open = DuckDbMarketDataStore._open
    opened: list[bool] = []

    def refuse_read_only(self, *, read_only):  # noqa: ANN001
        opened.append(bool(read_only))
        if read_only:
            raise duckdb.Error("Could not set lock: Conflicting lock is held in PID 0")
        return original_open(self, read_only=read_only)

    monkeypatch.setattr(DuckDbMarketDataStore, "_open", refuse_read_only)

    with store.read_session() as connection:
        assert connection.execute("select code from markers").fetchone()[0] == "SH600000"

    assert opened == [True, False]


def test_read_only_store_still_waits_the_ladder_and_reports_the_lock(tmp_path, monkeypatch) -> None:
    """只读实例没有退路可退：照旧等满预算再报「库被占用」，不静默改变行为。"""

    (tmp_path / "market.duckdb").write_bytes(b"occupied")
    monkeypatch.setenv(ENV, "0.7")
    monkeypatch.setattr(store_base.time, "sleep", lambda *_: None)

    def refuse(*args: object, **kwargs: object) -> None:
        raise duckdb.Error("Could not set lock on file: Conflicting lock is held")

    monkeypatch.setattr(store_base.duckdb, "connect", refuse)
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb", read_only=True)

    with pytest.raises(MarketDataStoreError):
        store.read_session().__enter__()


def test_write_transaction_commits_and_rolls_back(tmp_path) -> None:
    """写事务要么整批提交、要么整批回滚，并且只占一条连接。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store._ensure_initialized()

    with store.write_transaction() as connection:
        connection.execute("create table pool_markers (code varchar)")
        connection.execute("insert into pool_markers values ('SH600000')")

    with store.read_session() as connection:
        assert connection.execute("select count(*) from pool_markers").fetchone()[0] == 1

    with pytest.raises(RuntimeError), store.write_transaction() as connection:
        connection.execute("insert into pool_markers values ('SZ000001')")
        raise RuntimeError("中途失败")

    with store.read_session() as connection:
        assert connection.execute("select count(*) from pool_markers").fetchone()[0] == 1


def test_job_store_reads_survive_a_refused_read_only_open(tmp_path, monkeypatch) -> None:
    """任务表读与池表读从此同一个策略：被自己打回时退回读写，不再硬失败。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    jobs = DuckDbStrategyJobStore(store)
    jobs.initialize()
    job_id = "0" * 32
    jobs.create(
        job_id,
        operation="market_strategy_pool",
        payload={"mode": "full"},
        description="全市场策略池研究",
    )

    original_open = DuckDbMarketDataStore._open

    def refuse_read_only(self, *, read_only):  # noqa: ANN001
        if read_only:
            raise duckdb.Error("Could not set lock: Conflicting lock is held in PID 0")
        return original_open(self, read_only=read_only)

    monkeypatch.setattr(DuckDbMarketDataStore, "_open", refuse_read_only)

    state = jobs.read_state(job_id)
    assert state is not None and state["status"] == "queued"
