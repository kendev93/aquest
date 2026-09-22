"""Online market-data synchronization endpoints.

同步、回填、兼容别名 ``/sync`` 共用 ``_create_sync_job``：这条路径只走在线
通道和本地行情库，本地 vipdoc 导入在 ``local_import`` 里另有一套。
"""

from __future__ import annotations

from inspect import Parameter, signature

import duckdb
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse

from selector_app.adapters.market_sync import (
    AbortCheck,
    DetailCallback,
    InstrumentDiscoveryReport,
    MarketDataCancelledError,
    MarketSyncConfig,
    MarketSyncReport,
)
from selector_app.adapters.online_market_sync import OnlineProviderError
from selector_app.market_data.auto_sync import MARKET_SYNC_EXCLUSIVE_GROUP
from selector_app.market_data.store import MarketDataStoreError
from selector_app.screening.jobs import (
    ActiveJobConflictError,
    ProgressReporter,
    TaskCancelledError,
    TaskUserError,
    report_progress_detail,
)

from ...schemas import MarketSyncRequest
from ._shared import (
    MarketSyncService,
    _reject_duplicate_market_job,
    _runner,
    _service,
    _storage_error_message,
)
from .precompute import _with_indicator_precompute

router = APIRouter(tags=["market-data"])


@router.post("/sync-online", status_code=status.HTTP_202_ACCEPTED)
def create_online_sync_job(payload: MarketSyncRequest, request: Request) -> JSONResponse:
    return _create_sync_job(payload, request)


@router.post("/backfill", status_code=status.HTTP_202_ACCEPTED)
def create_eastmoney_backfill_job(payload: MarketSyncRequest, request: Request) -> JSONResponse:
    if payload.provider != "eastmoney":
        raise HTTPException(status_code=422, detail="全量回填只支持在线日线通道")
    # 回填只覆盖股票；没显式给类型时回落到股票，保持旧调用的行为不变。
    full_payload = payload.model_copy(
        update={
            "provider": "eastmoney",
            "instrument_types": payload.instrument_types or ["stock"],
            "full_history": True,
        }
    )
    return _create_sync_job(
        full_payload,
        request,
        description="历史全量回填",
    )


@router.post("/sync", status_code=status.HTTP_202_ACCEPTED)
def create_sync_job(payload: MarketSyncRequest, request: Request) -> JSONResponse:
    return _create_sync_job(payload, request)


@router.get("/sync/jobs/{job_id}")
def sync_job_status(job_id: str, request: Request) -> dict[str, object]:
    state = _runner(request).get(job_id)
    if state is None or state.description not in {
        "行情同步",
        "全市场行情同步",
        "历史全量回填",
    }:
        raise HTTPException(status_code=404, detail="行情同步任务不存在或已过期")
    return {"data": state.snapshot()}


def _create_sync_job(
    payload: MarketSyncRequest,
    request: Request,
    *,
    description: str = "行情同步",
) -> JSONResponse:
    """Create an online-only sync job.

    This route, its compatibility alias ``/sync``, backfill, and the automatic
    scheduler all operate on online providers and the local database.
    """

    runner = _runner(request)
    _reject_duplicate_market_job(runner)
    job_id_holder = [""]

    def abort_check() -> bool:
        return bool(job_id_holder[0]) and runner.is_cancel_requested(job_id_holder[0])

    config = _sync_config(payload, prefer_store_targets=False)

    def run_sync(progress: ProgressReporter) -> dict[str, object]:
        try:
            if abort_check():
                raise MarketDataCancelledError("行情同步已取消")
            result = _result_dict(
                _call_market_sync(
                    _service(request),
                    config,
                    progress,
                    detail_callback=lambda detail: report_progress_detail(progress, detail),
                    abort_check=abort_check,
                )
            )
            if abort_check():
                raise MarketDataCancelledError("行情同步已取消")
        except MarketDataCancelledError as exc:
            raise TaskCancelledError(str(exc)) from exc
        except OnlineProviderError as exc:
            raise TaskUserError(str(exc)) from exc
        except (MarketDataStoreError, duckdb.Error) as exc:
            raise TaskUserError(_storage_error_message(exc)) from exc
        if abort_check():
            raise TaskCancelledError("行情同步已取消")
        # 同步只影响一部分标的，缓存也只补算那些缺失或过期的。
        return _with_indicator_precompute(
            request,
            result,
            vipdoc_path=None,
            universe=payload.universe,
            instrument_types=payload.instrument_types,
            boards=payload.boards,
            progress_callback=progress,
            only_changed=True,
            years=payload.precompute_years,
        )

    try:
        job_id = runner.submit_callable(
            run_sync,
            description=description,
            exclusive_group=MARKET_SYNC_EXCLUSIVE_GROUP,
        )
        job_id_holder[0] = job_id
    except ActiveJobConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="任务执行器暂不可用") from exc
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"data": {"job_id": job_id, "status": "queued"}},
    )


def _sync_config(payload: MarketSyncRequest, *, prefer_store_targets: bool) -> MarketSyncConfig:
    return MarketSyncConfig(
        universe=payload.universe,
        provider=payload.provider,
        bars=payload.bars,
        instrument_types=tuple(payload.instrument_types or ()),
        boards=tuple(payload.boards or ()),
        prefer_store_targets=prefer_store_targets,
        refresh_names=prefer_store_targets,
        full_history=payload.full_history,
    )


def _result_dict(
    result: InstrumentDiscoveryReport | MarketSyncReport | dict[str, object],
) -> dict[str, object]:
    return (
        result.to_dict()
        if isinstance(result, (InstrumentDiscoveryReport, MarketSyncReport))
        else result
    )


def _call_market_sync(
    service: MarketSyncService,
    config: MarketSyncConfig,
    progress: ProgressReporter,
    *,
    detail_callback: DetailCallback,
    abort_check: AbortCheck,
) -> MarketSyncReport | dict[str, object]:
    if _supports_abort_check(service.sync):
        return service.sync(
            config,
            progress,
            detail_callback=detail_callback,
            abort_check=abort_check,
        )
    return service.sync(config, progress, detail_callback=detail_callback)


def _supports_abort_check(method: object) -> bool:
    if not callable(method):
        return False
    try:
        parameters = signature(method).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "abort_check"
        and parameter.kind in {Parameter.POSITIONAL_OR_KEYWORD, Parameter.KEYWORD_ONLY}
        for parameter in parameters
    )
