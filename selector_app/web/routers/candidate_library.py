"""候选库：把挖出来的候选条件存下来、表态、显式发布。

发布端点刻意只有一条动作且只写一个策略。这里没有任何定时、自动或"顺手"的上线
路径——上线日会冻结研究段基线，那件事只能由人在策略追踪页做。
"""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, HTTPException, Request, status

from selector_app.candidate_library.models import STATUS_LABELS
from selector_app.candidate_library.service import (
    CandidateLibraryError,
    CandidateLibraryService,
    CandidateNotFoundError,
)
from selector_app.market_data.store import MarketDataStoreError

from ..schemas import (
    CandidateImportRequest,
    CandidatePublishRequest,
    CandidateStatusRequest,
)

router = APIRouter(prefix="/candidate-library", tags=["candidate-library"])

_UNAVAILABLE = "候选库暂不可用"


def _service(request: Request) -> CandidateLibraryService:
    return cast(CandidateLibraryService, request.app.state.candidate_library_service)


@router.get("")
def list_candidates(request: Request, status: str | None = None) -> dict[str, object]:
    try:
        items = _service(request).list_candidates(status)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail=_UNAVAILABLE) from exc
    return {"data": {"items": items, "status_labels": STATUS_LABELS}}


@router.post("/import", status_code=status.HTTP_201_CREATED)
def import_candidates(payload: CandidateImportRequest, request: Request) -> dict[str, object]:
    try:
        outcome = _service(request).import_candidates(
            history_id=payload.history_id,
            candidate_ids=payload.candidate_ids,
            note=payload.note,
        )
    except CandidateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CandidateLibraryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail=_UNAVAILABLE) from exc
    return {"data": outcome}


@router.get("/{candidate_id}")
def read_candidate(candidate_id: str, request: Request) -> dict[str, object]:
    try:
        record = _service(request).read_candidate(candidate_id)
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail=_UNAVAILABLE) from exc
    if record is None:
        raise HTTPException(status_code=404, detail="候选不存在")
    return {"data": record}


@router.patch("/{candidate_id}")
def update_candidate(
    candidate_id: str,
    payload: CandidateStatusRequest,
    request: Request,
) -> dict[str, object]:
    try:
        record = _service(request).set_status(
            candidate_id, status=payload.status, note=payload.note
        )
    except CandidateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CandidateLibraryError as exc:
        # 状态流转被拒是对当下状态的判断，不是请求体写错了，所以给 409。
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail=_UNAVAILABLE) from exc
    return {"data": record}


@router.delete("/{candidate_id}")
def delete_candidate(candidate_id: str, request: Request) -> dict[str, object]:
    try:
        deleted = _service(request).delete_candidate(candidate_id)
    except CandidateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CandidateLibraryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (MarketDataStoreError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=_UNAVAILABLE) from exc
    return {"data": {"deleted": deleted}}


@router.get("/{candidate_id}/publish-preview")
def publish_preview(candidate_id: str, request: Request) -> dict[str, object]:
    """把「发布将写入什么」先摊开给人看：配置、说明、以及拦着的事。"""

    try:
        preview = _service(request).publish_preview(candidate_id)
    except CandidateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CandidateLibraryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail=_UNAVAILABLE) from exc
    return {"data": preview}


@router.post("/{candidate_id}/publish", status_code=status.HTTP_201_CREATED)
def publish_candidate(
    candidate_id: str,
    payload: CandidatePublishRequest,
    request: Request,
) -> dict[str, object]:
    try:
        outcome = _service(request).publish(
            candidate_id,
            name=payload.name,
            note=payload.note,
            overrides={
                "ranking_value": payload.ranking_value,
                "rank_order": payload.rank_order,
                "max_positions": payload.max_positions,
                "initial_cash": payload.initial_cash,
                "stop_loss_pct": payload.stop_loss_pct,
                "take_profit_pct": payload.take_profit_pct,
            },
        )
    except CandidateNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CandidateLibraryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail=_UNAVAILABLE) from exc
    return {"data": outcome}
