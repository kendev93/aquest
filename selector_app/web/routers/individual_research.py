"""Single-security swing, condition, and risk-optimization jobs."""

from __future__ import annotations

import uuid
from typing import Literal, cast

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse

from selector_app.individual_research.service import IndividualStockResearchService
from selector_app.market_data.store import DuckDbMarketDataStore, MarketDataStoreError
from selector_app.screening.jobs import (
    ActiveJobConflictError,
    JobState,
    ProgressReporter,
    ScreenJobRunner,
    TaskCancelledError,
    TaskUserError,
)

from ..backtest_history import save_completed_backtest_history, utc_now
from ..date_ranges import resolve_date_range
from ..market_source import resolve_market_status
from ..schemas.individual_research import IndividualResearchRequest

router = APIRouter(prefix="/individual-research", tags=["individual-research"])
JOB_DESCRIPTION = "个股研究"
HISTORY_TYPE: Literal["individual_research"] = "individual_research"


def _runner(request: Request) -> ScreenJobRunner:
    return cast(ScreenJobRunner, request.app.state.screen_job_runner)


def _store(request: Request) -> DuckDbMarketDataStore:
    return cast(DuckDbMarketDataStore, request.app.state.market_data_store)


def _service(request: Request) -> IndividualStockResearchService:
    return cast(IndividualStockResearchService, request.app.state.individual_research_service)


def _state_or_404(job_id: str, request: Request) -> JobState:
    state = _runner(request).get(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail="个股研究任务不存在或已过期")
    return state


@router.post("", status_code=status.HTTP_202_ACCEPTED)
def create_individual_research(
    payload: IndividualResearchRequest,
    request: Request,
) -> JSONResponse:
    try:
        store_status = resolve_market_status(request)
        start_date, end_date = resolve_date_range(
            mode=payload.date_mode,
            requested_start=payload.start_date,
            requested_end=payload.end_date,
            available_start=store_status.data_start,
            available_end=store_status.data_end,
        )
        if start_date is None or end_date is None:
            raise ValueError("本地行情没有可用于研究的日期")
        ref = request.app.state.market_data_adapter.stock_ref("", payload.market, payload.code)
        if getattr(ref, "instrument_type", "stock") != "stock":
            raise ValueError("个股研究只支持 A 股股票")
        resolved_payload = payload.model_copy(
            update={"date_mode": "custom", "start_date": start_date, "end_date": end_date}
        )
        config = resolved_payload.to_config(
            start_date=start_date,
            end_date=end_date,
            name=ref.name or "",
        )
        request_snapshot = cast(dict[str, object], resolved_payload.model_dump(mode="json"))
        job_id = uuid.uuid4().hex

        def run_task(progress: ProgressReporter) -> dict[str, object]:
            started_at = utc_now()
            try:
                result = _service(request).run(
                    config,
                    progress,
                    lambda: _runner(request).is_cancel_requested(job_id),
                )
            except TaskCancelledError:
                raise
            except ValueError as exc:
                raise TaskUserError(str(exc)) from exc
            result["history_id"] = job_id
            finished_at = utc_now()
            save_completed_backtest_history(
                _store(request),
                history_id=job_id,
                backtest_type=HISTORY_TYPE,
                request=request_snapshot,
                result=result,
                started_at=started_at,
                finished_at=finished_at,
            )
            return result

        job_id = _runner(request).submit_callable(
            run_task,
            description=JOB_DESCRIPTION,
            job_id=job_id,
            worker_count=1,
            exclusive_group="individual-research",
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"error": {"code": "validation_error", "message": str(exc)}},
        )
    except ActiveJobConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="个股研究任务执行器暂不可用") from exc
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"data": {"job_id": job_id, "status": "queued"}},
    )


@router.get("/history")
def individual_research_history(request: Request) -> dict[str, object]:
    try:
        return {"data": _store(request).list_backtest_history(HISTORY_TYPE)}
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="个股研究历史暂不可用") from exc


@router.get("/history/{history_id}")
def individual_research_history_detail(history_id: str, request: Request) -> dict[str, object]:
    try:
        record = _store(request).read_backtest_history(history_id)
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="个股研究历史暂不可用") from exc
    if record is None or record.get("backtest_type") != HISTORY_TYPE:
        raise HTTPException(status_code=404, detail="个股研究历史不存在")
    return {"data": record}


@router.delete("/history/{history_id}")
def delete_individual_research_history(history_id: str, request: Request) -> dict[str, object]:
    try:
        record = _store(request).read_backtest_history(history_id)
        if record is None or record.get("backtest_type") != HISTORY_TYPE:
            return {"data": {"deleted": False}}
        return {"data": {"deleted": _store(request).delete_backtest_history(history_id)}}
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="个股研究历史暂不可用") from exc


@router.get("/jobs/{job_id}")
def individual_research_job(job_id: str, request: Request) -> dict[str, object]:
    return {"data": _state_or_404(job_id, request).snapshot()}


@router.post("/jobs/{job_id}/cancel")
def cancel_individual_research(job_id: str, request: Request) -> dict[str, object]:
    _state_or_404(job_id, request)
    return {"data": {"cancelled": _runner(request).request_cancel(job_id)}}


__all__ = ["router"]
