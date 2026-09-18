"""Swing research endpoints.

Two steps, deliberately kept apart. ``/overview`` cuts the market and reports
distributions so a threshold can be picked with data rather than taste;
``/indicator-study`` then scores every dimensionless technical output against
the labels that segmentation implies.

Both are synchronous for now, which is fine for a calibration run or a bounded
universe but will sit in front of a job runner before the UI gets a button:
a full-market study is minutes of work, not a request.
"""

from __future__ import annotations

from typing import Protocol, cast

from fastapi import APIRouter, HTTPException, Request

from selector_app.swing_research.models import SwingIndicatorConfig, SwingOverviewConfig
from selector_app.swing_research.service import ProgressCallback

from ..schemas import SwingIndicatorRequest, SwingOverviewRequest

router = APIRouter(prefix="/swing-research", tags=["swing-research"])


class SwingResearchServiceLike(Protocol):
    def overview(
        self,
        config: SwingOverviewConfig,
        progress_callback: ProgressCallback = None,
    ) -> dict[str, object]: ...

    def indicator_study(
        self,
        config: SwingIndicatorConfig,
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


@router.post("/indicator-study")
def swing_indicator_study(
    payload: SwingIndicatorRequest, request: Request
) -> dict[str, object]:
    try:
        result = _service(request).indicator_study(payload.to_config())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"data": result}
