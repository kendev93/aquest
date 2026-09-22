"""Read-only endpoints over a local vipdoc tree.

这些接口不碰行情库，直接读宿主机挂载进来的 .day 文件，用于导入前的预览。
"""

from __future__ import annotations

from datetime import date
from typing import Literal

import duckdb
from fastapi import APIRouter, Query, Request, status
from fastapi import Path as PathParameter
from fastapi.responses import JSONResponse

from selector_app.market_data.service import ChartPeriod
from selector_app.market_data.store import MarketDataStoreError

from ._shared import _local_service, _resolved_vipdoc_path

router = APIRouter(tags=["market-data"])


@router.get("/local/instruments", response_model=None)
def local_instruments(
    request: Request,
    market: str = Query(default="all", pattern=r"^(all|SH|SZ)$"),
    keyword: str = Query(default="", max_length=64),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> dict[str, object] | JSONResponse:
    try:
        result = _local_service(request).list_instruments(
            _resolved_vipdoc_path(None),
            market=market,
            keyword=keyword,
            page=page,
            page_size=page_size,
        )
    except (OSError, ValueError, MarketDataStoreError, duckdb.Error) as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"error": {"code": "local_data_error", "message": str(exc)}},
        )
    payload = result.to_dict()
    return {
        "data": payload["items"],
        "meta": {
            "total": payload["total"],
            "page": payload["page"],
            "page_size": payload["page_size"],
            "pages": payload["pages"],
        },
    }


@router.get("/local/{market}/{code}/bars", response_model=None)
def local_market_bars(
    request: Request,
    market: Literal["SH", "SZ"],
    code: str = PathParameter(pattern=r"^\d{6}$"),
    period: ChartPeriod = Query(default="daily"),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    formula_ids: str | None = Query(default=None, max_length=256),
) -> dict[str, object] | JSONResponse:
    requested_formula_ids = tuple(
        item.strip() for item in (formula_ids or "").split(",") if item.strip()
    )
    try:
        if requested_formula_ids:
            result = _local_service(request).get_chart(
                _resolved_vipdoc_path(None),
                market,
                code,
                period=period,
                start_date=start_date,
                end_date=end_date,
                formula_ids=requested_formula_ids,
            )
        else:
            result = _local_service(request).get_chart(
                _resolved_vipdoc_path(None),
                market,
                code,
                period=period,
                start_date=start_date,
                end_date=end_date,
            )
    except (OSError, ValueError, MarketDataStoreError, duckdb.Error) as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"error": {"code": "local_data_error", "message": str(exc)}},
        )
    return {"data": result.to_dict()}
