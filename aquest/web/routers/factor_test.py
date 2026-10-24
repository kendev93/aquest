"""Cross-sectional factor test endpoints.

A full-market run reads every stock twice and ranks thousands of cross-sections
per value, which is tens of seconds to minutes of work - so it goes through the
job runner like every other long study rather than blocking a request.

The result is archived on its own (``factor_test`` history type) so the form
can be restored later: without the horizon and the value selection, a saved
table of ICs says nothing about what was actually measured.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Protocol, cast

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse

from aquest.concurrency import HEAVY_RESEARCH_EXCLUSIVE_GROUP
from aquest.factor_test.models import FactorTestConfig
from aquest.market_data.store import DuckDbMarketDataStore, MarketDataStoreError
from aquest.screening.jobs import (
    ActiveJobConflictError,
    JobState,
    ScreenJobRunner,
    TaskUserError,
)
from aquest.web.app_errors import TaskFailedError

from ..backtest_history import save_completed_backtest_history, utc_now
from ..date_ranges import resolve_date_range
from ..market_source import resolve_market_status
from ..schemas import FactorTestRequest

router = APIRouter(prefix="/factor-test", tags=["factor-test"])


class FactorTestServiceLike(Protocol):
    def run(
        self,
        config: FactorTestConfig,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> dict[str, object]: ...


def _runner(request: Request) -> ScreenJobRunner:
    return cast(ScreenJobRunner, request.app.state.screen_job_runner)


def _service(request: Request) -> FactorTestServiceLike:
    return cast(FactorTestServiceLike, request.app.state.factor_test_service)


def _store(request: Request) -> DuckDbMarketDataStore:
    return cast(DuckDbMarketDataStore, request.app.state.market_data_store)


def _state_or_404(job_id: str, request: Request) -> JobState:
    state = _runner(request).get(job_id)
    if state is None or state.description != "因子检验":
        raise HTTPException(status_code=404, detail="因子检验任务不存在或已过期")
    return state


@router.post("", status_code=status.HTTP_202_ACCEPTED)
def create_factor_test(payload: FactorTestRequest, request: Request) -> JSONResponse:
    try:
        job_id = uuid.uuid4().hex
        resolved = _resolve_payload_dates(payload, request)
        config = resolved.to_config()
        request_snapshot = cast(dict[str, object], resolved.model_dump(mode="json"))
        service = _service(request)

        def run_test(progress: Callable[[int, int], None]) -> dict[str, object]:
            started_at = utc_now()
            try:
                result = service.run(config, progress)
            except ValueError as exc:
                raise TaskUserError(str(exc)) from exc
            finished_at = utc_now()
            save_completed_backtest_history(
                _store(request),
                history_id=job_id,
                backtest_type="factor_test",
                request=request_snapshot,
                result=result,
                started_at=started_at,
                finished_at=finished_at,
            )
            return result

        job_id = _runner(request).submit_callable(
            run_test,
            description="因子检验",
            job_id=job_id,
            exclusive_group=HEAVY_RESEARCH_EXCLUSIVE_GROUP,
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"error": {"code": "validation_error", "message": str(exc)}},
        )
    except ActiveJobConflictError as exc:
        # 它是 RuntimeError 的子类，落到下面的兜底会变成 503「执行器暂不可用」；
        # 重活排队冲突要回 409，和其他路由保持一致。
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="任务执行器暂不可用") from exc
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"data": {"job_id": job_id, "status": "queued"}},
    )


def _resolve_payload_dates(payload: FactorTestRequest, request: Request) -> FactorTestRequest:
    """Fill in a preset range so the stored request is self-describing."""

    if payload.start_date is not None and payload.end_date is not None:
        return payload
    status_snapshot = resolve_market_status(request)
    start_date, end_date = resolve_date_range(
        mode="recent5",
        requested_start=payload.start_date,
        requested_end=payload.end_date,
        available_start=status_snapshot.data_start,
        available_end=status_snapshot.data_end,
    )
    return payload.model_copy(update={"start_date": start_date, "end_date": end_date})


@router.get("/history")
def factor_test_history(request: Request) -> dict[str, object]:
    try:
        return {"data": _store(request).list_backtest_history("factor_test")}
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="因子检验历史暂不可用") from exc


@router.get("/history/{history_id}")
def factor_test_history_detail(history_id: str, request: Request) -> dict[str, object]:
    try:
        record = _store(request).read_backtest_history(history_id)
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="因子检验历史暂不可用") from exc
    if record is None or record.get("backtest_type") != "factor_test":
        raise HTTPException(status_code=404, detail="因子检验历史不存在")
    return {"data": record}


@router.delete("/history/{history_id}")
def delete_factor_test_history(history_id: str, request: Request) -> dict[str, object]:
    try:
        record = _store(request).read_backtest_history(history_id)
        if record is None or record.get("backtest_type") != "factor_test":
            return {"data": {"deleted": False}}
        deleted = _store(request).delete_backtest_history(history_id)
    except (MarketDataStoreError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="因子检验历史暂不可用") from exc
    return {"data": {"deleted": deleted}}


@router.get("/{job_id}")
def factor_test_status(job_id: str, request: Request) -> dict[str, object]:
    state = _state_or_404(job_id, request)
    snapshot = state.snapshot()
    snapshot["result"] = None
    return {"data": snapshot}


@router.get("/{job_id}/results")
def factor_test_results(job_id: str, request: Request) -> dict[str, object]:
    state = _state_or_404(job_id, request)
    if state.status == "failed":
        raise TaskFailedError(state.error or "因子检验任务失败")
    if state.status != "completed" or state.result_payload is None:
        raise HTTPException(status_code=409, detail="因子检验尚未完成")
    return {"data": state.result_payload}
