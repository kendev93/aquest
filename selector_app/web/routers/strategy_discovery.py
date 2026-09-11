"""Automatic strategy discovery endpoints for local portfolio experiments."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol, cast

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse

from selector_app.market_data.store import DuckDbMarketDataStore, MarketDataStoreError
from selector_app.screening.jobs import (
    JobState,
    ScreenJobRunner,
    TaskCancelledError,
    TaskUserError,
)
from selector_app.strategy_discovery.models import DiscoveryRequest
from selector_app.strategy_discovery.replay import ReplayLeaderboard
from selector_app.strategy_discovery.replay_service import ReplayProgressCallback
from selector_app.strategy_discovery.service import StrategyDiscoveryService

from ..schemas import StrategyDiscoveryRequest, StrategyRefinementRequest, StrategyReplayRequest

router = APIRouter(prefix="/strategy-discovery", tags=["strategy-discovery"])
logger = logging.getLogger(__name__)


class StrategyDiscoveryServiceLike(Protocol):
    def capabilities(self) -> dict[str, object]: ...

    def run(
        self,
        request: DiscoveryRequest,
        *,
        progress_callback: Callable[..., None] | None = None,
        abort_check: Callable[[], bool] | None = None,
    ) -> dict[str, object]: ...

    def replay(
        self,
        experiment: Mapping[str, object],
        leaderboard: ReplayLeaderboard,
        *,
        progress_callback: ReplayProgressCallback | None = None,
        abort_check: Callable[[], bool] | None = None,
    ) -> dict[str, object]: ...

    def refine(
        self,
        experiment: Mapping[str, object],
        parent_candidate_hash: str,
        *,
        max_trials: int | None = None,
        seed: int | None = None,
        radius: Literal["medium"] = "medium",
        progress_callback: Callable[..., None] | None = None,
        abort_check: Callable[[], bool] | None = None,
    ) -> dict[str, object]: ...


def _runner(request: Request) -> ScreenJobRunner:
    return cast(ScreenJobRunner, request.app.state.screen_job_runner)


def _service(request: Request) -> StrategyDiscoveryServiceLike:
    return cast(StrategyDiscoveryService, request.app.state.strategy_discovery_service)


def _store(request: Request) -> DuckDbMarketDataStore:
    return cast(DuckDbMarketDataStore, request.app.state.market_data_store)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _state_or_404(job_id: str, request: Request) -> JobState:
    state = _runner(request).get(job_id)
    if state is None or state.description not in {"策略实验室", "策略二次优化"}:
        raise HTTPException(status_code=404, detail="策略实验任务不存在或已过期")
    return state


def _experiment_or_404(experiment_id: str, request: Request) -> dict[str, object]:
    state = _runner(request).get(experiment_id)
    if state is not None and state.description in {"策略实验室", "策略二次优化"}:
        if state.status != "completed" or state.result_payload is None:
            raise HTTPException(status_code=409, detail="策略实验尚未完成，暂时不能全量复核")
        return {"history_id": experiment_id, **state.result_payload}
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
            discovery_request = payload.to_discovery_request(
                default_start=store_status.data_start,
                default_end=store_status.data_end,
            )
        else:
            discovery_request = payload.to_discovery_request()
    except ValueError as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": {"code": "validation_error", "message": str(exc)}},
        )
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(
            status_code=503, detail="本地行情数据暂不可用，请检查 DuckDB 后重试"
        ) from exc

    job_id_holder = {"value": ""}

    def run_discovery(progress: Callable[[int, int], None]) -> dict[str, object]:
        started_at = _utc_now()

        def report_progress(
            current: int,
            total: int,
            phase: str,
            metadata: dict[str, object] | None = None,
        ) -> None:
            progress(current, total)
            if job_id_holder["value"]:
                overall_progress = metadata.get("overall_progress") if metadata else None
                _runner(request).update_callable_state(
                    job_id_holder["value"],
                    phase=phase,
                    metadata=dict(metadata or {}),
                    progress=(
                        float(overall_progress)
                        if isinstance(overall_progress, (int, float))
                        else None
                    ),
                )

        try:
            result = _service(request).run(
                discovery_request,
                progress_callback=report_progress,
                abort_check=lambda: _runner(request).is_cancel_requested(job_id_holder["value"]),
            )
            result = dict(result)
            finished_at = _utc_now()
            result["started_at"] = started_at.isoformat()
            result["finished_at"] = finished_at.isoformat()
            history_id = job_id_holder["value"]
            search_metadata = result.get("search_metadata")
            if isinstance(search_metadata, dict):
                _runner(request).update_callable_state(
                    history_id,
                    phase="completed",
                    metadata={
                        "algorithm": search_metadata.get("algorithm"),
                        "surrogate_status": search_metadata.get("surrogate_status"),
                        "surrogate_enabled": search_metadata.get("surrogate_status") == "enabled",
                        "stop_reason": search_metadata.get("stop_reason"),
                        "overall_progress": 1.0,
                        "estimated_seconds_remaining": 0.0,
                    },
                    progress=1.0,
                )
            if history_id:
                result["history_id"] = history_id
                _store(request).save_strategy_discovery_history(history_id, result)
            return result
        except RuntimeError as exc:
            if _runner(request).is_cancel_requested(job_id_holder["value"]):
                raise TaskCancelledError("策略实验已取消") from exc
            raise TaskUserError(str(exc)) from exc
        except (ValueError, MarketDataStoreError, OSError) as exc:
            if _runner(request).is_cancel_requested(job_id_holder["value"]):
                raise TaskCancelledError("策略实验已取消") from exc
            raise TaskUserError(str(exc)) from exc

    try:
        job_id = _runner(request).submit_callable(run_discovery, description="策略实验室")
        job_id_holder["value"] = job_id
    except RuntimeError as exc:
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
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": {"code": "validation_error", "message": "父实验中不存在指定候选"}},
        )
    job_id = uuid.uuid4().hex

    def run_refinement(progress: Callable[[int, int], None]) -> dict[str, object]:
        started_at = _utc_now()

        def report_progress(
            current: int,
            total: int,
            phase: str,
            metadata: dict[str, object] | None = None,
        ) -> None:
            progress(current, total)
            overall_progress = metadata.get("overall_progress") if metadata else None
            _runner(request).update_callable_state(
                job_id,
                phase=phase,
                metadata=dict(metadata or {}),
                progress=(
                    float(overall_progress) if isinstance(overall_progress, (int, float)) else None
                ),
            )

        try:
            result = dict(
                _service(request).refine(
                    experiment,
                    payload.parent_candidate_hash,
                    max_trials=payload.max_trials,
                    seed=payload.seed,
                    radius=payload.refinement_radius,
                    progress_callback=report_progress,
                    abort_check=lambda: _runner(request).is_cancel_requested(job_id),
                )
            )
            finished_at = _utc_now()
            result.update(
                {
                    "history_id": job_id,
                    "started_at": started_at.isoformat(),
                    "finished_at": finished_at.isoformat(),
                }
            )
            _runner(request).update_callable_state(
                job_id,
                phase="completed",
                metadata={
                    "overall_progress": 1.0,
                    "refinement": True,
                    "parent_candidate_hash": payload.parent_candidate_hash,
                },
                progress=1.0,
            )
            _store(request).save_strategy_discovery_history(
                job_id,
                result,
                started_at=started_at,
                finished_at=finished_at,
            )
            return result
        except RuntimeError as exc:
            if _runner(request).is_cancel_requested(job_id):
                raise TaskCancelledError("策略二次优化已取消") from exc
            raise TaskUserError(str(exc)) from exc
        except (ValueError, MarketDataStoreError, OSError) as exc:
            if _runner(request).is_cancel_requested(job_id):
                raise TaskCancelledError("策略二次优化已取消") from exc
            raise TaskUserError(str(exc)) from exc

    try:
        _runner(request).submit_callable(
            run_refinement,
            description="策略二次优化",
            job_id=job_id,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="任务执行器暂不可用") from exc
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"data": {"job_id": job_id, "status": "queued"}},
    )


@router.post("/{job_id}/cancel")
def cancel_strategy_discovery(job_id: str, request: Request) -> dict[str, object]:
    _state_or_404(job_id, request)
    if not _runner(request).request_cancel(job_id):
        state = _runner(request).get(job_id)
        return {
            "data": {
                "job_id": job_id,
                "status": state.status if state is not None else "unknown",
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
        job_id = uuid.uuid4().hex

        def run_replay(progress: Callable[[int, int], None]) -> dict[str, object]:
            started_at = _utc_now()

            def report_progress(
                current: int,
                total: int,
                phase: str,
                metadata: Mapping[str, object] | None = None,
            ) -> None:
                progress(current, total)
                overall = metadata.get("overall_progress") if metadata else None
                _runner(request).update_callable_state(
                    job_id,
                    phase=phase,
                    metadata=dict(metadata or {}),
                    progress=(float(overall) if isinstance(overall, (int, float)) else None),
                )

            try:
                result = dict(
                    _service(request).replay(
                        experiment,
                        payload.leaderboard,
                        progress_callback=report_progress,
                        abort_check=lambda: _runner(request).is_cancel_requested(job_id),
                    )
                )
                finished_at = _utc_now()
                duration_seconds = max(0.0, (finished_at - started_at).total_seconds())
                result.update(
                    {
                        "replay_id": job_id,
                        "started_at": started_at.isoformat(),
                        "finished_at": finished_at.isoformat(),
                        "duration_seconds": duration_seconds,
                    }
                )
                diagnostics = result.get("diagnostics")
                error_count = len(diagnostics) if isinstance(diagnostics, Mapping) else 0
                raw_candidate_count = result.get("candidate_count", 0)
                candidate_count = (
                    max(0, int(raw_candidate_count))
                    if isinstance(raw_candidate_count, (int, float))
                    else 0
                )
                _runner(request).update_callable_state(
                    job_id,
                    phase="completed",
                    metadata={
                        "overall_progress": 1.0,
                        "candidate_count": candidate_count,
                        "data_changed": result.get("data_changed", False),
                        "finished_at": finished_at.isoformat(),
                        "duration_seconds": duration_seconds,
                    },
                    progress=1.0,
                    errors=error_count,
                    total_candidates=candidate_count,
                    total_scanned=candidate_count,
                )
                try:
                    _store(request).save_strategy_discovery_replay_history(
                        job_id,
                        result,
                        started_at=started_at,
                        finished_at=finished_at,
                        duration_seconds=duration_seconds,
                    )
                except Exception:  # noqa: BLE001 - replay result remains viewable in job memory
                    logger.exception("自动保存策略复核历史失败: %s", job_id)
                return result
            except RuntimeError as exc:
                if _runner(request).is_cancel_requested(job_id):
                    raise TaskCancelledError("策略复核已取消") from exc
                raise TaskUserError(str(exc)) from exc
            except (ValueError, MarketDataStoreError, OSError) as exc:
                if _runner(request).is_cancel_requested(job_id):
                    raise TaskCancelledError("策略复核已取消") from exc
                raise TaskUserError(str(exc)) from exc

        _runner(request).submit_callable(
            run_replay,
            description="策略前十全量复核",
            worker_count=8,
            job_id=job_id,
        )
    except HTTPException:
        raise
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="本地行情或策略实验历史暂不可用") from exc
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


def _replay_state_or_404(job_id: str, request: Request) -> JobState:
    state = _runner(request).get(job_id)
    if state is None or state.description != "策略前十全量复核":
        raise HTTPException(status_code=404, detail="策略复核任务不存在或已过期")
    return state


@router.get("/replay-jobs/{job_id}")
def strategy_replay_status(job_id: str, request: Request) -> dict[str, object]:
    state = _replay_state_or_404(job_id, request)
    snapshot = state.snapshot()
    snapshot["result"] = None
    return {"data": snapshot}


@router.get("/replay-jobs/{job_id}/results")
def strategy_replay_results(job_id: str, request: Request) -> dict[str, object]:
    state = _replay_state_or_404(job_id, request)
    if state.status == "failed":
        raise HTTPException(status_code=500, detail=state.error or "策略复核任务失败")
    if state.status != "completed" or state.result_payload is None:
        raise HTTPException(status_code=409, detail="策略复核尚未完成")
    return {"data": state.result_payload}


@router.post("/replay-jobs/{job_id}/cancel")
def cancel_strategy_replay(job_id: str, request: Request) -> dict[str, object]:
    _replay_state_or_404(job_id, request)
    if not _runner(request).request_cancel(job_id):
        state = _runner(request).get(job_id)
        return {
            "data": {
                "job_id": job_id,
                "status": state.status if state is not None else "unknown",
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
    snapshot = state.snapshot()
    snapshot["result"] = None
    return {"data": snapshot}


@router.get("/{job_id}/results")
def strategy_discovery_results(job_id: str, request: Request) -> dict[str, object]:
    state = _state_or_404(job_id, request)
    if state.status == "failed":
        raise HTTPException(status_code=500, detail=state.error or "策略实验任务失败")
    if state.status != "completed" or state.result_payload is None:
        raise HTTPException(status_code=409, detail="策略实验尚未完成")
    return {"data": state.result_payload}
