"""后台调度只由「自己持有执行器」的那个进程启动。

嵌入方（或测试、worker）传了 runner 时，调度归它管；同一个库上再起一套只会在同一交易日
重复提交同步与跟踪扫描。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from aquest.market_data.store import DuckDbMarketDataStore
from aquest.screening.jobs import ScreenJobRunner
from aquest.web import app as app_module
from aquest.web.app import create_app


class _SpyScheduler:
    started: list[str] = []

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        _SpyScheduler.started.append(type(self).__name__)

    async def run(self, *_args: object, **_kwargs: object) -> None:
        return


def _install_spies(monkeypatch) -> None:
    for name in (
        "AutomaticMarketSyncScheduler",
        "StrategyTrackingScheduler",
        "PoolTrackingScheduler",
    ):
        monkeypatch.setattr(app_module, name, type(name, (_SpyScheduler,), {}))
    _SpyScheduler.started = []


def _create(**kwargs: object):
    return create_app(
        market_data_store=kwargs.pop("store"),
        auto_sync_enabled=True,
        strategy_tracking_enabled=True,
        pool_tracking_enabled=True,
        **kwargs,
    )


def test_an_injected_runner_does_not_start_a_second_set_of_schedulers(
    monkeypatch, tmp_path: Path
) -> None:
    _install_spies(monkeypatch)
    store = DuckDbMarketDataStore(tmp_path / "injected.duckdb")
    runner = ScreenJobRunner()
    try:
        with TestClient(_create(store=store, runner=runner)):
            assert _SpyScheduler.started == []
    finally:
        runner.shutdown()


def test_the_owning_process_starts_the_schedulers(monkeypatch, tmp_path: Path) -> None:
    _install_spies(monkeypatch)
    store = DuckDbMarketDataStore(tmp_path / "owner.duckdb")

    with TestClient(_create(store=store)):
        assert sorted(_SpyScheduler.started) == sorted(
            ["AutomaticMarketSyncScheduler", "PoolTrackingScheduler", "StrategyTrackingScheduler"]
        )
