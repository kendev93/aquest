"""研究方案：买卖条件研究页整套输入的保存 / 加载 / 另存为 / 重命名 / 删除。

payload 的形状就是跑研究时发的那个请求，所以这里直接用 ``ConditionResearchRequest``
校验：能存下来的方案一定也能原样提交去跑。非法一律 422，不做裁剪——研究配置里
的字段互相咬合，悄悄改一个会把整套规则的语义改掉。
"""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, HTTPException, Request, status

from selector_app.market_data.store import MarketDataStoreError
from selector_app.research_presets.service import ResearchPresetService

from ..schemas import ResearchPresetCreateRequest, ResearchPresetUpdateRequest

router = APIRouter(prefix="/research-presets", tags=["research-presets"])


def _service(request: Request) -> ResearchPresetService:
    return cast(ResearchPresetService, request.app.state.research_preset_service)


@router.get("")
def list_research_presets(request: Request) -> dict[str, object]:
    try:
        return {"data": _service(request).list_presets()}
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="研究方案暂不可用") from exc


@router.post("", status_code=status.HTTP_201_CREATED)
def create_research_preset(
    payload: ResearchPresetCreateRequest,
    request: Request,
) -> dict[str, object]:
    try:
        record = _service(request).create_preset(
            payload.name,
            payload.payload.model_dump(mode="json"),
            note=payload.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="研究方案暂不可用") from exc
    return {"data": record}


@router.get("/{preset_id}")
def read_research_preset(preset_id: str, request: Request) -> dict[str, object]:
    try:
        record = _service(request).read_preset(preset_id)
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="研究方案暂不可用") from exc
    if record is None:
        raise HTTPException(status_code=404, detail="研究方案不存在")
    return {"data": record}


@router.patch("/{preset_id}")
def update_research_preset(
    preset_id: str,
    payload: ResearchPresetUpdateRequest,
    request: Request,
) -> dict[str, object]:
    try:
        record = _service(request).update_preset(
            preset_id,
            name=payload.name,
            note=payload.note,
            payload=payload.payload.model_dump(mode="json") if payload.payload else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="研究方案暂不可用") from exc
    if record is None:
        raise HTTPException(status_code=404, detail="研究方案不存在")
    return {"data": record}


@router.delete("/{preset_id}")
def delete_research_preset(preset_id: str, request: Request) -> dict[str, object]:
    try:
        deleted = _service(request).delete_preset(preset_id)
    except (MarketDataStoreError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="研究方案暂不可用") from exc
    return {"data": {"deleted": deleted}}


__all__ = ["router"]
