"""Maintenance endpoint for undoing a bad import.

本地行永远压过在线行，所以一次导错的 ``.day`` 只能靠这里删掉，否则只能重建库。
"""

from __future__ import annotations

import duckdb
from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from selector_app.market_data.store import MarketDataStoreError

from ...schemas import BarDeleteRequest
from ._shared import _invalidate_indicator_cache, _storage_error_message, _store

router = APIRouter(tags=["market-data"])


@router.post("/bars/delete", response_model=None)
def delete_market_bars(
    payload: BarDeleteRequest,
    request: Request,
) -> dict[str, object] | JSONResponse:
    """Drop stored bars so a wrong import can be undone.

    Local rows outrank online rows forever, so without this a single bad
    ``.day`` import can only be fixed by rebuilding the database.
    """

    try:
        removed = _store(request).delete_bars(
            source=payload.source,
            markets=payload.markets,
            codes=payload.codes,
            start_date=payload.start_date,
            end_date=payload.end_date,
        )
    except (MarketDataStoreError, duckdb.Error, OSError, RuntimeError) as exc:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": {"code": "market_data_store_error", "message": _storage_error_message(exc)}
            },
        )
    # 删掉的行情可能正躺在指标缓存里，版本号副本不清掉下一次查询还会命中它。
    _invalidate_indicator_cache(request)
    return {"data": {"deleted_bars": removed, "source": payload.source}}
