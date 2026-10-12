"""Indicator cache inspection and invalidation endpoints."""

from __future__ import annotations

import duckdb
from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from aquest.market_data.store import MarketDataStoreError

from ._shared import _indicator_cache, _storage_error_message, _store

router = APIRouter(tags=["market-data"])


@router.get("/indicator-cache", response_model=None)
def indicator_cache_status(request: Request) -> dict[str, object] | JSONResponse:
    try:
        return {"data": _indicator_cache(request).status()}
    except (OSError, MarketDataStoreError, duckdb.Error) as exc:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": {
                    "code": "indicator_cache_error",
                    "message": _storage_error_message(exc),
                }
            },
        )


@router.delete("/indicator-cache", response_model=None)
def clear_indicator_cache(request: Request) -> dict[str, object] | JSONResponse:
    """Drop every persisted derived result so the next request recomputes it.

    Needed after the cache key semantics change: rows written under the old
    scheme can never match again and would only occupy store space.
    """

    try:
        _indicator_cache(request).clear_memory()
        removed = _store(request).clear_indicator_caches()
    except (OSError, MarketDataStoreError, duckdb.Error) as exc:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": {
                    "code": "indicator_cache_error",
                    "message": _storage_error_message(exc),
                }
            },
        )
    return {
        "data": {
            **removed,
            "total_entries": int(removed["builtin_entries"]) + int(removed["custom_entries"]),
        }
    }
