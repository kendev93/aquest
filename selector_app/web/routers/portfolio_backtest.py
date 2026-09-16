"""Dynamic ranked-slot portfolio backtest endpoints."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse

from selector_app.market_data.models import DataStoreStatus
from selector_app.market_data.store import DuckDbMarketDataStore, MarketDataStoreError
from selector_app.portfolio_backtest.models import (
    PortfolioBacktestConfig,
    PortfolioBacktestReport,
)
from selector_app.portfolio_backtest.service import (
    PortfolioBacktestProgressCallback,
    PortfolioBacktestService,
)
from selector_app.screening.jobs import JobState, ScreenJobRunner, TaskUserError

from ..backtest_history import save_completed_backtest_history, utc_now
from ..date_ranges import resolve_date_range
from ..market_source import resolve_market_status
from ..schemas import PortfolioBacktestRequest

router = APIRouter(prefix="/portfolio-backtests", tags=["portfolio-backtests"])


class PortfolioBacktestServiceLike(Protocol):
    def run(
        self,
        config: PortfolioBacktestConfig,
        progress_callback: PortfolioBacktestProgressCallback | None = None,
    ) -> PortfolioBacktestReport | dict[str, object]: ...


def _runner(request: Request) -> ScreenJobRunner:
    return cast(ScreenJobRunner, request.app.state.screen_job_runner)


def _service(request: Request) -> PortfolioBacktestServiceLike:
    return cast(
        PortfolioBacktestService | PortfolioBacktestServiceLike,
        request.app.state.portfolio_backtest_service,
    )


def _store(request: Request) -> DuckDbMarketDataStore:
    return cast(DuckDbMarketDataStore, request.app.state.market_data_store)


def _resolve_payload_dates(
    payload: PortfolioBacktestRequest,
    status_snapshot: DataStoreStatus,
) -> PortfolioBacktestRequest:
    if payload.start_date is not None or payload.end_date is not None:
        return payload.model_copy(update={"date_mode": "custom"})
    start_date, end_date = resolve_date_range(
        mode=payload.date_mode,
        requested_start=payload.start_date,
        requested_end=payload.end_date,
        available_start=status_snapshot.data_start,
        available_end=status_snapshot.data_end,
    )
    return payload.model_copy(update={"start_date": start_date, "end_date": end_date})


def _state_or_404(job_id: str, request: Request) -> JobState:
    state = _runner(request).get(job_id)
    if state is None or state.description != "组合回测":
        raise HTTPException(status_code=404, detail="组合回测任务不存在或已过期")
    return state


@router.post("", status_code=status.HTTP_202_ACCEPTED)
def create_portfolio_backtest(
    payload: PortfolioBacktestRequest,
    request: Request,
) -> JSONResponse:
    try:
        if payload.universe == "custom" and payload.universe_file:
            if not Path(payload.universe_file).expanduser().is_file():
                raise ValueError(f"自定义股票列表文件不存在: {payload.universe_file}")
        resolved_payload = _resolve_payload_dates(payload, resolve_market_status(request))
        config = resolved_payload.to_config()
        request_snapshot = cast(dict[str, object], resolved_payload.model_dump(mode="json"))
        job_id = uuid.uuid4().hex

        def run_backtest(progress: Callable[[int, int], None]) -> dict[str, object]:
            started_at = utc_now()
            try:
                result = _service(request).run(config, progress)
            except ValueError as exc:
                raise TaskUserError(str(exc)) from exc
            result_payload = (
                result.to_dict() if isinstance(result, PortfolioBacktestReport) else dict(result)
            )
            finished_at = utc_now()
            save_completed_backtest_history(
                _store(request),
                history_id=job_id,
                backtest_type="portfolio",
                request=request_snapshot,
                result=result_payload,
                started_at=started_at,
                finished_at=finished_at,
            )
            return result_payload

        job_id = _runner(request).submit_callable(
            run_backtest, description="组合回测", job_id=job_id
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
def portfolio_backtest_history(request: Request) -> dict[str, object]:
    try:
        return {"data": _store(request).list_backtest_history("portfolio")}
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="组合回测历史暂不可用") from exc


@router.get("/history/{history_id}")
def portfolio_backtest_history_detail(history_id: str, request: Request) -> dict[str, object]:
    try:
        record = _store(request).read_backtest_history(history_id)
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="组合回测历史暂不可用") from exc
    if record is None or record.get("backtest_type") != "portfolio":
        raise HTTPException(status_code=404, detail="组合回测历史不存在")
    return {"data": record}


@router.delete("/history/{history_id}")
def delete_portfolio_backtest_history(history_id: str, request: Request) -> dict[str, object]:
    try:
        record = _store(request).read_backtest_history(history_id)
        if record is None or record.get("backtest_type") != "portfolio":
            return {"data": {"deleted": False}}
        deleted = _store(request).delete_backtest_history(history_id)
    except (MarketDataStoreError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="组合回测历史暂不可用") from exc
    return {"data": {"deleted": deleted}}


@router.get("/{job_id}")
def portfolio_backtest_status(job_id: str, request: Request) -> dict[str, object]:
    state = _state_or_404(job_id, request)
    snapshot = state.snapshot()
    snapshot["result"] = None
    return {"data": snapshot}


@router.get("/{job_id}/results")
def portfolio_backtest_results(job_id: str, request: Request) -> dict[str, object]:
    state = _state_or_404(job_id, request)
    if state.status == "failed":
        raise HTTPException(status_code=500, detail=state.error or "组合回测任务失败")
    if state.status != "completed" or state.result_payload is None:
        raise HTTPException(status_code=409, detail="组合回测尚未完成")
    return {"data": state.result_payload}
