"""Read-only endpoints describing what the backend can do and what it holds.

``/store`` 是前端各页判断「有没有行情」的入口，所以它对行情库异常要降级成
503 文案而不是抛栈。
"""

from __future__ import annotations

import json

import duckdb
from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from aquest.market_data.store import MarketDataStoreError

from ._shared import _market_status, _runner, _service, _storage_error_message

router = APIRouter(tags=["market-data"])


def _last_market_sync_summary(
    writer: object | None,
) -> dict[str, object] | None:
    """Read the last sync summary off a writer, when one is supplied.

    Kept tolerant of the historical writer object so callers that no longer have
    one can pass ``None`` without special-casing.
    """

    reader = getattr(writer, "get_system_setting", None)
    if not callable(reader):
        return None
    try:
        raw = reader("market_sync_last_summary")
        value = json.loads(raw) if raw else None
    except (OSError, RuntimeError, ValueError, duckdb.Error):
        return None
    return dict(value) if isinstance(value, dict) else None


@router.get("/capabilities", response_model=None)
def market_data_capabilities(request: Request) -> dict[str, object]:
    service = _service(request)
    method = getattr(service, "provider_capabilities", None)
    if callable(method):
        capabilities = method()
        if isinstance(capabilities, dict):
            return {"data": capabilities}
    return {
        "data": {
            "default_provider": "eastmoney",
            "instrument_count": 0,
            "providers": {},
        }
    }


@router.get("/store", response_model=None)
def market_data_store_status(request: Request) -> dict[str, object] | JSONResponse:
    try:
        data = _market_status(request).to_dict()
        data["market_data_source"] = "aquest"
        data["market_data_read_only"] = False
        data["market_data_write_target"] = "aquest"
        data["full_market_sync_supported"] = False
        data["market_sync_lock"] = None
        data["last_market_sync_summary"] = _last_market_sync_summary(None)
        auto_sync_job_id = getattr(request.app.state, "auto_sync_job_id", None)
        if auto_sync_job_id:
            auto_sync_state = _runner(request).get(auto_sync_job_id)
            if auto_sync_state is None or auto_sync_state.status in {
                "completed",
                "failed",
                "cancelled",
            }:
                auto_sync_job_id = None
        data["auto_sync_job_id"] = auto_sync_job_id
        data["auto_sync_enabled"] = bool(getattr(request.app.state, "auto_sync_enabled", False))
        data["auto_sync_interval_seconds"] = getattr(
            request.app.state,
            "auto_sync_interval_seconds",
            None,
        )
        return {"data": data}
    except (MarketDataStoreError, duckdb.Error, OSError, RuntimeError) as exc:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": {
                    "code": "market_data_store_error",
                    "message": _storage_error_message(exc),
                }
            },
        )
