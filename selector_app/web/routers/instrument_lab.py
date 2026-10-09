"""Single-instrument workbench endpoints.

三件事：指标读数、单标的回测、把通过置换检验的条件重新放到这只票上检验。这里
没有任何「评分」接口——评分不可证伪，而检验台的价值恰恰在于把可核对的读数摆在
同一页上。
"""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, HTTPException, Query, Request

from selector_app.instrument_lab import InstrumentLabService
from selector_app.market_data.store import MarketDataStoreError

from ..schemas import (
    BacktestRequest,
    InstrumentLabCheckRequest,
    InstrumentLabValuesRequest,
)

router = APIRouter(prefix="/instrument-lab", tags=["instrument-lab"])


def _service(request: Request) -> InstrumentLabService:
    return cast(InstrumentLabService, request.app.state.instrument_lab_service)


@router.post("/values")
def instrument_values(payload: InstrumentLabValuesRequest, request: Request) -> dict[str, object]:
    try:
        values = _service(request).values(
            market=payload.market,
            code=payload.code,
            vipdoc_path=payload.vipdoc_path,
            value_ids=payload.value_ids,
            start_date=payload.start_date,
            end_date=payload.end_date,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="行情数据暂不可用") from exc
    return {"data": values.to_dict()}


@router.get("/conditions/library")
def instrument_condition_library(
    request: Request, limit: int = Query(default=60, ge=1, le=500)
) -> dict[str, object]:
    try:
        items = _service(request).condition_library(limit=limit)
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="历史记录暂不可用") from exc
    return {"data": {"items": [item.to_dict() for item in items]}}


@router.post("/conditions/check")
def instrument_condition_check(
    payload: InstrumentLabCheckRequest, request: Request
) -> dict[str, object]:
    conditions = [
        {
            "left_value": item.left_value,
            "operator": item.operator,
            "right_value": item.right_value,
            "right_constant": item.right_constant,
        }
        for item in payload.conditions
    ]
    try:
        report = _service(request).check_conditions(
            market=payload.market,
            code=payload.code,
            vipdoc_path=payload.vipdoc_path,
            conditions=conditions,
            horizon=payload.horizon,
            start_date=payload.start_date,
            end_date=payload.end_date,
            permutations=payload.permutations,
            block_size=payload.block_size,
            alpha=payload.alpha,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="行情数据暂不可用") from exc
    return {"data": report.to_dict()}


@router.post("/backtest")
def instrument_backtest(payload: BacktestRequest, request: Request) -> dict[str, object]:
    try:
        report = _service(request).backtest(payload.to_config())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="行情数据暂不可用") from exc
    return {"data": report.to_dict()}
