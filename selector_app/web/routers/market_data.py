"""Online market-data synchronization endpoints."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import date
from typing import Literal, Protocol, cast

import duckdb
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi import Path as PathParameter
from fastapi.responses import JSONResponse

from selector_app.adapters.market_sync import MarketSyncConfig, MarketSyncReport, ProgressCallback
from selector_app.indicators.cache import IndicatorCache
from selector_app.indicators.precompute import IndicatorPrecomputeService
from selector_app.market_data.auto_sync import AUTO_SYNC_DESCRIPTION
from selector_app.market_data.day_importer import ProgressCallback as LocalImportProgressCallback
from selector_app.market_data.models import (
    ImportReport,
    InstrumentBoard,
    InstrumentType,
    MarketCode,
)
from selector_app.market_data.service import (
    ChartPeriod,
    LocalInstrumentPage,
    LocalMarketChart,
)
from selector_app.market_data.store import DuckDbMarketDataStore, MarketDataStoreError
from selector_app.screening.jobs import ScreenJobRunner, TaskUserError

from ..schemas import LocalMarketImportRequest, MarketSyncRequest

router = APIRouter(prefix="/market-data", tags=["market-data"])


class MarketSyncService(Protocol):
    def sync(
        self,
        config: MarketSyncConfig,
        progress_callback: LocalImportProgressCallback | None = None,
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
    ) -> LocalMarketChart: ...


class LocalDayImporterLike(Protocol):
    def import_vipdoc(
        self,
        vipdoc_path: str,
        *,
        universe: str = "all",
        instrument_types: list[InstrumentType] | None = None,
        boards: list[InstrumentBoard] | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> ImportReport | dict[str, object]: ...


def _runner(request: Request) -> ScreenJobRunner:
    return cast(ScreenJobRunner, request.app.state.screen_job_runner)


def _service(request: Request) -> MarketSyncService:
    return cast(MarketSyncService, request.app.state.market_sync_service)


def _local_service(request: Request) -> LocalMarketDataServiceLike:
    return cast(LocalMarketDataServiceLike, request.app.state.local_market_data_service)


def _store(request: Request) -> DuckDbMarketDataStore:
    return cast(DuckDbMarketDataStore, request.app.state.market_data_store)


def _importer(request: Request) -> LocalDayImporterLike:
    return cast(LocalDayImporterLike, request.app.state.local_day_importer)


def _precomputer(request: Request) -> IndicatorPrecomputeService:
    return cast(IndicatorPrecomputeService, request.app.state.indicator_precompute_service)


def _indicator_cache(request: Request) -> IndicatorCache:
    return cast(IndicatorCache, request.app.state.indicator_cache)


def _resolved_vipdoc_path(vipdoc_path: str | None) -> str:
    return vipdoc_path or os.getenv("SELECTOR_VIPDOC_PATH") or "/data/vipdoc"


def _sync_config(payload: MarketSyncRequest, *, prefer_store_targets: bool) -> MarketSyncConfig:
    return MarketSyncConfig(
        universe=payload.universe,
        bars=payload.bars,
        instrument_types=tuple(payload.instrument_types or ()),
        boards=tuple(payload.boards or ()),
        prefer_store_targets=prefer_store_targets,
        refresh_names=prefer_store_targets,
    )


def _result_dict(result: MarketSyncReport | ImportReport | dict[str, object]) -> dict[str, object]:
    return result.to_dict() if isinstance(result, (MarketSyncReport, ImportReport)) else result


def _with_indicator_precompute(
    request: Request,
    result: dict[str, object],
    *,
    vipdoc_path: str | None,
    universe: str,
    instrument_types: list[InstrumentType] | tuple[InstrumentType, ...] | None,
    boards: list[InstrumentBoard] | tuple[InstrumentBoard, ...] | None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict[str, object]:
    try:
        precomputed = _precomputer(request).run(
            vipdoc_path=vipdoc_path or _resolved_vipdoc_path(None),
            universe=universe,
            instrument_types=tuple(instrument_types or ()),
            boards=tuple(boards or ()),
            progress_callback=progress_callback,
        )
        return {**result, "indicator_precompute": precomputed.to_dict()}
    except (OSError, ValueError, MarketDataStoreError, duckdb.Error):
        # Indicator materialization is derived data. A source-data import or
        # sync remains successful if warming the optional cache is unavailable.
        return {
            **result,
            "indicator_precompute": {
                "status": "failed",
                "message": "指标预计算未完成，后续筛选或回测会按需重建",
            },
        }


def _combined_result(
    local_result: dict[str, object],
    online_result: dict[str, object],
) -> dict[str, object]:
    return {
        **online_result,
        "source": "combined",
        "local_import": local_result,
        "online_sync": online_result,
    }


@router.get("/indicator-cache", response_model=None)
def indicator_cache_status(request: Request) -> dict[str, object] | JSONResponse:
    try:
        return {"data": _indicator_cache(request).status()}
    except (OSError, MarketDataStoreError, duckdb.Error) as exc:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": {
                    "code": "indicator_cache_error",
                    "message": _storage_error_message(exc),
                }
            },
        )


@router.post(
    "/indicator-cache/precompute",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=None,
)
def create_indicator_precompute_job(
    payload: MarketSyncRequest,
    request: Request,
) -> JSONResponse:
    def run_precompute(progress: Callable[[int, int], None]) -> dict[str, object]:
        try:
            report = _precomputer(request).run(
                vipdoc_path=_resolved_vipdoc_path(payload.vipdoc_path),
                universe=payload.universe,
                instrument_types=tuple(payload.instrument_types or ()),
                boards=tuple(payload.boards or ()),
                progress_callback=progress,
            )
        except (OSError, ValueError, MarketDataStoreError, duckdb.Error) as exc:
            raise TaskUserError(_storage_error_message(exc)) from exc
        return report.to_dict()

    try:
        job_id = _runner(request).submit_callable(run_precompute, description="指标预计算")
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="任务执行器暂不可用") from exc
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"data": {"job_id": job_id, "status": "queued"}},
    )


@router.post("/import-local", status_code=status.HTTP_202_ACCEPTED)
def create_local_import(
    payload: LocalMarketImportRequest,
    request: Request,
) -> JSONResponse:
    root = os.path.abspath(os.path.expanduser(payload.vipdoc_path))
    if not os.path.isdir(root):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "code": "data_directory_error",
                    "message": f"vipdoc 目录不存在或不是目录: {root}",
                }
            },
        )

    def run_import(progress: Callable[[int, int], None]) -> dict[str, object]:
        try:
            result = _importer(request).import_vipdoc(
                root,
                universe=payload.universe,
                instrument_types=payload.instrument_types,
                boards=payload.boards,
                progress_callback=progress,
            )
        except (MarketDataStoreError, duckdb.Error) as exc:
            raise TaskUserError(_storage_error_message(exc)) from exc
        return _with_indicator_precompute(
            request,
            result.to_dict() if isinstance(result, ImportReport) else result,
            vipdoc_path=root,
            universe=payload.universe,
            instrument_types=payload.instrument_types,
            boards=payload.boards,
            progress_callback=progress,
        )

    try:
        job_id = _runner(request).submit_callable(run_import, description="本地行情导入")
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="任务执行器暂不可用") from exc
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"data": {"job_id": job_id, "status": "queued"}},
    )


@router.get("/jobs/{job_id}")
def market_data_job_status(job_id: str, request: Request) -> dict[str, object]:
    state = _runner(request).get(job_id)
    if state is None or state.description not in {
        "本地行情导入",
        "本地行情自动导入",
        "行情同步",
        AUTO_SYNC_DESCRIPTION,
        "指标预计算",
    }:
        raise HTTPException(status_code=404, detail="行情任务不存在或已过期")
    return {"data": state.snapshot()}


@router.get("/store", response_model=None)
def market_data_store_status(request: Request) -> dict[str, object] | JSONResponse:
    try:
        data = _store(request).status().to_dict()
        startup_job_id = getattr(request.app.state, "startup_import_job_id", None)
        if startup_job_id:
            startup_state = _runner(request).get(startup_job_id)
            if startup_state is None or startup_state.status in {"completed", "failed"}:
                startup_job_id = None
        data["startup_import_job_id"] = startup_job_id
        auto_sync_job_id = getattr(request.app.state, "auto_sync_job_id", None)
        if auto_sync_job_id:
            auto_sync_state = _runner(request).get(auto_sync_job_id)
            if auto_sync_state is None or auto_sync_state.status in {"completed", "failed"}:
                auto_sync_job_id = None
        data["auto_sync_job_id"] = auto_sync_job_id
        data["auto_sync_enabled"] = bool(getattr(request.app.state, "auto_sync_enabled", False))
        data["auto_sync_interval_seconds"] = getattr(
            request.app.state,
            "auto_sync_interval_seconds",
            None,
        )
        return {"data": data}
    except (MarketDataStoreError, duckdb.Error, OSError) as exc:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": {
                    "code": "market_data_store_error",
                    "message": _storage_error_message(exc),
                }
            },
        )


@router.post("/sync-online", status_code=status.HTTP_202_ACCEPTED)
def create_online_sync_job(payload: MarketSyncRequest, request: Request) -> JSONResponse:
    return _create_sync_job(payload, request, include_local=False)


@router.post("/sync", status_code=status.HTTP_202_ACCEPTED)
def create_sync_job(payload: MarketSyncRequest, request: Request) -> JSONResponse:
    return _create_sync_job(payload, request, include_local=True)


def _create_sync_job(
    payload: MarketSyncRequest,
    request: Request,
    *,
    include_local: bool,
) -> JSONResponse:
    config = _sync_config(payload, prefer_store_targets=include_local)

    def run_sync(progress: Callable[[int, int], None]) -> dict[str, object]:
        try:
            if not include_local:
                return _result_dict(_service(request).sync(config, progress))

            local_root = os.path.abspath(
                os.path.expanduser(_resolved_vipdoc_path(payload.vipdoc_path))
            )
            if os.path.isdir(local_root):
                local_result = _result_dict(
                    _importer(request).import_vipdoc(
                        local_root,
                        universe=payload.universe,
                        instrument_types=payload.instrument_types,
                        boards=payload.boards,
                        progress_callback=progress,
                    )
                )
            else:
                local_result = {
                    "source": "local",
                    "status": "skipped",
                    "reason": "未找到 vipdoc 目录",
                    "vipdoc_path": local_root,
                }
            online_result = _result_dict(_service(request).sync(config, progress))
        except (MarketDataStoreError, duckdb.Error) as exc:
            raise TaskUserError(_storage_error_message(exc)) from exc
        result = (
            online_result if not include_local else _combined_result(local_result, online_result)
        )
        return _with_indicator_precompute(
            request,
            result,
            vipdoc_path=_resolved_vipdoc_path(payload.vipdoc_path),
            universe=payload.universe,
            instrument_types=payload.instrument_types,
            boards=payload.boards,
            progress_callback=progress,
        )

    try:
        job_id = _runner(request).submit_callable(run_sync, description="行情同步")
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="任务执行器暂不可用") from exc
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"data": {"job_id": job_id, "status": "queued"}},
    )


@router.get("/sync/jobs/{job_id}")
def sync_job_status(job_id: str, request: Request) -> dict[str, object]:
    state = _runner(request).get(job_id)
    if state is None or state.description != "行情同步":
        raise HTTPException(status_code=404, detail="行情同步任务不存在或已过期")
    return {"data": state.snapshot()}


@router.get("/local/instruments", response_model=None)
def local_instruments(
    request: Request,
    market: str = Query(default="all", pattern=r"^(all|SH|SZ)$"),
    keyword: str = Query(default="", max_length=64),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> dict[str, object] | JSONResponse:
    try:
        result = _local_service(request).list_instruments(
            _resolved_vipdoc_path(None),
            market=market,
            keyword=keyword,
            page=page,
            page_size=page_size,
        )
    except (OSError, ValueError, MarketDataStoreError, duckdb.Error) as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": {"code": "local_data_error", "message": str(exc)}},
        )
    payload = result.to_dict()
    return {
        "data": payload["items"],
        "meta": {
            "total": payload["total"],
            "page": payload["page"],
            "page_size": payload["page_size"],
            "pages": payload["pages"],
        },
    }


@router.get("/local/{market}/{code}/bars", response_model=None)
def local_market_bars(
    request: Request,
    market: Literal["SH", "SZ"],
    code: str = PathParameter(pattern=r"^\d{6}$"),
    period: ChartPeriod = Query(default="daily"),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
) -> dict[str, object] | JSONResponse:
    try:
        result = _local_service(request).get_chart(
            _resolved_vipdoc_path(None),
            market,
            code,
            period=period,
            start_date=start_date,
            end_date=end_date,
        )
    except (OSError, ValueError, MarketDataStoreError, duckdb.Error) as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": {"code": "local_data_error", "message": str(exc)}},
        )
    return {"data": result.to_dict()}


def _storage_error_message(exc: BaseException) -> str:
    if isinstance(exc, MarketDataStoreError):
        return str(exc)
    message = str(exc).lower()
    if "lock" in message or "conflicting" in message:
        return "行情数据库当前被其它进程占用，请关闭其它写入进程后重试"
    return "行情数据库无法读取，请检查文件权限或数据库完整性"
