"""FastAPI application factory for the standalone selector."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from selector_app.adapters.market_data_sync import MarketDataSync
from selector_app.backtest.service import BacktestService
from selector_app.condition_research.service import ConditionResearchService
from selector_app.indicators.cache import IndicatorCache
from selector_app.indicators.precompute import IndicatorPrecomputeService
from selector_app.market_data.adapter import (
    BatchMarketDataAdapter,
    DuckDbMarketDataAdapter,
    MarketDataAdapter,
)
from selector_app.market_data.auto_sync import (
    DEFAULT_AUTO_SYNC_INTERVAL_SECONDS,
    AutomaticMarketSyncScheduler,
)
from selector_app.market_data.maintenance import (
    DatabaseMaintenanceScheduler,
    DatabaseMaintenanceService,
    resolve_backup_retention_days,
)
from selector_app.market_data.service import LocalMarketDataService
from selector_app.market_data.staging import cleanup_stale_staging, staging_directory
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.portfolio_backtest.service import PortfolioBacktestService
from selector_app.screening.engine import ScreenEngine
from selector_app.screening.jobs import (
    ScanEngineLike,
    ScreenJobRunner,
)
from selector_app.strategy_discovery.duckdb_job_store import DuckDbStrategyJobStore
from selector_app.strategy_discovery.job_backend import (
    DuckDbStrategyJobBackend,
    InProcessStrategyJobBackend,
    StrategyJobBackend,
)
from selector_app.strategy_discovery.job_store import (
    default_strategy_job_root,
)
from selector_app.strategy_discovery.resources import resolve_discovery_workers
from selector_app.strategy_discovery.service import StrategyDiscoveryService
from selector_app.strategy_fitness.service import StrategyFitnessService

from .routers.formula_screen import router as formula_screen_router

logger = logging.getLogger(__name__)


def _resolve_market_data_adapter(store: DuckDbMarketDataStore) -> MarketDataAdapter:
    """Return the adapter over this installation's own market database."""

    return DuckDbMarketDataAdapter(store)


def create_app(
    *,
    engine: ScanEngineLike | None = None,
    runner: ScreenJobRunner | None = None,
    market_sync: MarketDataSync | object | None = None,
    backtest_service: BacktestService | object | None = None,
    condition_research_service: ConditionResearchService | object | None = None,
    portfolio_backtest_service: PortfolioBacktestService | object | None = None,
    strategy_discovery_service: StrategyDiscoveryService | object | None = None,
    strategy_job_backend: StrategyJobBackend | object | None = None,
    strategy_fitness_service: StrategyFitnessService | object | None = None,
    local_market_data_service: LocalMarketDataService | object | None = None,
    market_data_store: DuckDbMarketDataStore | object | None = None,
    market_data_adapter: MarketDataAdapter | object | None = None,
    strategy_worker_token: str | None = None,
    indicator_cache: IndicatorCache | object | None = None,
    indicator_precompute_service: IndicatorPrecomputeService | object | None = None,
    database_maintenance_service: DatabaseMaintenanceService | object | None = None,
    auto_sync_enabled: bool | None = None,
    auto_sync_interval_seconds: float | None = None,
) -> FastAPI:
    owns_runner = runner is None
    selected_store = cast(
        DuckDbMarketDataStore,
        market_data_store if market_data_store is not None else DuckDbMarketDataStore(),
    )
    selected_strategy_job_store = DuckDbStrategyJobStore(selected_store)
    selected_adapter = cast(
        MarketDataAdapter,
        market_data_adapter
        if market_data_adapter is not None
        else _resolve_market_data_adapter(selected_store),
    )
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
    selected_market_sync = cast(
        MarketDataSync,
        market_sync if market_sync is not None else MarketDataSync(store=selected_store),
    )
    selected_auto_sync_enabled = _resolve_auto_sync_enabled(auto_sync_enabled)
    selected_auto_sync_interval = _resolve_auto_sync_interval(auto_sync_interval_seconds)
    selected_maintenance_service = cast(
        DatabaseMaintenanceService,
        database_maintenance_service
        if database_maintenance_service is not None
        else DatabaseMaintenanceService(
            selected_store.database_path,
            retention_days=resolve_backup_retention_days(),
        ),
    )
    selected_runner = runner or ScreenJobRunner(
        engine=engine
        or ScreenEngine(adapter=selected_adapter, indicator_cache=selected_indicator_cache)
    )
    selected_backtest_service = cast(
        BacktestService,
        backtest_service
        if backtest_service is not None
        else BacktestService(
            adapter=selected_adapter,
            indicator_cache=selected_indicator_cache,
        ),
    )
    selected_condition_research_service = cast(
        ConditionResearchService,
        condition_research_service
        if condition_research_service is not None
        else ConditionResearchService(
            adapter=selected_adapter,
            backtest_service=selected_backtest_service,
        ),
    )
    selected_portfolio_service = cast(
        PortfolioBacktestService,
        portfolio_backtest_service
        if portfolio_backtest_service is not None
        else PortfolioBacktestService(
            adapter=selected_adapter,
            indicator_cache=selected_indicator_cache,
        ),
    )
    selected_discovery_service = cast(
        StrategyDiscoveryService,
        strategy_discovery_service
        if strategy_discovery_service is not None
        else StrategyDiscoveryService(
            portfolio_service=selected_portfolio_service,
            store=selected_store,
            market_data_adapter=selected_adapter,
        ),
    )
    selected_strategy_backend = cast(
        StrategyJobBackend,
        strategy_job_backend
        if strategy_job_backend is not None
        else InProcessStrategyJobBackend(
            selected_store,
            selected_runner,
            selected_discovery_service,
        )
        if strategy_discovery_service is not None
        else DuckDbStrategyJobBackend(
            selected_store,
            selected_strategy_job_store,
            worker_count=resolve_discovery_workers(),
            market_data_adapter=cast(BatchMarketDataAdapter, selected_adapter),
        ),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.auto_sync_job_id = None
        auto_sync_task: asyncio.Task[None] | None = None
        maintenance_task: asyncio.Task[None] | None = None
        selected_store.status()
        imported_strategy_jobs = selected_strategy_job_store.initialize(
            default_strategy_job_root(selected_store.database_path)
        )
        if imported_strategy_jobs:
            logger.info(
                "已将 %d 条旧策略任务状态迁入 Indicator Lab DuckDB",
                imported_strategy_jobs,
            )
        try:
            cleanup_stale_staging(staging_directory(selected_store.database_path))
            maintenance_result = selected_maintenance_service.run_if_needed()
            app.state.database_maintenance_result = maintenance_result.to_dict()
            if maintenance_result.status == "failed":
                logger.warning("启动时 DuckDB 整理未完成: %s", maintenance_result.error)
        except Exception:  # noqa: BLE001 - maintenance failure must not block the app
            logger.exception("启动时 DuckDB 整理检查失败")
            app.state.database_maintenance_result = {
                "status": "failed",
                "reason": "maintenance_check_failed",
            }
        if owns_runner:
            maintenance_scheduler = DatabaseMaintenanceScheduler(
                selected_maintenance_service,
                selected_runner,
            )
            maintenance_task = asyncio.create_task(maintenance_scheduler.run())
        if selected_auto_sync_enabled:
            scheduler = AutomaticMarketSyncScheduler(
                store=selected_store,
                runner=selected_runner,
                market_sync=selected_market_sync,
                precomputer=selected_precompute_service,
                interval_seconds=selected_auto_sync_interval,
            )
            auto_sync_task = asyncio.create_task(
                scheduler.run(
                    on_job_submitted=lambda job_id: setattr(app.state, "auto_sync_job_id", job_id)
                )
            )
        try:
            yield
        finally:
            if maintenance_task is not None:
                maintenance_task.cancel()
                try:
                    await maintenance_task
                except asyncio.CancelledError:
                    pass
            if auto_sync_task is not None:
                auto_sync_task.cancel()
                try:
                    await auto_sync_task
                except asyncio.CancelledError:
                    pass
            if owns_runner:
                selected_runner.shutdown()

    app = FastAPI(
        title="Indicator Lab",
        description="基于项目自有 DuckDB 行情仓库的指标筛选、组合与回测 API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.screen_job_runner = selected_runner
    app.state.market_sync_service = selected_market_sync
    app.state.backtest_service = selected_backtest_service
    app.state.condition_research_service = selected_condition_research_service
    app.state.portfolio_backtest_service = selected_portfolio_service
    app.state.strategy_discovery_service = selected_discovery_service
    app.state.strategy_job_backend = selected_strategy_backend
    app.state.strategy_job_store = selected_strategy_job_store
    app.state.strategy_worker_token = _resolve_strategy_worker_token(strategy_worker_token)
    app.state.strategy_fitness_service = strategy_fitness_service or StrategyFitnessService(
        adapter=selected_adapter,
        indicator_cache=selected_indicator_cache,
    )
    app.state.market_data_store = selected_store
    app.state.market_data_adapter = selected_adapter
    app.state.indicator_cache = selected_indicator_cache
    app.state.indicator_precompute_service = selected_precompute_service
    app.state.local_market_data_service = local_market_data_service or LocalMarketDataService(
        adapter=selected_adapter,
        store=selected_store,
    )
    app.state.auto_sync_enabled = selected_auto_sync_enabled
    app.state.auto_sync_interval_seconds = selected_auto_sync_interval
    app.state.database_maintenance_service = selected_maintenance_service
    app.state.database_maintenance_result = None
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
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

    @app.get("/api/v1/authorization/status", include_in_schema=False, response_model=None)
    async def authorization_status() -> dict[str, object]:
        """Report that this installation does not gate features behind membership.

        The previous machine-bound ETFQuest licensing was removed together with
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

    from .routers.backtest import router as backtest_router
    from .routers.condition_research import router as condition_research_router
    from .routers.data_transfer import router as data_transfer_router
    from .routers.jobs import router as jobs_router
    from .routers.market_data import router as market_data_router
    from .routers.portfolio_backtest import router as portfolio_backtest_router
    from .routers.strategy_discovery import router as strategy_discovery_router
    from .routers.strategy_fitness import router as strategy_fitness_router
    from .routers.strategy_worker import router as strategy_worker_router

    app.include_router(formula_screen_router, prefix="/api/v1")
    app.include_router(jobs_router, prefix="/api/v1")
    app.include_router(market_data_router, prefix="/api/v1")
    app.include_router(data_transfer_router, prefix="/api/v1")
    app.include_router(backtest_router, prefix="/api/v1")
    app.include_router(condition_research_router, prefix="/api/v1")
    app.include_router(portfolio_backtest_router, prefix="/api/v1")
    app.include_router(strategy_discovery_router, prefix="/api/v1")
    app.include_router(strategy_fitness_router, prefix="/api/v1")
    app.include_router(strategy_worker_router)
    return app


def _resolve_strategy_worker_token(value: str | None) -> str | None:
    if value is not None:
        return value.strip() or None
    token_path = str(os.getenv("SELECTOR_STRATEGY_WORKER_TOKEN_FILE") or "").strip()
    if not token_path:
        return None
    path = Path(token_path).expanduser()
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return _generate_strategy_worker_token(path)
    except OSError:
        logger.exception("策略 worker token 文件不可读")
        return None
    return token or None


def _generate_strategy_worker_token(path: Path) -> str | None:
    """Create the worker token on first boot of a self-hosted installation.

    Generation stays opt-in: a deployment that mounts a token from elsewhere
    should fail loudly when the file is missing rather than silently start with
    a different secret than its worker.
    """

    if str(os.getenv("SELECTOR_STRATEGY_WORKER_TOKEN_AUTOGENERATE") or "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        logger.warning("策略 worker token 文件不存在: %s", path)
        return None
    if not path.parent.is_dir():
        logger.warning("策略 worker token 目录不存在，无法生成: %s", path.parent)
        return None
    token = secrets.token_hex(32)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(token)
    except OSError:
        logger.exception("策略 worker token 生成失败: %s", path)
        return None
    logger.info("已生成策略 worker token: %s", path)
    return token


def _resolve_auto_sync_enabled(value: bool | None) -> bool:
    if value is not None:
        if not isinstance(value, bool):
            raise ValueError("自动行情同步开关必须是布尔值")
        return value
    raw = os.getenv("SELECTOR_AUTO_SYNC_ENABLED")
    if raw is None:
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_auto_sync_interval(value: float | None) -> float:
    if value is None:
        raw = os.getenv("SELECTOR_AUTO_SYNC_CHECK_INTERVAL_SECONDS")
        if raw is None:
            return float(DEFAULT_AUTO_SYNC_INTERVAL_SECONDS)
        try:
            value = float(raw)
        except ValueError as exc:
            raise ValueError("自动行情同步检查间隔必须是数字") from exc
    if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError("自动行情同步检查间隔必须大于 0")
    return float(value)


app = create_app()
