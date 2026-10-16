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
