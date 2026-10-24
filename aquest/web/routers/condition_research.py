"""Independent per-stock buy/sell condition research endpoints."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Protocol, cast

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse

from aquest.concurrency import HEAVY_RESEARCH_EXCLUSIVE_GROUP
from aquest.condition_research.models import ConditionResearchConfig
from aquest.condition_research.resources import (
    ResearchResourceBudgetError,
    ResearchResourceEstimate,
)
from aquest.condition_research.service import ConditionResearchService
from aquest.market_data.store import DuckDbMarketDataStore, MarketDataStoreError
from aquest.screening.jobs import (
    ActiveJobConflictError,
    JobState,
    ProgressReporter,
    ScreenJobRunner,
    TaskUserError,
)
from aquest.web.app_errors import TaskFailedError

from ..backtest_history import save_completed_backtest_history, utc_now
from ..date_ranges import resolve_date_range
from ..market_source import resolve_market_status
from ..schemas import ConditionResearchRequest


def _estimate_payload(estimate: ResearchResourceEstimate) -> dict[str, object]:
    return {
        "stock_count": estimate.stock_count,
        "candidates_per_stock": estimate.candidates_per_stock,
        "bars_per_stock": estimate.bars_per_stock,
        "selected_workers": estimate.selected_workers,
        "worker_bytes": estimate.worker_bytes,
        "parent_bytes": estimate.parent_bytes,
        "estimated_peak_bytes": estimate.estimated_peak_bytes,
        "memory_budget_bytes": estimate.memory_budget_bytes,
        "reason": estimate.reason,
    }


router = APIRouter(prefix="/condition-research", tags=["condition-research"])


class ConditionResearchServiceLike(Protocol):
    def run(
        self,
        config: ConditionResearchConfig,
        progress_callback: Callable[[int, int], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> dict[str, object]: ...

    def estimate_resources(self, config: ConditionResearchConfig) -> ResearchResourceEstimate: ...


def _runner(request: Request) -> ScreenJobRunner:
    return cast(ScreenJobRunner, request.app.state.screen_job_runner)


def _service(request: Request) -> ConditionResearchServiceLike:
    return cast(
        ConditionResearchService | ConditionResearchServiceLike,
        request.app.state.condition_research_service,
    )


def _store(request: Request) -> DuckDbMarketDataStore:
    return cast(DuckDbMarketDataStore, request.app.state.market_data_store)


def _state_or_404(job_id: str, request: Request) -> JobState:
    state = _runner(request).get(job_id)
    if state is None or state.description != "买卖条件研究":
        raise HTTPException(status_code=404, detail="买卖条件研究任务不存在或已过期")
    return state


@router.get("/capabilities")
def condition_research_capabilities() -> dict[str, object]:
    return {
        "data_frequency": "daily",
        "universe": "all_stock_refs",
        "portfolio": False,
        "max_positions": None,
        "default_range": "recent5",
        "default_split": {"train": 0.6, "validation": 0.2, "holdout": 0.2},
        "search_sides": ["buy", "sell", "both"],
        "default_candidate_limit_per_side": 5,
        "default_holdout_candidates_per_side": 1,
        "search_type": "contrastive_threshold_and_pairwise_rules",
    }


@router.post("", status_code=status.HTTP_202_ACCEPTED)
def create_condition_research(
    payload: ConditionResearchRequest,
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
        resolved_payload = payload.model_copy(
            update={
                "date_mode": "custom",
                "start_date": start_date,
                "end_date": end_date,
            }
        )
        config = resolved_payload.to_config()
        estimate = _service(request).estimate_resources(config)
        if not estimate.within_budget:
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                content={
                    "error": {
                        "code": "resource_budget_exceeded",
                        "message": str(ResearchResourceBudgetError(estimate)),
                        "estimate": _estimate_payload(estimate),
                    }
                },
            )
        request_snapshot = cast(
            dict[str, object],
            resolved_payload.model_dump(mode="json"),
        )
        job_id = uuid.uuid4().hex

        def run_task(progress: ProgressReporter) -> dict[str, object]:
            started_at = utc_now()
            try:
                result = _service(request).run(
                    config,
                    progress,
                    lambda: _runner(request).is_cancel_requested(job_id),
                )
            except ValueError as exc:
                raise TaskUserError(str(exc)) from exc
            finished_at = utc_now()
            save_completed_backtest_history(
                _store(request),
                history_id=job_id,
                backtest_type="condition_research",
                request=request_snapshot,
                result=result,
                started_at=started_at,
                finished_at=finished_at,
            )
            return result

        job_id = _runner(request).submit_callable(
            run_task,
            description="买卖条件研究",
            job_id=job_id,
            worker_count=1,
            exclusive_group=HEAVY_RESEARCH_EXCLUSIVE_GROUP,
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"error": {"code": "validation_error", "message": str(exc)}},
        )
    except ActiveJobConflictError as exc:
        # 它是 RuntimeError 的子类，落到下面的兜底会变成 503「执行器暂不可用」；
        # 其他路由对同一情况返回 409，这里保持一致。
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="研究任务执行器暂不可用") from exc
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"data": {"job_id": job_id, "status": "queued"}},
    )


@router.get("/history")
def condition_research_history(request: Request) -> dict[str, object]:
    try:
        return {"data": _store(request).list_backtest_history("condition_research")}
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="买卖条件研究历史暂不可用") from exc


@router.get("/history/{history_id}")
def condition_research_history_detail(
    history_id: str,
    request: Request,
) -> dict[str, object]:
    try:
        record = _store(request).read_backtest_history(history_id)
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="买卖条件研究历史暂不可用") from exc
    if record is None or record.get("backtest_type") != "condition_research":
        raise HTTPException(status_code=404, detail="买卖条件研究历史不存在")
    return {"data": record}


@router.delete("/history/{history_id}")
def delete_condition_research_history(
    history_id: str,
    request: Request,
) -> dict[str, object]:
    try:
        record = _store(request).read_backtest_history(history_id)
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="买卖条件研究历史暂不可用") from exc
    # 别的历史类型共用一张表，删错了只剩一句「已删除」会让人以为删的是研究。
    if record is None or record.get("backtest_type") != "condition_research":
        return {"data": {"deleted": False}}
    deleted = _store(request).delete_backtest_history(history_id)
    return {"data": {"deleted": deleted}}


@router.get("/{job_id}")
def condition_research_status(job_id: str, request: Request) -> dict[str, object]:
    state = _state_or_404(job_id, request)
    snapshot = state.snapshot()
    snapshot["result"] = None
    return {"data": snapshot}


@router.get("/{job_id}/results")
def condition_research_results(job_id: str, request: Request) -> dict[str, object]:
    state = _state_or_404(job_id, request)
    if state.status == "failed":
        raise TaskFailedError(state.error or "买卖条件研究失败")
    if state.status == "cancelled":
        raise HTTPException(status_code=409, detail="买卖条件研究任务已取消")
    if state.status != "completed" or state.result_payload is None:
        raise HTTPException(status_code=409, detail="买卖条件研究尚未完成")
    return {"data": state.result_payload}


@router.post("/{job_id}/cancel")
def cancel_condition_research(job_id: str, request: Request) -> dict[str, object]:
    _state_or_404(job_id, request)
    return {"data": {"cancelled": _runner(request).request_cancel(job_id)}}


__all__ = ["router"]
