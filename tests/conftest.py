"""Shared pytest configuration for isolated application tests."""

from __future__ import annotations

import pytest

from aquest.adapters import online_market_sync
from aquest.adapters.baostock_catalog import BaostockUnavailable


@pytest.fixture(autouse=True)
def disable_automatic_market_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ordinary tests offline; scheduler tests opt in explicitly."""

    monkeypatch.setenv("AQUEST_AUTO_SYNC_ENABLED", "0")


@pytest.fixture(autouse=True)
def disable_release_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认关掉上游版本检查。

    它是 app 装配期起来的后台任务，不关掉的话每个 `create_app` 的测试都会真的去连
    GitHub——慢、看网络、还可能在 CI 里被限流。版本检查自己的用例注入假的上游，
    或者显式改回这个环境变量。
    """

    monkeypatch.setenv("AQUEST_UPDATE_CHECK", "off")


@pytest.fixture(autouse=True)
def disable_real_baostock_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the baostock catalog offline too.

    baostock 是可选依赖，装了之后目录发现会真的去连它的 TCP 服务——那是联网
    且串行的，不能进测试。默认当作「没装」，让目录走东财回退路径；专门验证
    baostock 的用例自己 monkeypatch 覆盖回来。
    """

    def refuse(**_kwargs: object) -> object:
        raise BaostockUnavailable("测试中未启用 baostock 目录源")

    monkeypatch.setattr(online_market_sync, "fetch_all_securities", refuse)


@pytest.fixture(autouse=True)
def isolate_default_duckdb(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let app-factory tests initialize the user's home database."""

    monkeypatch.setenv("AQUEST_DATA_DIR", str(tmp_path / "default-aquest"))
