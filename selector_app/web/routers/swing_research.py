"""Swing segmentation endpoints.

The only endpoint here is the overview, and it is deliberately synchronous:
it cuts the market and reports distributions so a threshold can be chosen with
data. The heavy indicator study that reuses this segmentation will run as a
job; calibration should feel instant enough to iterate on.
"""

from __future__ import annotations

from typing import Protocol, cast

from fastapi import APIRouter, HTTPException, Request

from selector_app.swing_research.models import SwingOverviewConfig
from selector_app.swing_research.service import ProgressCallback

from ..schemas import SwingOverviewRequest

router = APIRouter(prefix="/swing-research", tags=["swing-research"])


class SwingResearchServiceLike(Protocol):
    def overview(
        self,
        config: SwingOverviewConfig,
        progress_callback: ProgressCallback = None,
    ) -> dict[str, object]: ...


def _service(request: Request) -> SwingResearchServiceLike:
    return cast(SwingResearchServiceLike, request.app.state.swing_research_service)


@router.post("/overview")
def swing_overview(payload: SwingOverviewRequest, request: Request) -> dict[str, object]:
    try:
        result = _service(request).overview(payload.to_config())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"data": result}
