"""筛选方案：保存、加载、另存为、重命名、删除。"""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, HTTPException, Request, status

from selector_app.market_data.store import MarketDataStoreError
from selector_app.screen_presets.service import ScreenPresetService

from ..schemas import ScreenPresetCreateRequest, ScreenPresetUpdateRequest

router = APIRouter(prefix="/screen-presets", tags=["screen-presets"])


def _service(request: Request) -> ScreenPresetService:
    return cast(ScreenPresetService, request.app.state.screen_preset_service)


@router.get("")
def list_screen_presets(request: Request) -> dict[str, object]:
    try:
        return {"data": _service(request).list_presets()}
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="筛选方案暂不可用") from exc


@router.post("", status_code=status.HTTP_201_CREATED)
def create_screen_preset(payload: ScreenPresetCreateRequest, request: Request) -> dict[str, object]:
    try:
        record = _service(request).create_preset(payload.name, payload.payload, note=payload.note)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="筛选方案暂不可用") from exc
    return {"data": record}


@router.get("/{preset_id}")
def read_screen_preset(preset_id: str, request: Request) -> dict[str, object]:
    try:
        record = _service(request).read_preset(preset_id)
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="筛选方案暂不可用") from exc
    if record is None:
        raise HTTPException(status_code=404, detail="方案不存在")
    return {"data": record}


@router.patch("/{preset_id}")
def update_screen_preset(
    preset_id: str,
    payload: ScreenPresetUpdateRequest,
    request: Request,
) -> dict[str, object]:
    try:
        record = _service(request).update_preset(
            preset_id, name=payload.name, note=payload.note, payload=payload.payload
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="筛选方案暂不可用") from exc
    if record is None:
        raise HTTPException(status_code=404, detail="方案不存在")
    return {"data": record}


@router.delete("/{preset_id}")
def delete_screen_preset(preset_id: str, request: Request) -> dict[str, object]:
    try:
        deleted = _service(request).delete_preset(preset_id)
    except (MarketDataStoreError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="筛选方案暂不可用") from exc
    return {"data": {"deleted": deleted}}
