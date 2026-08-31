"""FastAPI application factory for the standalone selector."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from selector_app.adapters.market_sync import TdxMarketSync
from selector_app.backtest.service import BacktestService
from selector_app.indicators.cache import IndicatorCache
from selector_app.indicators.precompute import IndicatorPrecomputeService
from selector_app.market_data.adapter import DuckDbMarketDataAdapter
from selector_app.market_data.day_importer import LocalDayImporter
from selector_app.market_data.models import ImportReport
from selector_app.market_data.service import LocalMarketDataService
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.portfolio_backtest.service import PortfolioBacktestService
from selector_app.screening.engine import ScreenEngine
from selector_app.screening.jobs import ScanEngineLike, ScreenJobRunner
from selector_app.strategy_fitness.service import StrategyFitnessService

from .routers.formula_screen import router as formula_screen_router

logger = logging.getLogger(__name__)


def create_app(
    *,
    engine: ScanEngineLike | None = None,
    runner: ScreenJobRunner | None = None,
    market_sync: TdxMarketSync | object | None = None,
    backtest_service: BacktestService | object | None = None,
    portfolio_backtest_service: PortfolioBacktestService | object | None = None,
    strategy_fitness_service: StrategyFitnessService | object | None = None,
    local_market_data_service: LocalMarketDataService | object | None = None,
    market_data_store: DuckDbMarketDataStore | object | None = None,
    local_day_importer: LocalDayImporter | object | None = None,
    indicator_cache: IndicatorCache | object | None = None,
    indicator_precompute_service: IndicatorPrecomputeService | object | None = None,
) -> FastAPI:
    owns_runner = runner is None
    selected_store = cast(
        DuckDbMarketDataStore,
        market_data_store if market_data_store is not None else DuckDbMarketDataStore(),
    )
    selected_adapter = DuckDbMarketDataAdapter(selected_store)
    selected_indicator_cache = cast(
        IndicatorCache,
        indicator_cache if indicator_cache is not None else IndicatorCache(selected_store),
    )
    selected_precompute_service = cast(
        IndicatorPrecomputeService,
        indicator_precompute_service
        if indicator_precompute_service is not None
        else IndicatorPrecomputeService(selected_adapter, selected_indicator_cache),
    )
    selected_runner = runner or ScreenJobRunner(
        engine=engine
        or ScreenEngine(adapter=selected_adapter, indicator_cache=selected_indicator_cache)
    )
    selected_importer = cast(
        LocalDayImporter,
        local_day_importer if local_day_importer is not None else LocalDayImporter(selected_store),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.startup_import_job_id = None
        if owns_runner:
            vipdoc_root = Path(os.getenv("SELECTOR_VIPDOC_PATH") or "/data/vipdoc").expanduser()
            has_day_files = vipdoc_root.is_dir() and any(
                directory.glob("*.day")
                for directory in (
                    vipdoc_root / "sh" / "lday",
                    vipdoc_root / "sz" / "lday",
                )
            )
            if has_day_files:

                def run_startup_import(progress: Callable[[int, int], None]) -> dict[str, object]:
                    result = selected_importer.import_vipdoc(
                        vipdoc_root,
                        progress_callback=progress,
                    )
                    payload = result.to_dict() if isinstance(result, ImportReport) else result
                    try:
                        precomputed = selected_precompute_service.run(
                            vipdoc_path=str(vipdoc_root),
                            progress_callback=progress,
                        )
                        return {**payload, "indicator_precompute": precomputed.to_dict()}
                    except (OSError, RuntimeError, ValueError):
                        logger.exception("启动导入后的指标预计算失败")
                        return {
                            **payload,
                            "indicator_precompute": {
                                "status": "failed",
                                "message": "指标预计算未完成，后续筛选或回测会按需重建",
                            },
                        }

                try:
                    app.state.startup_import_job_id = selected_runner.submit_callable(
                        run_startup_import,
                        description="本地行情自动导入",
                    )
                except RuntimeError:
                    logger.exception("无法提交本地行情自动导入任务")
        yield
        if owns_runner:
            selected_runner.shutdown()

    app = FastAPI(
        title="Indicator Lab",
        description="基于项目自有 DuckDB 行情仓库的指标筛选、组合与回测 API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.screen_job_runner = selected_runner
    app.state.market_sync_service = market_sync or TdxMarketSync(store=selected_store)
    app.state.backtest_service = backtest_service or BacktestService(
        adapter=selected_adapter,
        indicator_cache=selected_indicator_cache,
    )
    app.state.portfolio_backtest_service = portfolio_backtest_service or PortfolioBacktestService(
        adapter=selected_adapter,
        indicator_cache=selected_indicator_cache,
    )
    app.state.strategy_fitness_service = strategy_fitness_service or StrategyFitnessService(
        adapter=selected_adapter,
        indicator_cache=selected_indicator_cache,
    )
    app.state.market_data_store = selected_store
    app.state.indicator_cache = selected_indicator_cache
    app.state.indicator_precompute_service = selected_precompute_service
    app.state.local_day_importer = selected_importer
    app.state.local_market_data_service = local_market_data_service or LocalMarketDataService(
        store=selected_store
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        messages = [error.get("msg", "请求参数无效") for error in exc.errors()]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "；".join(messages),
                }
            },
        )

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        code_by_status = {
            404: "not_found",
            409: "conflict",
            422: "validation_error",
            503: "service_unavailable",
        }
        message = exc.detail if isinstance(exc.detail, str) else "请求失败"
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": code_by_status.get(exc.status_code, "request_error"),
                    "message": message,
                }
            },
        )

    @app.exception_handler(Exception)
    async def unexpected_error(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理的 API 异常", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal_error", "message": "服务器内部错误，请稍后重试"}},
        )

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    from .routers.backtest import router as backtest_router
    from .routers.market_data import router as market_data_router
    from .routers.portfolio_backtest import router as portfolio_backtest_router
    from .routers.strategy_fitness import router as strategy_fitness_router

    app.include_router(formula_screen_router, prefix="/api/v1")
    app.include_router(market_data_router, prefix="/api/v1")
    app.include_router(backtest_router, prefix="/api/v1")
    app.include_router(portfolio_backtest_router, prefix="/api/v1")
    app.include_router(strategy_fitness_router, prefix="/api/v1")
    return app


app = create_app()
