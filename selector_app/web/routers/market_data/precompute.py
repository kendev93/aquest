"""Indicator precompute job, plus the warm-up step the write paths share.

同步 / 本地导入写完行情之后都走 ``_with_indicator_precompute``，把派生指标补齐；
独立入口则允许手动触发一次全量预热。
"""

from __future__ import annotations

from collections.abc import Callable
from inspect import Parameter, signature

import duckdb
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse

from selector_app.indicators.precompute import (
    DEFAULT_PRECOMPUTE_YEARS,
    PRECOMPUTE_MAX_BARS,
    PrecomputeCancelledError,
    should_defer_precompute,
)
from selector_app.market_data.models import InstrumentBoard, InstrumentType
from selector_app.market_data.store import MarketDataStoreError
from selector_app.screening.jobs import TaskCancelledError, TaskUserError

from ...schemas import MarketSyncRequest
from ._shared import (
    _invalidate_indicator_cache,
    _market_status,
    _precomputer,
    _runner,
    _storage_error_message,
    _stored_end_year,
)

router = APIRouter(tags=["market-data"])


def _supports_abort_check(method: Callable[..., object]) -> bool:
    return any(
        parameter.name == "abort_check" or parameter.kind is Parameter.VAR_KEYWORD
        for parameter in signature(method).parameters.values()
    )


def _with_indicator_precompute(
    request: Request,
    result: dict[str, object],
    *,
    vipdoc_path: str | None,
    universe: str,
    instrument_types: list[InstrumentType] | tuple[InstrumentType, ...] | None,
    boards: list[InstrumentBoard] | tuple[InstrumentBoard, ...] | None,
    progress_callback: Callable[[int, int], None] | None = None,
    abort_check: Callable[[], bool] | None = None,
    only_changed: bool = False,
    years: int | None = None,
) -> dict[str, object]:
    if abort_check is not None and abort_check():
        raise TaskCancelledError("行情任务已取消")
    # 调用方刚写完行情：先把进程内的指标缓存和版本号丢掉，否则下面的预计算
    # 会带着旧版本号入库，把新算出来的结果又存成旧键。
    _invalidate_indicator_cache(request)
    try:
        store_status = _market_status(request)
        if should_defer_precompute(store_status.bar_count):
            return {
                **result,
                "indicator_precompute": {
                    "status": "deferred",
                    "reason": "large_dataset",
                    "bar_count": store_status.bar_count,
                    "max_bars": PRECOMPUTE_MAX_BARS,
                    "message": (
                        f"行情数据量为 {store_status.bar_count:,} 根，超过指标预计算上限 "
                        f"{PRECOMPUTE_MAX_BARS:,} 根，已改为按需计算"
                    ),
                },
            }
        precomputer = _precomputer(request)
        if _supports_abort_check(precomputer.run):
            precomputed = precomputer.run(
                vipdoc_path=vipdoc_path or ".",
                universe=universe,
                instrument_types=tuple(instrument_types or ()),
                boards=tuple(boards or ()),
                years=years if years is not None else DEFAULT_PRECOMPUTE_YEARS,
                only_changed=only_changed,
                end_year=_stored_end_year(store_status),
                progress_callback=progress_callback,
                abort_check=abort_check,
            )
        else:
            precomputed = precomputer.run(
                vipdoc_path=vipdoc_path or ".",
                universe=universe,
                instrument_types=tuple(instrument_types or ()),
                boards=tuple(boards or ()),
                years=years if years is not None else DEFAULT_PRECOMPUTE_YEARS,
                only_changed=only_changed,
                end_year=_stored_end_year(store_status),
                progress_callback=progress_callback,
            )
        if abort_check is not None and abort_check():
            raise TaskCancelledError("行情任务已取消")
        return {**result, "indicator_precompute": precomputed.to_dict()}
    except PrecomputeCancelledError as exc:
        raise TaskCancelledError(str(exc)) from exc
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


@router.post(
    "/indicator-cache/precompute",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=None,
)
def create_indicator_precompute_job(
    payload: MarketSyncRequest,
    request: Request,
) -> JSONResponse:
    job_id_holder = [""]

    def abort_check() -> bool:
        return bool(job_id_holder[0]) and _runner(request).is_cancel_requested(job_id_holder[0])

    def run_precompute(progress: Callable[[int, int], None]) -> dict[str, object]:
        if abort_check():
            raise TaskCancelledError("指标预计算已取消")
        try:
            store_status = _market_status(request)
            if should_defer_precompute(store_status.bar_count):
                return {
                    "status": "deferred",
                    "reason": "large_dataset",
                    "bar_count": store_status.bar_count,
                    "max_bars": PRECOMPUTE_MAX_BARS,
                    "message": (
                        f"行情数据量为 {store_status.bar_count:,} 根，超过指标预计算上限 "
                        f"{PRECOMPUTE_MAX_BARS:,} 根，已改为按需计算"
                    ),
                }
            precomputer = _precomputer(request)
            if _supports_abort_check(precomputer.run):
                report = precomputer.run(
                    vipdoc_path=payload.vipdoc_path or ".",
                    universe=payload.universe,
                    instrument_types=tuple(payload.instrument_types or ()),
                    boards=tuple(payload.boards or ()),
                    years=payload.precompute_years or DEFAULT_PRECOMPUTE_YEARS,
                    end_year=_stored_end_year(store_status),
                    only_changed=payload.precompute_only_changed,
                    progress_callback=progress,
                    abort_check=abort_check,
                )
            else:
                report = precomputer.run(
                    vipdoc_path=payload.vipdoc_path or ".",
                    universe=payload.universe,
                    instrument_types=tuple(payload.instrument_types or ()),
                    boards=tuple(payload.boards or ()),
                    years=payload.precompute_years or DEFAULT_PRECOMPUTE_YEARS,
                    end_year=_stored_end_year(store_status),
                    only_changed=payload.precompute_only_changed,
                    progress_callback=progress,
                )
        except PrecomputeCancelledError as exc:
            raise TaskCancelledError(str(exc)) from exc
        except (OSError, ValueError, MarketDataStoreError, duckdb.Error) as exc:
            raise TaskUserError(_storage_error_message(exc)) from exc
        return report.to_dict()

    try:
        job_id = _runner(request).submit_callable(run_precompute, description="指标预计算")
        job_id_holder[0] = job_id
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="任务执行器暂不可用") from exc
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"data": {"job_id": job_id, "status": "queued"}},
    )
