"""FastAPI application factory for the standalone selector."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from aquest.adapters.market_data_sync import MarketDataSync
from aquest.backtest.service import BacktestService
from aquest.candidate_library.service import CandidateLibraryService
from aquest.condition_research.service import ConditionResearchService
from aquest.factor_attribution.service import FactorAttributionService
from aquest.factor_test.service import FactorTestService
from aquest.history_compare.service import HistoryCompareService
from aquest.indicators.cache import IndicatorCache
from aquest.indicators.precompute import IndicatorPrecomputeService
from aquest.individual_research.service import IndividualStockResearchService
from aquest.instrument_lab.service import InstrumentLabService
from aquest.market_data.adapter import (
    BatchMarketDataAdapter,
    DuckDbMarketDataAdapter,
    MarketDataAdapter,
)
from aquest.market_data.auto_sync import AutomaticMarketSyncScheduler
from aquest.market_data.maintenance import (
    DatabaseMaintenanceScheduler,
    DatabaseMaintenanceService,
    resolve_backup_retention_days,
)
from aquest.market_data.service import LocalMarketDataService
from aquest.market_data.staging import cleanup_stale_staging, staging_directory
from aquest.market_data.store import DuckDbMarketDataStore
from aquest.portfolio_backtest.auto_tracking import StrategyTrackingScheduler
from aquest.portfolio_backtest.service import PortfolioBacktestService
from aquest.research_presets.service import ResearchPresetService
from aquest.screen_presets.service import ScreenPresetService
from aquest.screening.engine import ScreenEngine
from aquest.screening.jobs import (
    ScanEngineLike,
    ScreenJobRunner,
)
from aquest.stock_groups.service import StockGroupService
from aquest.strategy_discovery.duckdb_job_store import DuckDbStrategyJobStore
from aquest.strategy_discovery.job_backend import (
    DuckDbStrategyJobBackend,
    InProcessStrategyJobBackend,
    StrategyJobBackend,
)
from aquest.strategy_discovery.job_store import default_strategy_job_root
from aquest.strategy_discovery.resources import resolve_discovery_workers
from aquest.strategy_discovery.service import StrategyDiscoveryService
from aquest.strategy_fitness.service import StrategyFitnessService
from aquest.strategy_pool.store import DuckDbStrategyPoolStore
from aquest.strategy_pool.tracking_scheduler import PoolTrackingScheduler
from aquest.strategy_pool.tracking_store import PoolTrackingStore
from aquest.swing_research.service import SwingResearchService

from .app_errors import install_error_handlers
from .app_routes import install_meta_endpoints, install_routers
from .app_settings import (
    _resolve_auto_sync_enabled,
    _resolve_auto_sync_interval,
    _resolve_strategy_tracking_enabled,
    _resolve_strategy_tracking_interval,
    _resolve_strategy_worker_token,
)

logger = logging.getLogger(__name__)


async def _cancel_background_tasks(tasks: list[asyncio.Task[None] | None]) -> None:
    """Cancel and drain every lifespan task even if one already failed."""

    active = [task for task in tasks if task is not None]
    for task in active:
        task.cancel()
    if not active:
        return
    outcomes = await asyncio.gather(*active, return_exceptions=True)
    for outcome in outcomes:
        if isinstance(outcome, BaseException) and not isinstance(outcome, asyncio.CancelledError):
            logger.error(
                "后台 lifespan 任务退出时发生异常",
                exc_info=(type(outcome), outcome, outcome.__traceback__),
            )


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
    individual_research_service: IndividualStockResearchService | object | None = None,
    swing_research_service: SwingResearchService | object | None = None,
    factor_test_service: FactorTestService | object | None = None,
    factor_attribution_service: FactorAttributionService | object | None = None,
    history_compare_service: HistoryCompareService | object | None = None,
    instrument_lab_service: InstrumentLabService | object | None = None,
    screen_preset_service: ScreenPresetService | object | None = None,
    research_preset_service: ResearchPresetService | object | None = None,
    candidate_library_service: CandidateLibraryService | object | None = None,
    stock_group_service: StockGroupService | object | None = None,
    local_market_data_service: LocalMarketDataService | object | None = None,
    market_data_store: DuckDbMarketDataStore | object | None = None,
    market_data_adapter: MarketDataAdapter | object | None = None,
    strategy_worker_token: str | None = None,
    indicator_cache: IndicatorCache | object | None = None,
    indicator_precompute_service: IndicatorPrecomputeService | object | None = None,
    database_maintenance_service: DatabaseMaintenanceService | object | None = None,
    auto_sync_enabled: bool | None = None,
    auto_sync_interval_seconds: float | None = None,
    strategy_tracking_enabled: bool | None = None,
    pool_tracking_enabled: bool | None = None,
    strategy_tracking_interval_seconds: float | None = None,
) -> FastAPI:
    owns_runner = runner is None
    selected_store = cast(
        DuckDbMarketDataStore,
        market_data_store if market_data_store is not None else DuckDbMarketDataStore(),
    )
    selected_strategy_job_store = DuckDbStrategyJobStore(selected_store)
    selected_strategy_pool_store = DuckDbStrategyPoolStore(selected_store)
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
    selected_tracking_enabled = _resolve_strategy_tracking_enabled(strategy_tracking_enabled)
    selected_tracking_interval = _resolve_strategy_tracking_interval(
        strategy_tracking_interval_seconds
    )
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
    # 回测用一份独立的缓存（内存上限更小），读缓存但不写缓存：缓存键是标的级数据
    # 版本、存的是全历史，所以窗口请求能命中预计算出来的结果；写入留给预计算任务
    # 单线程批量做——8 个线程抢同一把写锁实测比计算贵三个数量级。
    #
    # 但「独立」不等于可以不被失效：行情重写后它的版本号同样会陈旧，于是两份都要
    # 登记进 ``app.state.indicator_caches``，由 _invalidate_indicator_cache 一起清。
    selected_backtest_cache = IndicatorCache(selected_store, max_memory_entries=512)
    selected_portfolio_service = cast(
        PortfolioBacktestService,
        portfolio_backtest_service
        if portfolio_backtest_service is not None
        else PortfolioBacktestService(
            adapter=selected_adapter,
            indicator_cache=selected_backtest_cache,
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
    selected_individual_research_service = cast(
        IndividualStockResearchService,
        individual_research_service
        if individual_research_service is not None
        else IndividualStockResearchService(
            adapter=selected_adapter,
            backtest_service=selected_backtest_service,
            portfolio_service=selected_portfolio_service,
            strategy_service=selected_discovery_service,
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

    def has_active_strategy_jobs() -> bool:
        return bool(selected_strategy_backend.active_snapshots())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.auto_sync_job_id = None
        auto_sync_task: asyncio.Task[None] | None = None
        maintenance_task: asyncio.Task[None] | None = None
        tracking_task: asyncio.Task[None] | None = None
        pool_tracking_task: asyncio.Task[None] | None = None
        selected_store.status()
        selected_strategy_pool_store.initialize()
        PoolTrackingStore(selected_store).bootstrap()
        imported_strategy_jobs = selected_strategy_job_store.initialize(
            default_strategy_job_root(selected_store.database_path)
        )
        if imported_strategy_jobs:
            logger.info(
                "已将 %d 条旧策略任务状态迁入 AQuest DuckDB",
                imported_strategy_jobs,
            )
        try:
            cleanup_stale_staging(staging_directory(selected_store.database_path))
            if selected_runner.has_active_jobs() or has_active_strategy_jobs():
                app.state.database_maintenance_result = {
                    "status": "skipped",
                    "reason": "active_jobs",
                }
            else:
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
                additional_busy_check=has_active_strategy_jobs,
            )
            maintenance_task = asyncio.create_task(maintenance_scheduler.run())
        if selected_auto_sync_enabled:
            scheduler = AutomaticMarketSyncScheduler(
                store=selected_store,
                runner=selected_runner,
                market_sync=selected_market_sync,
                precomputer=selected_precompute_service,
                interval_seconds=selected_auto_sync_interval,
                indicator_cache=selected_indicator_cache,
            )
            auto_sync_task = asyncio.create_task(
                scheduler.run(
                    on_job_submitted=lambda job_id: setattr(app.state, "auto_sync_job_id", job_id)
                )
            )
        if selected_tracking_enabled:
            tracking_task = asyncio.create_task(
                StrategyTrackingScheduler(
                    store=selected_store,
                    runner=selected_runner,
                    runner_service=selected_portfolio_service,
                    interval_seconds=selected_tracking_interval,
                ).run()
            )
        if pool_tracking_enabled is True or (
            pool_tracking_enabled is None
            and auto_sync_enabled is not False
            and os.getenv("AQUEST_POOL_TRACKING_ENABLED", "1").strip().lower()
            not in {"0", "false", "off"}
        ):
            pool_tracking_task = asyncio.create_task(
                PoolTrackingScheduler(
                    selected_store, selected_strategy_backend, selected_runner
                ).run()
            )
        try:
            yield
        finally:
            try:
                await _cancel_background_tasks(
                    [pool_tracking_task, maintenance_task, auto_sync_task, tracking_task]
                )
            finally:
                if owns_runner:
                    selected_runner.shutdown()

    app = FastAPI(
        title="AQuest",
        description="基于项目自有 DuckDB 行情仓库的指标筛选、组合与回测 API",
        version="0.1.2",
        lifespan=lifespan,
    )
    app.state.screen_job_runner = selected_runner
    app.state.market_sync_service = selected_market_sync
    app.state.condition_research_service = selected_condition_research_service
    app.state.portfolio_backtest_service = selected_portfolio_service
    app.state.strategy_discovery_service = selected_discovery_service
    app.state.individual_research_service = selected_individual_research_service
    app.state.strategy_job_backend = selected_strategy_backend
    app.state.strategy_job_store = selected_strategy_job_store
    app.state.strategy_pool_store = selected_strategy_pool_store
    app.state.strategy_worker_token = _resolve_strategy_worker_token(strategy_worker_token)
    app.state.strategy_fitness_service = strategy_fitness_service or StrategyFitnessService(
        adapter=selected_adapter,
        indicator_cache=selected_indicator_cache,
    )
    app.state.swing_research_service = swing_research_service or SwingResearchService(
        adapter=cast(BatchMarketDataAdapter, selected_adapter),
    )
    app.state.factor_test_service = factor_test_service or FactorTestService(
        adapter=cast(BatchMarketDataAdapter, selected_adapter),
    )
    app.state.factor_attribution_service = factor_attribution_service or FactorAttributionService(
        adapter=cast(BatchMarketDataAdapter, selected_adapter),
        store=selected_store,
    )
    app.state.history_compare_service = history_compare_service or HistoryCompareService(
        selected_store,
    )
    app.state.instrument_lab_service = instrument_lab_service or InstrumentLabService(
        selected_store,
        backtest_service=BacktestService(
            adapter=selected_adapter,
            indicator_cache=selected_indicator_cache,
        ),
    )
    app.state.screen_preset_service = screen_preset_service or ScreenPresetService(
        selected_store,
    )
    app.state.research_preset_service = research_preset_service or ResearchPresetService(
        selected_store,
    )
    app.state.candidate_library_service = candidate_library_service or CandidateLibraryService(
        selected_store,
    )
    app.state.stock_group_service = stock_group_service or StockGroupService(
        selected_store,
    )
    app.state.market_data_store = selected_store
    app.state.market_data_adapter = selected_adapter
    app.state.backtest_service = selected_backtest_service
    app.state.indicator_cache = selected_indicator_cache
    # 行情重写后要一起失效的全部缓存实例（组合回测那份是独立实例）。
    app.state.indicator_caches = [selected_indicator_cache, selected_backtest_cache]
    app.state.indicator_precompute_service = selected_precompute_service
    app.state.local_market_data_service = local_market_data_service or LocalMarketDataService(
        adapter=selected_adapter,
        store=selected_store,
    )
    app.state.auto_sync_enabled = selected_auto_sync_enabled
    app.state.auto_sync_interval_seconds = selected_auto_sync_interval
    app.state.strategy_tracking_enabled = selected_tracking_enabled
    app.state.strategy_tracking_interval_seconds = selected_tracking_interval
    app.state.database_maintenance_service = selected_maintenance_service
    app.state.database_maintenance_result = None
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type"],
    )

    install_error_handlers(app)
    install_meta_endpoints(app)
    install_routers(app)
    return app


app = create_app()
