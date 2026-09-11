"""Batch strategy suitability evaluation endpoints."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse

from selector_app.market_data.store import DuckDbMarketDataStore, MarketDataStoreError
from selector_app.screening.jobs import JobState, ScreenJobRunner, TaskUserError
from selector_app.strategy_fitness.models import StrategyFitnessConfig, StrategyFitnessReport
from selector_app.strategy_fitness.service import (
    StrategyFitnessProgressCallback,
    StrategyFitnessService,
)

from ..backtest_history import save_completed_backtest_history, utc_now
from ..schemas import StrategyFitnessRequest

router = APIRouter(prefix="/strategy-fitness", tags=["strategy-fitness"])


class StrategyFitnessServiceLike(Protocol):
    def run(
        self,
        config: StrategyFitnessConfig,
        progress_callback: StrategyFitnessProgressCallback | None = None,
    ) -> StrategyFitnessReport | dict[str, object]: ...


def _runner(request: Request) -> ScreenJobRunner:
    return cast(ScreenJobRunner, request.app.state.screen_job_runner)


def _service(request: Request) -> StrategyFitnessServiceLike:
    return cast(
        StrategyFitnessService | StrategyFitnessServiceLike,
        request.app.state.strategy_fitness_service,
    )


def _store(request: Request) -> DuckDbMarketDataStore:
    return cast(DuckDbMarketDataStore, request.app.state.market_data_store)


def _state_or_404(job_id: str, request: Request) -> JobState:
    state = _runner(request).get(job_id)
    if state is None or state.description != "策略适配性评估":
        raise HTTPException(status_code=404, detail="策略适配性评估任务不存在或已过期")
    return state


@router.post("", status_code=status.HTTP_202_ACCEPTED)
def create_strategy_fitness(
    payload: StrategyFitnessRequest,
    request: Request,
) -> JSONResponse:
    try:
        if payload.universe == "custom" and payload.universe_file:
            if not Path(payload.universe_file).expanduser().is_file():
                raise ValueError(f"自定义股票列表文件不存在: {payload.universe_file}")
        config = payload.to_fitness_config()
        request_snapshot = cast(dict[str, object], payload.model_dump(mode="json"))
        job_id = uuid.uuid4().hex

        def run_fitness(progress: Callable[[int, int], None]) -> dict[str, object]:
            started_at = utc_now()
            try:
                result = _service(request).run(config, progress)
            except ValueError as exc:
                raise TaskUserError(str(exc)) from exc
            result_payload = (
                result.to_dict() if isinstance(result, StrategyFitnessReport) else dict(result)
            )
            finished_at = utc_now()
            save_completed_backtest_history(
                _store(request),
                history_id=job_id,
                backtest_type="strategy_fitness",
                request=request_snapshot,
                result=result_payload,
                started_at=started_at,
                finished_at=finished_at,
            )
            return result_payload

        job_id = _runner(request).submit_callable(
            run_fitness, description="策略适配性评估", job_id=job_id
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": {"code": "validation_error", "message": str(exc)}},
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="任务执行器暂不可用") from exc
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"data": {"job_id": job_id, "status": "queued"}},
    )


@router.get("/history")
def strategy_fitness_history(request: Request) -> dict[str, object]:
    try:
        return {"data": _store(request).list_backtest_history("strategy_fitness")}
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="策略适配性历史暂不可用") from exc


@router.get("/history/{history_id}")
def strategy_fitness_history_detail(history_id: str, request: Request) -> dict[str, object]:
    try:
        record = _store(request).read_backtest_history(history_id)
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="策略适配性历史暂不可用") from exc
    if record is None or record.get("backtest_type") != "strategy_fitness":
        raise HTTPException(status_code=404, detail="策略适配性历史不存在")
    return {"data": record}


@router.delete("/history/{history_id}")
def delete_strategy_fitness_history(history_id: str, request: Request) -> dict[str, object]:
    try:
        record = _store(request).read_backtest_history(history_id)
        if record is None or record.get("backtest_type") != "strategy_fitness":
            return {"data": {"deleted": False}}
        deleted = _store(request).delete_backtest_history(history_id)
    except (MarketDataStoreError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="策略适配性历史暂不可用") from exc
    return {"data": {"deleted": deleted}}


@router.get("/{job_id}")
def strategy_fitness_status(job_id: str, request: Request) -> dict[str, object]:
    state = _state_or_404(job_id, request)
    snapshot = state.snapshot()
    snapshot["result"] = None
    return {"data": snapshot}


@router.get("/{job_id}/results")
def strategy_fitness_results(job_id: str, request: Request) -> dict[str, object]:
    state = _state_or_404(job_id, request)
    if state.status == "failed":
        raise HTTPException(status_code=500, detail=state.error or "策略适配性评估任务失败")
    if state.status != "completed" or state.result_payload is None:
        raise HTTPException(status_code=409, detail="策略适配性评估尚未完成")
    return {"data": state.result_payload}
