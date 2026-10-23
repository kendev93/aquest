"""`GET /api/v1/release-status`：顶栏那条提示的接口面。

断言只钉两件事：状态里同时给出本机版本与上游版本（前端要拿它们做比较），以及读状态
永远不再打一次网络（检查是后台任务按天做的事，一次页面加载不该为出网等待）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aquest.market_data.store import DuckDbMarketDataStore
from aquest.release_check import ReleaseChecker, ReleaseInfo
from aquest.web.app import create_app

UTC = timezone.utc


def _checker(*, calls: list[str], result: ReleaseInfo | None) -> ReleaseChecker:
    def fetch(repository: str) -> ReleaseInfo | None:
        calls.append(repository)
        return result

    return ReleaseChecker(
        current_version="0.1.7",
        fetch=fetch,
        clock=lambda: datetime(2026, 10, 24, 2, 0, tzinfo=UTC),
        interval_seconds=3600,
    )


def _app(tmp_path: Path, checker: ReleaseChecker) -> TestClient:
    store = DuckDbMarketDataStore(tmp_path / "release-api.duckdb")
    app = create_app(
        market_data_store=store,
        auto_sync_enabled=False,
        strategy_tracking_enabled=False,
        pool_tracking_enabled=False,
        release_checker=checker,
    )
    return TestClient(app)


def test_release_status_reports_current_and_latest_without_refetching(tmp_path: Path) -> None:
    calls: list[str] = []
    checker = _checker(
        calls=calls,
        result=ReleaseInfo(
            tag="v0.1.8",
            version="v0.1.8",
            url="https://github.com/kendev93/aquest/releases/tag/v0.1.8",
            published_at="2026-10-24T02:00:00Z",
        ),
    )
    assert checker.check().update_available is True
    calls.clear()

    client = _app(tmp_path, checker)
    with client:
        first = client.get("/api/v1/release-status").json()["data"]
        second = client.get("/api/v1/release-status").json()["data"]

    assert first["current"] == "0.1.7"
    assert first["update_available"] is True
    assert first["latest"]["tag"] == "v0.1.8"
    assert first["latest"]["url"].endswith("/releases/tag/v0.1.8")
    assert first["checked_at"]
    # 读状态只回缓存：每次页面加载都去敲 GitHub 的话，限流只是时间问题。
    assert calls == []
    assert second == first


def test_release_status_says_no_update_when_versions_match(tmp_path: Path) -> None:
    calls: list[str] = []
    checker = _checker(
        calls=calls,
        result=ReleaseInfo(
            tag="v0.1.7",
            version="v0.1.7",
            url="https://github.com/kendev93/aquest/releases/tag/v0.1.7",
            published_at="2026-10-22T03:19:22Z",
        ),
    )

    with _app(tmp_path, checker) as client:
        data = client.get("/api/v1/release-status").json()["data"]

    assert data["update_available"] is False
    assert data["latest"]["tag"] == "v0.1.7"


def test_release_status_is_quiet_when_the_check_is_switched_off(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """离线部署把检查关掉之后，接口照常答"不知道"，而不是报错或 404。"""

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("检查关掉之后不该有任何出网请求")

    monkeypatch.setattr("aquest.release_check.urlopen", refuse)
    monkeypatch.setenv("AQUEST_UPDATE_CHECK", "off")
    store = DuckDbMarketDataStore(tmp_path / "release-off.duckdb")
    app = create_app(
        market_data_store=store,
        auto_sync_enabled=False,
        strategy_tracking_enabled=False,
        pool_tracking_enabled=False,
    )

    with TestClient(app) as client:
        data = client.get("/api/v1/release-status").json()["data"]

    assert data["enabled"] is False
    assert data["update_available"] is False
    assert data["latest"] is None


def test_app_wiring_follows_the_switch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """装配期的默认值：不显式关掉就开着（真正的检查由后台任务做，不进测试）。"""

    store = DuckDbMarketDataStore(tmp_path / "release-wiring.duckdb")

    monkeypatch.setenv("AQUEST_UPDATE_CHECK", "1")
    assert create_app(market_data_store=store).state.release_checker.enabled is True

    monkeypatch.setenv("AQUEST_UPDATE_CHECK", "off")
    assert create_app(market_data_store=store).state.release_checker.enabled is False
