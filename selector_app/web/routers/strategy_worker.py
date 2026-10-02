"""Private backend endpoints for the isolated strategy worker process."""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from typing import cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from selector_app.market_data.store import MarketDataStoreError
from selector_app.strategy_discovery.duckdb_job_store import DuckDbStrategyJobStore
from selector_app.strategy_discovery.job_store import DEFAULT_STALE_AFTER_SECONDS
from selector_app.strategy_pool.store import DuckDbStrategyPoolStore

router = APIRouter(
    prefix="/internal/strategy-worker",
    tags=["internal-strategy-worker"],
    include_in_schema=False,
)


class WorkerIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_id: str = Field(min_length=1, max_length=128)

    @field_validator("worker_id")
    @classmethod
    def validate_worker_id(cls, value: str) -> str:
        selected = value.strip()
        if not selected:
            raise ValueError("策略 worker ID 无效")
        return selected


class WorkerProgress(WorkerIdentity):
    phase: str | None = Field(default=None, max_length=120)
    progress: float | None = Field(default=None, ge=0.0, le=1.0)
    total_candidates: int | None = Field(default=None, ge=0)
    total_scanned: int | None = Field(default=None, ge=0)
    total_signals: int | None = Field(default=None, ge=0)
    errors: int | None = Field(default=None, ge=0)
    skipped: int | None = Field(default=None, ge=0)
    metadata: dict[str, object] | None = None


class WorkerResult(WorkerIdentity):
    result: dict[str, object]


class WorkerFailure(WorkerIdentity):
    error: str = Field(min_length=1, max_length=500)
    error_code: str = Field(min_length=1, max_length=80)


class WorkerPoolCheckpoint(WorkerIdentity):
    entry: dict[str, object]


def require_worker_token(request: Request) -> None:
    expected = str(getattr(request.app.state, "strategy_worker_token", "") or "")
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="策略 worker 未配置认证凭据",
        )
    authorization = request.headers.get("authorization", "")
    scheme, separator, supplied = authorization.partition(" ")
    if (
        not separator
        or scheme.lower() != "bearer"
        or not supplied
        or not secrets.compare_digest(supplied, expected)
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未授权")


def _store(request: Request) -> DuckDbStrategyJobStore:
    return cast(DuckDbStrategyJobStore, request.app.state.strategy_job_store)


def _pool_store(request: Request) -> DuckDbStrategyPoolStore:
    return cast(DuckDbStrategyPoolStore, request.app.state.strategy_pool_store)


def _conflict(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


def _store_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="策略任务存储暂时被占用，请稍后重试",
    )


@router.post("/jobs/claim", dependencies=[Depends(require_worker_token)])
def claim_job(payload: WorkerIdentity, request: Request) -> dict[str, object]:
    store = _store(request)
    try:
        store.recover_stale_running(stale_after_seconds=DEFAULT_STALE_AFTER_SECONDS)
        job_id = store.claim_next(payload.worker_id)
        if job_id is None:
            return {"job": None}
        return {"job": store.read_claimed_job(job_id, payload.worker_id)}
    except (MarketDataStoreError, OSError) as exc:
        raise _store_unavailable() from exc
    except ValueError as exc:
        raise _conflict(exc) from exc


@router.post("/jobs/{job_id}/heartbeat", dependencies=[Depends(require_worker_token)])
def heartbeat_job(
    job_id: str,
    payload: WorkerIdentity,
    request: Request,
) -> dict[str, object]:
    try:
        state = _store(request).heartbeat(job_id, payload.worker_id)
    except (MarketDataStoreError, OSError) as exc:
        raise _store_unavailable() from exc
    except ValueError as exc:
        raise _conflict(exc) from exc
    return {"data": {"state": state, "cancel_requested": state["cancel_requested"]}}


@router.post("/jobs/{job_id}/pool-entry", dependencies=[Depends(require_worker_token)])
def persist_pool_entry(
    job_id: str,
    payload: WorkerPoolCheckpoint,
    request: Request,
) -> dict[str, object]:
    try:
        claimed = _store(request).read_claimed_job(job_id, payload.worker_id)
        operation = claimed.get("request")
        envelope = operation if isinstance(operation, Mapping) else {}
        if envelope.get("operation") != "market_strategy_pool":
            raise ValueError("当前任务不是全市场策略池任务")
        _pool_store(request).persist_worker_entry(payload.entry, source_job_id=job_id)
    except (MarketDataStoreError, OSError) as exc:
        raise _store_unavailable() from exc
    except ValueError as exc:
        raise _conflict(exc) from exc
    return {"data": {"saved": True}}


@router.post("/jobs/{job_id}/progress", dependencies=[Depends(require_worker_token)])
def update_job_progress(
    job_id: str,
    payload: WorkerProgress,
    request: Request,
) -> dict[str, object]:
    updates = payload.model_dump(exclude={"worker_id"}, exclude_unset=True, exclude_none=True)
    try:
        state = _store(request).update_from_worker(job_id, payload.worker_id, updates)
    except (MarketDataStoreError, OSError) as exc:
        raise _store_unavailable() from exc
    except ValueError as exc:
        raise _conflict(exc) from exc
    return {"data": {"state": state, "cancel_requested": state["cancel_requested"]}}


@router.post("/jobs/{job_id}/complete", dependencies=[Depends(require_worker_token)])
def complete_job(
    job_id: str,
    payload: WorkerResult,
    request: Request,
) -> dict[str, object]:
    try:
        state = _store(request).write_result_from_worker(
            job_id,
            payload.worker_id,
            payload.result,
        )
    except (MarketDataStoreError, OSError) as exc:
        raise _store_unavailable() from exc
    except ValueError as exc:
        raise _conflict(exc) from exc
    return {"data": {"state": state, "cancel_requested": state["cancel_requested"]}}


@router.post("/jobs/{job_id}/fail", dependencies=[Depends(require_worker_token)])
def fail_job(
    job_id: str,
    payload: WorkerFailure,
    request: Request,
) -> dict[str, object]:
    try:
        state = _store(request).fail_from_worker(
            job_id,
            payload.worker_id,
            payload.error,
            payload.error_code,
        )
    except (MarketDataStoreError, OSError) as exc:
        raise _store_unavailable() from exc
    except ValueError as exc:
        raise _conflict(exc) from exc
    return {"data": {"state": state, "cancel_requested": state["cancel_requested"]}}


@router.post("/jobs/{job_id}/cancelled", dependencies=[Depends(require_worker_token)])
def cancel_job(
    job_id: str,
    payload: WorkerIdentity,
    request: Request,
) -> dict[str, object]:
    try:
        state = _store(request).cancel_from_worker(job_id, payload.worker_id)
    except (MarketDataStoreError, OSError) as exc:
        raise _store_unavailable() from exc
    except ValueError as exc:
        raise _conflict(exc) from exc
    return {"data": {"state": state, "cancel_requested": state["cancel_requested"]}}


__all__ = ["router"]
