"""URL-level assembly: business routers plus the meta endpoints.

新增功能的路由注册集中在这里，``create_app`` 不再需要为每个新模块加两行。
业务路由统一挂 ``/api/v1``；策略 worker 的路由自带前缀，不重复加。
"""

from __future__ import annotations

from fastapi import FastAPI


def install_meta_endpoints(app: FastAPI) -> None:
    """Register the endpoints that are not part of the documented API surface."""

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/authorization/status", include_in_schema=False, response_model=None)
    async def authorization_status() -> dict[str, object]:
        """Report that this installation does not gate features behind membership.

        The previous machine-bound licensing was removed together with
        the shared market-data integration.  Account-based sign-in is planned,
        so the endpoint stays in place and keeps its shape for existing clients.
        """

        return {
            "data": {
                "required": False,
                "available": True,
                "can_access_core": True,
                "access_state": "unrestricted",
                "message": "本机版不校验会员",
            }
        }


def install_routers(app: FastAPI) -> None:
    from .routers.candidate_library import router as candidate_library_router
    from .routers.condition_research import router as condition_research_router
    from .routers.data_transfer import router as data_transfer_router
    from .routers.factor_attribution import router as factor_attribution_router
    from .routers.factor_test import router as factor_test_router
    from .routers.formula_screen import router as formula_screen_router
    from .routers.history_compare import router as history_compare_router
    from .routers.instrument_lab import router as instrument_lab_router
    from .routers.jobs import router as jobs_router
    from .routers.market_data import router as market_data_router
    from .routers.portfolio_backtest import router as portfolio_backtest_router
    from .routers.research_presets import router as research_presets_router
    from .routers.screen_presets import router as screen_presets_router
    from .routers.stock_groups import router as stock_groups_router
    from .routers.strategies import router as strategies_router
    from .routers.strategy_discovery import router as strategy_discovery_router
    from .routers.strategy_fitness import router as strategy_fitness_router
    from .routers.strategy_worker import router as strategy_worker_router
    from .routers.swing_research import router as swing_research_router

    app.include_router(candidate_library_router, prefix="/api/v1")
    app.include_router(formula_screen_router, prefix="/api/v1")
    app.include_router(jobs_router, prefix="/api/v1")
    app.include_router(market_data_router, prefix="/api/v1")
    app.include_router(data_transfer_router, prefix="/api/v1")
    app.include_router(condition_research_router, prefix="/api/v1")
    app.include_router(factor_test_router, prefix="/api/v1")
    app.include_router(factor_attribution_router, prefix="/api/v1")
    app.include_router(history_compare_router, prefix="/api/v1")
    app.include_router(instrument_lab_router, prefix="/api/v1")
    app.include_router(portfolio_backtest_router, prefix="/api/v1")
    app.include_router(research_presets_router, prefix="/api/v1")
    app.include_router(screen_presets_router, prefix="/api/v1")
    app.include_router(stock_groups_router, prefix="/api/v1")
    app.include_router(strategy_discovery_router, prefix="/api/v1")
    app.include_router(strategy_fitness_router, prefix="/api/v1")
    app.include_router(strategies_router, prefix="/api/v1")
    app.include_router(swing_research_router, prefix="/api/v1")
    app.include_router(strategy_worker_router)
