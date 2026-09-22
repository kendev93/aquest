"""股票组：存一批标的，并观察它们入组之后的表现。

只有 ``performance`` 是重活（最坏要扫全市场五年的收盘价，实测 3 秒出头），其余
都是单条记录的读写。它没走后台任务，因为同步返回的话用户能看到明确的加载态，
而走任务要多一次轮询、还要处理「任务没跑完又点了一次」——对一次点击只画两条
曲线这件事来说不划算。
"""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, HTTPException, Request, status

from selector_app.market_data.store import MarketDataStoreError
from selector_app.stock_groups.service import StockGroupService

from ..schemas import (
    StockGroupCreateRequest,
    StockGroupRemoveMembersRequest,
    StockGroupUpdateRequest,
)

router = APIRouter(prefix="/stock-groups", tags=["stock-groups"])


def _service(request: Request) -> StockGroupService:
    return cast(StockGroupService, request.app.state.stock_group_service)


@router.get("")
def list_stock_groups(request: Request) -> dict[str, object]:
    try:
        return {"data": _service(request).list_groups()}
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="股票组暂不可用") from exc


@router.post("", status_code=status.HTTP_201_CREATED)
def create_stock_group(payload: StockGroupCreateRequest, request: Request) -> dict[str, object]:
    try:
        record = _service(request).create_group(
            name=payload.name,
            items=[item.model_dump() for item in payload.items],
            note=payload.note,
            source=payload.source,
            source_history_id=payload.source_history_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="股票组暂不可用") from exc
    return {"data": record}


@router.get("/{group_id}")
def read_stock_group(group_id: str, request: Request) -> dict[str, object]:
    record = _read_or_404(request, group_id)
    return {"data": record}


@router.patch("/{group_id}")
def update_stock_group(
    group_id: str,
    payload: StockGroupUpdateRequest,
    request: Request,
) -> dict[str, object]:
    try:
        record = _service(request).update_group(
            group_id,
            name=payload.name,
            note=payload.note,
            items=None if payload.items is None else [item.model_dump() for item in payload.items],
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="股票组暂不可用") from exc
    if record is None:
        raise HTTPException(status_code=404, detail="分组不存在")
    return {"data": record}


@router.post("/{group_id}/members/remove")
def remove_stock_group_members(
    group_id: str,
    payload: StockGroupRemoveMembersRequest,
    request: Request,
) -> dict[str, object]:
    try:
        record = _service(request).remove_members(
            group_id, [(item.market, item.code) for item in payload.members]
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="股票组暂不可用") from exc
    if record is None:
        raise HTTPException(status_code=404, detail="分组不存在")
    return {"data": record}


@router.delete("/{group_id}")
def delete_stock_group(group_id: str, request: Request) -> dict[str, object]:
    try:
        deleted = _service(request).delete_group(group_id)
    except (MarketDataStoreError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="股票组暂不可用") from exc
    return {"data": {"deleted": deleted}}


@router.get("/{group_id}/performance")
def read_stock_group_performance(group_id: str, request: Request) -> dict[str, object]:
    """入组至今的等权买入持有曲线，外加同一区间的全市场等权对照。"""

    try:
        result = _service(request).performance(group_id)
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="股票组暂不可用") from exc
    if result is None:
        raise HTTPException(status_code=404, detail="分组不存在")
    return {"data": result.to_dict()}


def _read_or_404(request: Request, group_id: str) -> dict[str, object]:
    try:
        record = _service(request).read_group(group_id)
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="股票组暂不可用") from exc
    if record is None:
        raise HTTPException(status_code=404, detail="分组不存在")
    return record


__all__ = ["router"]
