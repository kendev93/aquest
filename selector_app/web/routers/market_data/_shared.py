"""State accessors and shared guards for the market-data routes.

只放被多个路由模块共用的东西：从 ``app.state`` 上取服务的取值函数、行情库
报错到用户可读文案的映射、以及「同一时间只允许一个行情任务」这道闸门。
"""

from __future__ import annotations

import os
from datetime import date
from typing import Protocol, cast

from fastapi import HTTPException, Request, status

from selector_app.adapters.market_sync import (
    AbortCheck,
    DetailCallback,
    InstrumentDiscoveryReport,
    MarketSyncConfig,
    MarketSyncReport,
    ProgressCallback,
)
from selector_app.indicators.cache import IndicatorCache
from selector_app.indicators.precompute import IndicatorPrecomputeService
from selector_app.market_data.auto_sync import MARKET_SYNC_JOB_DESCRIPTIONS
from selector_app.market_data.local_adapter import map_host_path
from selector_app.market_data.local_import import LocalDayImportService
from selector_app.market_data.models import DataStoreStatus, MarketCode, stored_end_year
from selector_app.market_data.service import (
    ChartPeriod,
    LocalInstrumentPage,
    LocalMarketChart,
)
from selector_app.market_data.store import DuckDbMarketDataStore, MarketDataStoreError
from selector_app.screening.jobs import ScreenJobRunner

from ...market_source import resolve_market_status


class MarketSyncService(Protocol):
    def discover_instruments(
        self,
        config: MarketSyncConfig,
        *,
        detail_callback: DetailCallback | None = None,
    ) -> InstrumentDiscoveryReport | dict[str, object]: ...

    def sync(
        self,
        config: MarketSyncConfig,
        progress_callback: ProgressCallback | None = None,
        *,
        detail_callback: DetailCallback | None = None,
        abort_check: AbortCheck | None = None,
    ) -> MarketSyncReport | dict[str, object]: ...


class LocalMarketDataServiceLike(Protocol):
    def list_instruments(
        self,
        vipdoc_path: str,
        *,
        market: str,
        keyword: str,
        page: int,
        page_size: int,
    ) -> LocalInstrumentPage: ...

    def get_chart(
        self,
        vipdoc_path: str,
        market: MarketCode,
        code: str,
        *,
        period: ChartPeriod,
        start_date: date | None,
        end_date: date | None,
        formula_ids: tuple[str, ...] = (),
    ) -> LocalMarketChart: ...


def _runner(request: Request) -> ScreenJobRunner:
    return cast(ScreenJobRunner, request.app.state.screen_job_runner)


def _service(request: Request) -> MarketSyncService:
    return cast(MarketSyncService, request.app.state.market_sync_service)


def _local_service(request: Request) -> LocalMarketDataServiceLike:
    return cast(LocalMarketDataServiceLike, request.app.state.local_market_data_service)


def _store(request: Request) -> DuckDbMarketDataStore:
    return cast(DuckDbMarketDataStore, request.app.state.market_data_store)


def _market_status(request: Request) -> DataStoreStatus:
    return resolve_market_status(request)


def _local_import_service(request: Request) -> LocalDayImportService:
    service = getattr(request.app.state, "local_day_import_service", None)
    if service is None:
        service = LocalDayImportService(_store(request))
        request.app.state.local_day_import_service = service
    return cast(LocalDayImportService, service)


def _precomputer(request: Request) -> IndicatorPrecomputeService:
    return cast(IndicatorPrecomputeService, request.app.state.indicator_precompute_service)


def _indicator_cache(request: Request) -> IndicatorCache:
    return cast(IndicatorCache, request.app.state.indicator_cache)


def _invalidate_indicator_cache(request: Request) -> None:
    """Drop in-process indicator results after market data changed.

    指标缓存的键里带着标的级版本号（`bar_count|last_date|updated_at`），而这个
    版本号在进程内是缓存住的：行情一变它必然变化，但缓存里的副本不会自己失效。
    于是「日期不变、数值变了」的重写（本地重新导入同一段历史就是这种）会命中
    旧条目，回测拿到上一版行情算出来的指标，且不报错。持久层反而是对的——新
    进程会重新查版本号，所以表现为「重启后端就正常」。

    内存缓存只有几百条，重建的代价远小于算错，所以这里直接整片丢掉。
    """

    # 进程里可能不止一份缓存（组合回测那份是独立实例），只清 app.state 那一份会
    # 让回测继续命中上一版行情算出的指标，且不报错。
    caches: list[IndicatorCache] = [
        item
        for item in getattr(request.app.state, "indicator_caches", None) or ()
        if isinstance(item, IndicatorCache)
    ]
    primary = getattr(request.app.state, "indicator_cache", None)
    if isinstance(primary, IndicatorCache) and not any(item is primary for item in caches):
        caches.insert(0, primary)
    for cache in caches:
        cache.clear_memory()


def _stored_end_year(status: DataStoreStatus) -> int | None:
    """Calendar year of the newest stored bar, or None when the store is empty."""

    return stored_end_year(status)


def _resolved_vipdoc_path(vipdoc_path: str | None) -> str:
    raw = vipdoc_path or os.getenv("SELECTOR_VIPDOC_PATH") or "/data/vipdoc"
    return map_host_path(raw)


def _reject_duplicate_market_job(runner: ScreenJobRunner) -> None:
    if runner.has_active_jobs(MARKET_SYNC_JOB_DESCRIPTIONS):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="已有行情同步任务正在运行，请先查看进度或取消后再试。",
        )


def _storage_error_message(exc: BaseException) -> str:
    if isinstance(exc, MarketDataStoreError):
        return str(exc)
    if isinstance(exc, FileNotFoundError):
        return "未找到行情数据库，请先同步行情后重试"
    detail = str(exc).strip()
    if isinstance(exc, RuntimeError) and "schema" in detail.lower():
        return detail[:240] or "行情数据库 schema 不兼容"
    message = str(exc).lower()
    if "lock" in message or "conflicting" in message:
        return "行情数据库当前被其它进程占用，请关闭其它写入进程后重试"
    return "行情数据库无法读取，请检查文件权限或数据库完整性"
