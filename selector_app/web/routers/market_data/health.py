"""Health check and repair endpoints for the stored daily bars.

两个端点都提交后台任务：600 万行的体检实测约 10 秒，同步返回会把请求线程按死，
而且体检/修复和行情同步共用同一个库，必须走同一道「同时只允许一个行情任务」的闸门。
任务描述登记在 ``auto_sync.MARKET_SYNC_JOB_DESCRIPTIONS``，所以能用现成的
``/market-data/jobs/{job_id}`` 轮询与取消。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from time import perf_counter

import duckdb
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse

from selector_app.adapters.market_sync import MarketDataCancelledError, MarketSyncConfig
from selector_app.adapters.online_market_sync import OnlineProviderError
from selector_app.market_data.auto_sync import (
    HEALTH_CHECK_DESCRIPTION,
    HEALTH_REPAIR_DESCRIPTION,
    MARKET_SYNC_EXCLUSIVE_GROUP,
)
from selector_app.market_data.health import (
    CHECK_REPAIRABLE,
    DEFAULT_EXAMPLE_LIMIT,
    normalize_keys,
)
from selector_app.market_data.store import MarketDataStoreError
from selector_app.screening.jobs import (
    ActiveJobConflictError,
    ProgressReporter,
    TaskCancelledError,
    TaskUserError,
    report_progress_detail,
)

from ...schemas import HealthRepairRequest
from ._shared import (
    _invalidate_indicator_cache,
    _reject_duplicate_market_job,
    _runner,
    _service,
    _storage_error_message,
    _store,
)
from .sync import _call_market_sync

router = APIRouter(tags=["market-data"])

_HealthTask = Callable[[ProgressReporter], dict[str, object]]


@router.post("/health/check", status_code=status.HTTP_202_ACCEPTED)
def create_health_check_job(request: Request) -> JSONResponse:
    """Scan the stored bars and report gaps, bad prices and stale instruments."""

    _reject_duplicate_market_job(_runner(request))

    def run_check(progress: ProgressReporter) -> dict[str, object]:
        progress(0, 1)
        try:
            started = perf_counter()
            report = _store(request).health_report(
                generated_at=datetime.now(timezone.utc).isoformat(),
                duration_seconds=0.0,
                example_limit=DEFAULT_EXAMPLE_LIMIT,
            )
        except (MarketDataStoreError, duckdb.Error, OSError, RuntimeError) as exc:
            raise TaskUserError(_storage_error_message(exc)) from exc
        report = replace(report, duration_seconds=perf_counter() - started)
        progress(1, 1)
        return report.to_dict()

    return _submit(request, run_check, description=HEALTH_CHECK_DESCRIPTION)


@router.post("/health/repair", status_code=status.HTTP_202_ACCEPTED)
def create_health_repair_job(
    payload: HealthRepairRequest,
    request: Request,
) -> JSONResponse:
    """Delete every bar of the flagged instruments and re-download them.

    顺序不能反：在线行写不进已有本地行的位置（本地行永远压过在线行），而且
    ``full_history`` 只补已有区间**之外**的缺口——不先删就覆盖不到中间那段。
    代价是删掉后在线拉不到的标的会变空，所以结果里会单独列出这批。
    """

    runner = _runner(request)
    _reject_duplicate_market_job(runner)
    keys = tuple(key for key in normalize_keys(payload.checks) if CHECK_REPAIRABLE[key])
    explicit_codes = tuple(dict.fromkeys(str(code).strip() for code in payload.codes or ()))
    limit = payload.limit
    job_id_holder = [""]

    def abort_check() -> bool:
        return bool(job_id_holder[0]) and runner.is_cancel_requested(job_id_holder[0])

    def run_repair(progress: ProgressReporter) -> dict[str, object]:
        store = _store(request)
        try:
            pairs = (
                tuple(("", code) for code in explicit_codes)
                if explicit_codes
                else store.health.codes_for(keys)
            )
        except (MarketDataStoreError, duckdb.Error, OSError, RuntimeError) as exc:
            raise TaskUserError(_storage_error_message(exc)) from exc
        truncated = len(pairs) > limit
        pairs = pairs[:limit]
        if not pairs:
            return {
                "checked_keys": list(keys),
                "requested_codes": 0,
                "truncated": False,
                "deleted_bars": 0,
                "written_bars": 0,
                "errors": 0,
                "failure_reasons": {},
                "empty_after_repair": [],
                "message": "没有需要修复的标的。",
            }
        codes = tuple(code for _market, code in pairs)
        progress(0, len(codes))
        try:
            deleted = store.delete_bars(codes=list(codes))
        except (MarketDataStoreError, duckdb.Error, OSError, RuntimeError) as exc:
            raise TaskUserError(_storage_error_message(exc)) from exc
        # 删掉的行可能正躺在指标缓存里，版本号副本不清掉下一次查询还会命中它。
        _invalidate_indicator_cache(request)
        if abort_check():
            raise TaskCancelledError("行情修复已取消，已删除的历史请重新同步补回。")
        config = MarketSyncConfig(
            universe="all",
            provider="eastmoney",
            full_history=True,
            codes=codes,
            # 从库里已有的标的取目标，而不是拉一遍在线证券列表：修复只关心库里的票，
            # 退市票在线列表里往往已经没有，但它们的数据同样要修。
            prefer_store_targets=True,
            refresh_names=False,
        )
        try:
            if abort_check():
                raise MarketDataCancelledError("行情修复已取消")
            result = _call_market_sync(
                _service(request),
                config,
                progress,
                detail_callback=lambda detail: report_progress_detail(progress, detail),
                abort_check=abort_check,
            )
            if abort_check():
                raise MarketDataCancelledError("行情修复已取消")
        except MarketDataCancelledError as exc:
            raise TaskCancelledError(str(exc)) from exc
        except OnlineProviderError as exc:
            raise TaskUserError(str(exc)) from exc
        except (MarketDataStoreError, duckdb.Error) as exc:
            raise TaskUserError(_storage_error_message(exc)) from exc
        _invalidate_indicator_cache(request)
        empty = _empty_after_repair(codes)
        return {
            "checked_keys": list(keys),
            "requested_codes": len(codes),
            "truncated": truncated,
            "deleted_bars": deleted,
            "written_bars": _sync_value(result, "written_bars"),
            "errors": _sync_value(result, "errors"),
            "failure_reasons": _failure_reasons(result),
            "empty_after_repair": empty,
        }

    def _empty_after_repair(codes: tuple[str, ...]) -> list[str]:
        try:
            return list(_store(request).health.codes_without_bars(codes))
        except (MarketDataStoreError, duckdb.Error, OSError, RuntimeError):
            return []

    return _submit(
        request,
        run_repair,
        description=HEALTH_REPAIR_DESCRIPTION,
        holder=job_id_holder,
    )


def _sync_value(result: object, key: str) -> object:
    """Read one field from either a report object or the plain dict fallback."""

    if isinstance(result, dict):
        return result.get(key)
    return getattr(result, key, None)


def _failure_reasons(result: object) -> dict[str, int]:
    value = _sync_value(result, "failure_reasons")
    if isinstance(value, dict):
        return {str(name): int(count) for name, count in value.items()}
    return {}


def _submit(
    request: Request,
    task: _HealthTask,
    *,
    description: str,
    holder: list[str] | None = None,
) -> JSONResponse:
    runner = _runner(request)
    try:
        job_id = runner.submit_callable(
            task,
            description=description,
            exclusive_group=MARKET_SYNC_EXCLUSIVE_GROUP,
        )
    except ActiveJobConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="任务执行器暂不可用") from exc
    if holder is not None:
        holder[0] = job_id
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"data": {"job_id": job_id, "status": "queued"}},
    )


__all__ = ["create_health_check_job", "create_health_repair_job", "router"]
