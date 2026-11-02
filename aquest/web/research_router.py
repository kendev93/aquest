"""Shared route skeleton for the saved research studies.

Factor test, factor attribution and the other research pages all expose the
same six shapes: submit, history list/detail/delete, job status and results.
Only the request model, job label, history type and date preparation differ.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any, Protocol, cast

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse

from aquest.concurrency import HEAVY_RESEARCH_EXCLUSIVE_GROUP
from aquest.market_data.models import BacktestHistoryType
from aquest.screening.jobs import ActiveJobConflictError, JobState, TaskUserError
from aquest.web.app_errors import TaskFailedError

from .backtest_history import save_completed_backtest_history, utc_now
from .routers._deps import job_runner as _runner
from .routers._deps import market_store as _store
from .routers._errors import store_guard


class ResearchService(Protocol):
    def run(
        self,
        config: Any,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> dict[str, object]: ...


def build_research_router(
    *,
    prefix: str,
    tag: str,
    job_description: str,
    label: str,
    history_type: BacktestHistoryType,
    service_attr: str,
    prepare_payload: Callable[[Any, Request], Any] | None = None,
    source_types: tuple[str, ...] | None = None,
) -> tuple[APIRouter, Callable[[Request, Any], JSONResponse]]:
    router = APIRouter(prefix=prefix, tags=[tag])

    def service(request: Request) -> ResearchService:
        return cast(ResearchService, getattr(request.app.state, service_attr))

    def state_or_404(job_id: str, request: Request) -> JobState:
        state = _runner(request).get(job_id)
        if state is None or state.description != job_description:
            raise HTTPException(status_code=404, detail=f"{label}任务不存在或已过期")
        return state

    def submit(request: Request, payload: Any) -> JSONResponse:
        try:
            job_id = uuid.uuid4().hex
            resolved = prepare_payload(payload, request) if prepare_payload else payload
            config = resolved.to_config()
            request_snapshot = cast(dict[str, object], resolved.model_dump(mode="json"))
            job_service = service(request)

            def run(progress: Callable[[int, int], None]) -> dict[str, object]:
                started_at = utc_now()
                try:
                    result = job_service.run(config, progress)
                except ValueError as exc:
                    raise TaskUserError(str(exc)) from exc
                finished_at = utc_now()
                save_completed_backtest_history(
                    _store(request),
                    history_id=job_id,
                    backtest_type=history_type,
                    request=request_snapshot,
                    result=result,
                    started_at=started_at,
                    finished_at=finished_at,
                )
                return result

            job_id = _runner(request).submit_callable(
                run,
                description=job_description,
                job_id=job_id,
                exclusive_group=HEAVY_RESEARCH_EXCLUSIVE_GROUP,
            )
        except ValueError as exc:
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                content={"error": {"code": "validation_error", "message": str(exc)}},
            )
        except ActiveJobConflictError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail="任务执行器暂不可用") from exc
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={"data": {"job_id": job_id, "status": "queued"}},
        )

    if source_types is not None:

        @router.get("/sources", name=f"{tag}_sources")
        def sources(request: Request) -> dict[str, object]:
            with store_guard("回测历史暂不可用"):
                records = _store(request).list_backtest_history_all()
            return {
                "data": [
                    record for record in records if record.get("backtest_type") in source_types
                ]
            }

    @router.get("/history", name=f"{tag}_history")
    def history(request: Request) -> dict[str, object]:
        with store_guard(f"{label}历史暂不可用"):
            return {"data": _store(request).list_backtest_history(history_type)}

    @router.get("/history/{history_id}", name=f"{tag}_history_detail")
    def history_detail(history_id: str, request: Request) -> dict[str, object]:
        with store_guard(f"{label}历史暂不可用"):
            record = _store(request).read_backtest_history(history_id)
        if record is None or record.get("backtest_type") != history_type:
            raise HTTPException(status_code=404, detail=f"{label}历史不存在")
        return {"data": record}

    @router.delete("/history/{history_id}", name=f"{tag}_history_delete")
    def delete_history(history_id: str, request: Request) -> dict[str, object]:
        with store_guard(f"{label}历史暂不可用", include_runtime=True):
            record = _store(request).read_backtest_history(history_id)
            if record is None or record.get("backtest_type") != history_type:
                return {"data": {"deleted": False}}
            deleted = _store(request).delete_backtest_history(history_id)
        return {"data": {"deleted": deleted}}

    @router.get("/{job_id}", name=f"{tag}_status")
    def job_status(job_id: str, request: Request) -> dict[str, object]:
        state = state_or_404(job_id, request)
        snapshot = state.snapshot()
        snapshot["result"] = None
        return {"data": snapshot}

    @router.get("/{job_id}/results", name=f"{tag}_results")
    def job_results(job_id: str, request: Request) -> dict[str, object]:
        state = state_or_404(job_id, request)
        if state.status == "failed":
            raise TaskFailedError(state.error or f"{label}任务失败")
        if state.status != "completed" or state.result_payload is None:
            raise HTTPException(status_code=409, detail=f"{label}尚未完成")
        return {"data": state.result_payload}

    return router, submit
