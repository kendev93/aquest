"""Job polling and cancellation for market-data tasks.

注册顺序有讲究：``/jobs/active`` 必须排在 ``/jobs/{job_id}`` 前面，否则
``active`` 会被当成 job_id 命中后面那条路径。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from aquest.market_data.auto_sync import (
    ACTIVE_DATA_JOB_DESCRIPTIONS,
    MARKET_SYNC_JOB_DESCRIPTIONS,
)

from ._shared import _runner

router = APIRouter(tags=["market-data"])


@router.get("/jobs/active")
def active_market_data_jobs(request: Request) -> dict[str, object]:
    return {"data": _runner(request).active_snapshots(MARKET_SYNC_JOB_DESCRIPTIONS)}


@router.post("/jobs/{job_id}/cancel")
def cancel_market_data_job(job_id: str, request: Request) -> dict[str, object]:
    state = _runner(request).get(job_id)
    if state is None or state.description not in MARKET_SYNC_JOB_DESCRIPTIONS:
        raise HTTPException(status_code=404, detail="行情同步任务不存在或已过期")
    if not _runner(request).request_cancel(job_id):
        return {
            "data": {
                "job_id": job_id,
                "status": state.status,
                "cancel_requested": False,
            }
        }
    return {"data": {"job_id": job_id, "status": "cancelling", "cancel_requested": True}}


@router.get("/jobs/{job_id}")
def market_data_job_status(job_id: str, request: Request) -> dict[str, object]:
    state = _runner(request).get(job_id)
    if state is None or state.description not in ACTIVE_DATA_JOB_DESCRIPTIONS:
        raise HTTPException(status_code=404, detail="行情任务不存在或已过期")
    return {"data": state.snapshot()}
