"""行情路由的注册守卫。

``routers/market_data`` 已经拆成包：新增子模块后如果忘了在 ``__init__`` 里
include，路由会**静默消失**——后端不报错，前端只表现为某个接口 404。这两条
断言分别钉住「子模块都被挂上了」和「实际暴露的路径没变」。

新增或删除行情接口时改 ``EXPECTED_ROUTES``；改动是预期的就更新它。
"""

from __future__ import annotations

import importlib
import pkgutil

from selector_app.web import routers
from selector_app.web.app import create_app
from selector_app.web.routers.market_data import _SUBROUTERS

EXPECTED_ROUTES = {
    "DELETE /api/v1/market-data/indicator-cache",
    "GET /api/v1/market-data/capabilities",
    "GET /api/v1/market-data/indicator-cache",
    "GET /api/v1/market-data/jobs/active",
    "GET /api/v1/market-data/jobs/{job_id}",
    "GET /api/v1/market-data/local-import/detect",
    "GET /api/v1/market-data/local/instruments",
    "GET /api/v1/market-data/local/{market}/{code}/bars",
    "GET /api/v1/market-data/store",
    "GET /api/v1/market-data/sync/jobs/{job_id}",
    "POST /api/v1/market-data/backfill",
    "POST /api/v1/market-data/bars/delete",
    "POST /api/v1/market-data/health/check",
    "POST /api/v1/market-data/health/repair",
    "POST /api/v1/market-data/indicator-cache/precompute",
    "POST /api/v1/market-data/jobs/{job_id}/cancel",
    "POST /api/v1/market-data/local-import",
    "POST /api/v1/market-data/sync",
    "POST /api/v1/market-data/sync-online",
}


def _submodule_routers() -> dict[str, object]:
    package = importlib.import_module(f"{routers.__name__}.market_data")
    found: dict[str, object] = {}
    for info in pkgutil.iter_modules(package.__path__):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{package.__name__}.{info.name}")
        sub_router = getattr(module, "router", None)
        if sub_router is not None:
            found[info.name] = sub_router
    return found


def test_every_market_data_submodule_is_mounted() -> None:
    submodules = _submodule_routers()
    assert submodules, "包里没找到任何子路由模块，拆分结构可能已经变了"

    mounted = {id(sub_router) for sub_router in _SUBROUTERS}
    missing = sorted(name for name, sub in submodules.items() if id(sub) not in mounted)
    assert not missing, f"这些子模块的路由没挂到 market-data 上: {missing}"


def test_market_data_routes_match_expected_set() -> None:
    exposed = {
        f"{verb.upper()} {route_path}"
        for route_path, operations in create_app().openapi()["paths"].items()
        if route_path.startswith("/api/v1/market-data")
        for verb in operations
    }
    assert exposed == EXPECTED_ROUTES, (
        f"行情接口集合变了，新增/删除是预期的就更新 EXPECTED_ROUTES: "
        f"{sorted(exposed ^ EXPECTED_ROUTES)}"
    )
