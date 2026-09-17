"""Automatic strategy discovery endpoints for local portfolio experiments."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Protocol, cast

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse

from selector_app.market_data.store import DuckDbMarketDataStore, MarketDataStoreError
from selector_app.strategy_discovery.job_backend import StrategyJobBackend
from selector_app.strategy_discovery.job_store import JobOperation
from selector_app.strategy_discovery.resources import (
    DiscoveryResourceBudgetError,
    DiscoveryResourceEstimate,
)
from selector_app.strategy_discovery.service import StrategyDiscoveryService

from ..schemas import (
    StrategyDiscoveryRequest,
    StrategyRefinementRequest,
    StrategyReplayRequest,
    resolve_strategy_discovery_defaults,
)

router = APIRouter(prefix="/strategy-discovery", tags=["strategy-discovery"])
logger = logging.getLogger(__name__)


class StrategyDiscoveryServiceLike(Protocol):
    def capabilities(self) -> dict[str, object]: ...


def _service(request: Request) -> StrategyDiscoveryServiceLike:
    return cast(StrategyDiscoveryService, request.app.state.strategy_discovery_service)


def _backend(request: Request) -> StrategyJobBackend:
    return cast(StrategyJobBackend, request.app.state.strategy_job_backend)


def _store(request: Request) -> DuckDbMarketDataStore:
    return cast(DuckDbMarketDataStore, request.app.state.market_data_store)


def _preflight_job(
    request: Request,
    operation: JobOperation,
    payload: Mapping[str, object],
) -> DiscoveryResourceEstimate | JSONResponse | None:
    try:
        return _backend(request).preflight_operation(operation, payload)
    except DiscoveryResourceBudgetError as exc:
        estimate = exc.estimate
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "error": {
                    "code": "resource_budget_exceeded",
                    "message": str(exc),
                    "details": {
                        "row_count": estimate.row_count,
                        "snapshot_bytes": estimate.snapshot_bytes,
                        "estimated_peak_bytes": estimate.estimated_peak_bytes,
                        "memory_budget_bytes": estimate.memory_budget_bytes,
                        "requested_workers": estimate.requested_workers,
                        "selected_workers": estimate.selected_workers,
                    },
                }
            },
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"error": {"code": "validation_error", "message": str(exc)}},
        )
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(
            status_code=503,
            detail="本地行情数据暂不可用，请检查 DuckDB 后重试",
        ) from exc


def _resource_metadata(estimate: DiscoveryResourceEstimate | None) -> dict[str, object]:
    if estimate is None:
        return {}
    return {
        "resource_estimate": {
            "row_count": estimate.row_count,
            "snapshot_bytes": estimate.snapshot_bytes,
            "estimated_peak_bytes": estimate.estimated_peak_bytes,
            "memory_budget_bytes": estimate.memory_budget_bytes,
            "requested_workers": estimate.requested_workers,
            "selected_workers": estimate.selected_workers,
            "reason": estimate.reason,
        }
    }


def _state_or_404(job_id: str, request: Request) -> dict[str, object]:
    state = _backend(request).get(job_id)
    if state is None or state.get("description") not in {"策略实验室", "策略二次优化"}:
        raise HTTPException(status_code=404, detail="策略实验任务不存在或已过期")
    return state


def _experiment_or_404(experiment_id: str, request: Request) -> dict[str, object]:
    state = _backend(request).get(experiment_id)
    if state is not None and state.get("description") in {"策略实验室", "策略二次优化"}:
        if state.get("status") != "completed":
            raise HTTPException(status_code=409, detail="策略实验尚未完成，暂时不能全量复核")
        result = _backend(request).result(experiment_id)
        if result is None:
            raise HTTPException(status_code=409, detail="策略实验结果尚未准备好")
        return {"history_id": experiment_id, **result}
    record = _store(request).read_strategy_discovery_history(experiment_id)
    if record is None:
        raise HTTPException(status_code=404, detail="策略实验历史不存在")
    return record


@router.get("/capabilities")
def strategy_discovery_capabilities(request: Request) -> dict[str, object]:
    try:
        return {"data": _service(request).capabilities()}
    except Exception as exc:  # noqa: BLE001 - hide dependency internals from API clients
        raise HTTPException(status_code=503, detail="策略实验室能力暂不可用") from exc


@router.post("", status_code=status.HTTP_202_ACCEPTED)
def create_strategy_discovery(
    payload: StrategyDiscoveryRequest,
    request: Request,
) -> JSONResponse:
    try:
        if payload.universe == "custom" and payload.universe_file:
            if not Path(payload.universe_file).expanduser().is_file():
                raise ValueError(f"自定义股票列表文件不存在: {payload.universe_file}")
        if payload.start_date is None and payload.end_date is None:
            store_status = _store(request).status()
            default_start, default_end = resolve_strategy_discovery_defaults(
                store_status.data_start,
                store_status.data_end,
                payload.date_mode,
            )
            discovery_request = payload.to_discovery_request(
                default_start=default_start,
                default_end=default_end,
            )
        else:
            discovery_request = payload.to_discovery_request()
    except ValueError as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"error": {"code": "validation_error", "message": str(exc)}},
        )
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(
            status_code=503, detail="本地行情数据暂不可用，请检查 DuckDB 后重试"
        ) from exc

    resource_estimate = _preflight_job(
        request,
        "discovery",
        {"request": discovery_request.to_dict()},
    )
    if isinstance(resource_estimate, JSONResponse):
        return resource_estimate

    try:
        job_id = _backend(request).submit(
            "discovery",
            {"request": discovery_request.to_dict()},
            description="策略实验室",
            worker_count=(
                resource_estimate.selected_workers
                if isinstance(resource_estimate, DiscoveryResourceEstimate)
                else None
            ),
            metadata=_resource_metadata(
                resource_estimate
                if isinstance(resource_estimate, DiscoveryResourceEstimate)
                else None
            ),
        )
    except (RuntimeError, OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="任务执行器暂不可用") from exc
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"data": {"job_id": job_id, "status": "queued"}},
    )


@router.post("/refinements", status_code=status.HTTP_202_ACCEPTED)
def create_strategy_refinement(
    payload: StrategyRefinementRequest,
    request: Request,
) -> JSONResponse:
    experiment = _store(request).read_strategy_discovery_history(payload.parent_history_id)
    if experiment is None:
        raise HTTPException(status_code=404, detail="父策略实验历史不存在")
    parent_found = False
    for key in ("results_by_sharpe", "results_by_return"):
        candidates = experiment.get(key)
        if isinstance(candidates, (list, tuple)) and any(
            isinstance(candidate, Mapping)
            and str(candidate.get("candidate_hash")) == payload.parent_candidate_hash
            for candidate in candidates
        ):
            parent_found = True
            break
    if not parent_found:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"error": {"code": "validation_error", "message": "父实验中不存在指定候选"}},
        )
    job_payload: dict[str, object] = {
        "experiment": experiment,
        "parent_candidate_hash": payload.parent_candidate_hash,
        "max_trials": payload.max_trials,
        "seed": payload.seed,
        "radius": payload.refinement_radius,
    }
    resource_estimate = _preflight_job(request, "refinement", job_payload)
    if isinstance(resource_estimate, JSONResponse):
        return resource_estimate
    try:
        job_id = _backend(request).submit(
            "refinement",
            job_payload,
            description="策略二次优化",
            worker_count=(
                resource_estimate.selected_workers
                if isinstance(resource_estimate, DiscoveryResourceEstimate)
                else None
            ),
            metadata=_resource_metadata(
                resource_estimate
                if isinstance(resource_estimate, DiscoveryResourceEstimate)
                else None
            ),
        )
    except (RuntimeError, OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="任务执行器暂不可用") from exc
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"data": {"job_id": job_id, "status": "queued"}},
    )


@router.post("/{job_id}/cancel")
def cancel_strategy_discovery(job_id: str, request: Request) -> dict[str, object]:
    _state_or_404(job_id, request)
    if not _backend(request).cancel(job_id):
        state = _backend(request).get(job_id)
        return {
            "data": {
                "job_id": job_id,
                "status": state.get("status") if state is not None else "unknown",
                "cancel_requested": False,
            }
        }
    return {"data": {"job_id": job_id, "status": "cancelling", "cancel_requested": True}}


@router.get("/history")
def strategy_discovery_history(request: Request) -> dict[str, object]:
    try:
        return {"data": _store(request).list_strategy_discovery_history()}
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="策略实验历史暂不可用") from exc


@router.get("/history/{history_id}")
def strategy_discovery_history_detail(history_id: str, request: Request) -> dict[str, object]:
    try:
        result = _store(request).read_strategy_discovery_history(history_id)
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="策略实验历史暂不可用") from exc
    if result is None:
        raise HTTPException(status_code=404, detail="策略实验历史不存在")
    return {"data": result}


@router.delete("/history/{history_id}")
def delete_strategy_discovery_history(history_id: str, request: Request) -> dict[str, object]:
    try:
        deleted = _store(request).delete_strategy_discovery_history(history_id)
    except (MarketDataStoreError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="策略实验历史暂不可用") from exc
    return {"data": {"deleted": deleted}}


@router.post("/{experiment_id}/replays", status_code=status.HTTP_202_ACCEPTED)
def create_strategy_replay(
    experiment_id: str,
    payload: StrategyReplayRequest,
    request: Request,
) -> JSONResponse:
    try:
        experiment = _experiment_or_404(experiment_id, request)
        job_payload: dict[str, object] = {
            "experiment": experiment,
            "leaderboard": payload.leaderboard,
        }
        resource_estimate = _preflight_job(request, "replay", job_payload)
        if isinstance(resource_estimate, JSONResponse):
            return resource_estimate
        job_id = _backend(request).submit(
            "replay",
            job_payload,
            description="策略前十全量复核",
            worker_count=(
                resource_estimate.selected_workers
                if isinstance(resource_estimate, DiscoveryResourceEstimate)
                else None
            ),
            metadata={
                **_resource_metadata(
                    resource_estimate
                    if isinstance(resource_estimate, DiscoveryResourceEstimate)
                    else None
                ),
                "experiment_id": experiment_id,
                "leaderboard": payload.leaderboard,
            },
        )
    except HTTPException:
        raise
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="本地行情或策略实验历史暂不可用") from exc
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


def _replay_state_or_404(job_id: str, request: Request) -> dict[str, object]:
    state = _backend(request).get(job_id)
    if state is None or state.get("description") != "策略前十全量复核":
        raise HTTPException(status_code=404, detail="策略复核任务不存在或已过期")
    return state


@router.get("/replay-jobs/{job_id}")
def strategy_replay_status(job_id: str, request: Request) -> dict[str, object]:
    state = _replay_state_or_404(job_id, request)
    snapshot = dict(state)
    snapshot["result"] = None
    return {"data": snapshot}


@router.get("/replay-jobs/{job_id}/results")
def strategy_replay_results(job_id: str, request: Request) -> dict[str, object]:
    state = _replay_state_or_404(job_id, request)
    if state.get("status") == "failed":
        raise HTTPException(status_code=500, detail=str(state.get("error") or "策略复核任务失败"))
    if state.get("status") != "completed":
        raise HTTPException(status_code=409, detail="策略复核尚未完成")
    result = _backend(request).result(job_id)
    if result is None:
        raise HTTPException(status_code=409, detail="策略复核结果尚未准备好")
    return {"data": result}


@router.post("/replay-jobs/{job_id}/cancel")
def cancel_strategy_replay(job_id: str, request: Request) -> dict[str, object]:
    _replay_state_or_404(job_id, request)
    if not _backend(request).cancel(job_id):
        state = _backend(request).get(job_id)
        return {
            "data": {
                "job_id": job_id,
                "status": state.get("status") if state is not None else "unknown",
                "cancel_requested": False,
            }
        }
    return {"data": {"job_id": job_id, "status": "cancelling", "cancel_requested": True}}


@router.get("/history/{experiment_id}/replays")
def strategy_replay_history(
    experiment_id: str,
    request: Request,
    leaderboard: Literal["sharpe", "return"],
) -> dict[str, object]:
    try:
        record = _store(request).read_strategy_discovery_replay_history(experiment_id, leaderboard)
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="策略复核历史暂不可用") from exc
    if record is None:
        raise HTTPException(status_code=404, detail="策略复核历史不存在")
    return {"data": record}


@router.get("/{job_id}")
def strategy_discovery_status(job_id: str, request: Request) -> dict[str, object]:
    state = _state_or_404(job_id, request)
    snapshot = dict(state)
    snapshot["result"] = None
    return {"data": snapshot}


@router.get("/{job_id}/results")
def strategy_discovery_results(job_id: str, request: Request) -> dict[str, object]:
    state = _state_or_404(job_id, request)
    if state.get("status") == "failed":
        raise HTTPException(status_code=500, detail=str(state.get("error") or "策略实验任务失败"))
    if state.get("status") != "completed":
        raise HTTPException(status_code=409, detail="策略实验尚未完成")
    result = _backend(request).result(job_id)
    if result is None:
        raise HTTPException(status_code=409, detail="策略实验结果尚未准备好")
    return {"data": result}
