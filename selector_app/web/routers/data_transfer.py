"""Backup and restore of the records a user cannot regenerate.

Bars and indicator caches can always be re-synced, so they stay out of the
archive.  What travels is the research history: backtests, condition research,
strategy discovery, and screening.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import cast

from fastapi import APIRouter, File, Request, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

from selector_app.market_data.store import DuckDbMarketDataStore, MarketDataStoreError
from selector_app.market_data.store_history import ASSET_TABLES
from selector_app.market_data.transfer import (
    default_export_name,
    export_user_assets,
    import_user_assets,
    read_manifest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/data-transfer", tags=["data-transfer"])

_MEDIA_TYPE = "application/zip"
_MAX_UPLOAD_BYTES = 1024 * 1024 * 1024
_CHUNK_BYTES = 1024 * 1024


def _store(request: Request) -> DuckDbMarketDataStore:
    return cast(DuckDbMarketDataStore, request.app.state.market_data_store)


def _message(exc: Exception) -> str:
    return str(exc) or "数据管理操作失败"


def _error(code: str, message: str, http_status: int) -> JSONResponse:
    return JSONResponse(
        status_code=http_status,
        content={"error": {"code": code, "message": message}},
    )


@router.get("/manifest", response_model=None)
def data_transfer_manifest(request: Request) -> dict[str, object] | JSONResponse:
    """Describe what an export would contain, without producing one."""

    store = _store(request)
    try:
        counts = {table: sum(1 for _ in _rows(store, table)) for table in ASSET_TABLES}
    except (MarketDataStoreError, OSError, RuntimeError) as exc:
        return _error("data_manifest_failed", _message(exc), status.HTTP_503_SERVICE_UNAVAILABLE)
    return {"data": {"counts": counts, "total": sum(counts.values())}}


def _rows(store: DuckDbMarketDataStore, table: str) -> Iterator[dict[str, object]]:
    return store.iter_history_rows(table)


@router.get("/export", response_model=None)
def data_transfer_export(request: Request) -> FileResponse | JSONResponse:
    """Stream a zip archive of every user-asset table."""

    workspace = Path(tempfile.mkdtemp(prefix="indicator-lab-export-"))
    target = workspace / default_export_name()
    try:
        summary = export_user_assets(_store(request), target)
    except (MarketDataStoreError, OSError, RuntimeError) as exc:
        shutil.rmtree(workspace, ignore_errors=True)
        return _error("data_export_failed", _message(exc), status.HTTP_503_SERVICE_UNAVAILABLE)

    return FileResponse(
        summary.path,
        media_type=_MEDIA_TYPE,
        filename=summary.path.name,
        background=BackgroundTask(shutil.rmtree, workspace, ignore_errors=True),
    )


@router.post("/import", response_model=None)
async def data_transfer_import(
    request: Request,
    file: UploadFile = File(...),
) -> dict[str, object] | JSONResponse:
    """Merge an uploaded archive into this installation.

    Existing records are never overwritten or deleted; rows whose key already
    exists are reported as skipped, so re-importing is a no-op.
    """

    store = _store(request)
    workspace = Path(tempfile.mkdtemp(prefix="indicator-lab-import-"))
    staged = workspace / "upload.zip"
    try:
        written = 0
        with staged.open("wb") as handle:
            while chunk := await file.read(_CHUNK_BYTES):
                written += len(chunk)
                if written > _MAX_UPLOAD_BYTES:
                    return _error(
                        "data_import_too_large",
                        "备份文件过大，无法导入",
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    )
                handle.write(chunk)

        try:
            read_manifest(staged)
            summary = import_user_assets(store, staged)
        except MarketDataStoreError as exc:
            return _error(
                "data_import_rejected", _message(exc), status.HTTP_422_UNPROCESSABLE_ENTITY
            )
        except (OSError, RuntimeError) as exc:
            return _error("data_import_failed", _message(exc), status.HTTP_503_SERVICE_UNAVAILABLE)
    finally:
        await file.close()
        shutil.rmtree(workspace, ignore_errors=True)

    return {"data": summary.to_dict()}
