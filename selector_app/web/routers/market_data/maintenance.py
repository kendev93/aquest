"""Maintenance endpoint for undoing a bad import.

本地行永远压过在线行，所以一次导错的 ``.day`` 只能靠这里删掉，否则只能重建库。

同一个位置也放"把漏记的除权事件补齐"—— 它同样是对既有数据的手工修正，同样需要
能先体检再决定是否落库。
"""

from __future__ import annotations

import duckdb
from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from selector_app.market_data.gap_backfill import backfill_inferred_actions
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


@router.post("/corporate-actions/backfill", response_model=None)
def backfill_corporate_actions(
    request: Request,
    write: bool = False,
) -> dict[str, object] | JSONResponse:
    """从行情自身的除权跳空反推，补齐分红送配接口漏记的事件。

    ``write`` 默认 **false**，也就是只体检不落库。补进来的因子是估算的（用除权日
    开盘价反推），所以先看清"能补出多少、哪些补不了"再决定要不要写。真正落库时
    返回的明细里会标出每条用了哪个前收盘价和开盘价，事后可逐条核对。

    补齐后会触发指标缓存失效 —— 事件表内容哈希变了，因子序列全变，旧缓存必须重算。
    """

    try:
        report = backfill_inferred_actions(_store(request), write=write)
    except (MarketDataStoreError, ValueError, duckdb.Error, OSError, RuntimeError) as exc:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": {
                    "code": "corporate_action_backfill_error",
                    "message": _storage_error_message(exc),
                }
            },
        )
    if report.written:
        _invalidate_indicator_cache(request)
    return {"data": report.to_dict()}
