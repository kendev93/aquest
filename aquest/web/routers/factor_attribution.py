"""Trade attribution endpoints: why did these trades win and those lose?

The study reads an archived backtest's trades and compares the two groups, so
the only input that matters is which history record to explain. Everything else
(permutations, alpha, the indicator menu) is a statistical knob and behaves
exactly like the factor test's.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Protocol, cast

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse

from aquest.factor_attribution.models import FactorAttributionConfig
from aquest.factor_attribution.service import TRADABLE_TYPES
from aquest.market_data.store import DuckDbMarketDataStore, MarketDataStoreError
from aquest.screening.jobs import JobState, ScreenJobRunner, TaskUserError

from ..backtest_history import save_completed_backtest_history, utc_now
from ..schemas import FactorAttributionRequest

router = APIRouter(prefix="/factor-attribution", tags=["factor-attribution"])

JOB_DESCRIPTION = "因子归因"


class FactorAttributionServiceLike(Protocol):
    def run(
        self,
        config: FactorAttributionConfig,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> dict[str, object]: ...


def _runner(request: Request) -> ScreenJobRunner:
    return cast(ScreenJobRunner, request.app.state.screen_job_runner)


def _service(request: Request) -> FactorAttributionServiceLike:
    return cast(FactorAttributionServiceLike, request.app.state.factor_attribution_service)


def _store(request: Request) -> DuckDbMarketDataStore:
    return cast(DuckDbMarketDataStore, request.app.state.market_data_store)


def _state_or_404(job_id: str, request: Request) -> JobState:
    state = _runner(request).get(job_id)
    if state is None or state.description != JOB_DESCRIPTION:
        raise HTTPException(status_code=404, detail="因子归因任务不存在或已过期")
    return state


@router.get("/sources")
def attribution_sources(request: Request) -> dict[str, object]:
    """Backtests whose result carries trades, i.e. the attributable ones."""

    try:
        records = _store(request).list_backtest_history_all()
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="回测历史暂不可用") from exc
    return {"data": [record for record in records if record.get("backtest_type") in TRADABLE_TYPES]}


@router.post("", status_code=status.HTTP_202_ACCEPTED)
def create_attribution(payload: FactorAttributionRequest, request: Request) -> JSONResponse:
    try:
        job_id = uuid.uuid4().hex
        config = payload.to_config()
        request_snapshot = cast(dict[str, object], payload.model_dump(mode="json"))
        service = _service(request)

        def run_attribution(progress: Callable[[int, int], None]) -> dict[str, object]:
            started_at = utc_now()
            try:
                result = service.run(config, progress)
            except ValueError as exc:
                raise TaskUserError(str(exc)) from exc
            finished_at = utc_now()
            save_completed_backtest_history(
                _store(request),
                history_id=job_id,
                backtest_type="factor_attribution",
                request=request_snapshot,
                result=result,
                started_at=started_at,
                finished_at=finished_at,
            )
            return result

        job_id = _runner(request).submit_callable(
            run_attribution, description=JOB_DESCRIPTION, job_id=job_id
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"error": {"code": "validation_error", "message": str(exc)}},
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="任务执行器暂不可用") from exc
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"data": {"job_id": job_id, "status": "queued"}},
    )


@router.get("/history")
def attribution_history(request: Request) -> dict[str, object]:
    try:
        return {"data": _store(request).list_backtest_history("factor_attribution")}
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="因子归因历史暂不可用") from exc


@router.get("/history/{history_id}")
def attribution_history_detail(history_id: str, request: Request) -> dict[str, object]:
    try:
        record = _store(request).read_backtest_history(history_id)
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="因子归因历史暂不可用") from exc
    if record is None or record.get("backtest_type") != "factor_attribution":
        raise HTTPException(status_code=404, detail="因子归因历史不存在")
    return {"data": record}


@router.delete("/history/{history_id}")
def delete_attribution_history(history_id: str, request: Request) -> dict[str, object]:
    try:
        record = _store(request).read_backtest_history(history_id)
        if record is None or record.get("backtest_type") != "factor_attribution":
            return {"data": {"deleted": False}}
        deleted = _store(request).delete_backtest_history(history_id)
    except (MarketDataStoreError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="因子归因历史暂不可用") from exc
    return {"data": {"deleted": deleted}}


@router.get("/{job_id}")
def attribution_status(job_id: str, request: Request) -> dict[str, object]:
    state = _state_or_404(job_id, request)
    snapshot = state.snapshot()
    snapshot["result"] = None
    return {"data": snapshot}


@router.get("/{job_id}/results")
def attribution_results(job_id: str, request: Request) -> dict[str, object]:
    state = _state_or_404(job_id, request)
    if state.status == "failed":
        raise HTTPException(status_code=500, detail=state.error or "因子归因任务失败")
    if state.status != "completed" or state.result_payload is None:
        raise HTTPException(status_code=409, detail="因子归因尚未完成")
    return {"data": state.result_payload}
