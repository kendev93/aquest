"""Cross-type comparison endpoints for archived research runs.

历史是按类型分开存的、也是按类型分开看的，但研究本身是「反复做实验 + 比较
实验」——这两个端点把散在各处的记录拉到同一张表里，并且负责把「它们不是同一
件事」说清楚。
"""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, HTTPException, Request

from aquest.history_compare import HistoryCompareService

from ..schemas import HistoryCompareRequest
from ._errors import store_guard

router = APIRouter(prefix="/history-compare", tags=["history-compare"])


def _service(request: Request) -> HistoryCompareService:
    return cast(HistoryCompareService, request.app.state.history_compare_service)


@router.get("/candidates")
def history_compare_candidates(request: Request) -> dict[str, object]:
    with store_guard("历史记录暂不可用", include_runtime=False):
        return {"data": _service(request).candidates()}


@router.post("")
def create_history_compare(payload: HistoryCompareRequest, request: Request) -> dict[str, object]:
    try:
        with store_guard("历史记录暂不可用", include_runtime=False):
            report = _service(request).compare(
                payload.history_ids,
                normalize=payload.normalize,
                x_axis=payload.x_axis,
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"data": report.to_dict()}
