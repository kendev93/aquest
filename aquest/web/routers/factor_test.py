"""Cross-sectional factor test endpoints."""

from __future__ import annotations

from fastapi import Request, status
from fastapi.responses import JSONResponse

from ..date_ranges import resolve_date_range
from ..market_source import resolve_market_status
from ..research_router import build_research_router
from ..schemas import FactorTestRequest

router, _submit_research = build_research_router(
    prefix="/factor-test",
    tag="factor-test",
    job_description="因子检验",
    label="因子检验",
    history_type="factor_test",
    service_attr="factor_test_service",
    prepare_payload=lambda payload, request: _resolve_payload_dates(payload, request),
)


@router.post("", status_code=status.HTTP_202_ACCEPTED, name="factor-test_submit")
def create_factor_test(payload: FactorTestRequest, request: Request) -> JSONResponse:
    return _submit_research(request, payload)


def _resolve_payload_dates(payload: FactorTestRequest, request: Request) -> FactorTestRequest:
    """Fill in a preset range so the stored request is self-describing."""

    if payload.start_date is not None and payload.end_date is not None:
        return payload
    status_snapshot = resolve_market_status(request)
    start_date, end_date = resolve_date_range(
        mode="recent5",
        requested_start=payload.start_date,
        requested_end=payload.end_date,
        available_start=status_snapshot.data_start,
        available_end=status_snapshot.data_end,
    )
    return payload.model_copy(update={"start_date": start_date, "end_date": end_date})
