"""Trade attribution endpoints: why did these trades win and those lose?"""

from __future__ import annotations

from fastapi import Request, status
from fastapi.responses import JSONResponse

from aquest.factor_attribution.service import TRADABLE_TYPES

from ..research_router import build_research_router
from ..schemas import FactorAttributionRequest

router, _submit_research = build_research_router(
    prefix="/factor-attribution",
    tag="factor-attribution",
    job_description="因子归因",
    label="因子归因",
    history_type="factor_attribution",
    service_attr="factor_attribution_service",
    source_types=TRADABLE_TYPES,
)


@router.post("", status_code=status.HTTP_202_ACCEPTED, name="factor-attribution_submit")
def create_attribution(payload: FactorAttributionRequest, request: Request) -> JSONResponse:
    return _submit_research(request, payload)
