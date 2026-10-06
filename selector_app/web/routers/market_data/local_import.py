"""Local TongDaXin vipdoc import endpoints."""

from __future__ import annotations

import duckdb
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse

from selector_app.market_data.auto_sync import MARKET_SYNC_EXCLUSIVE_GROUP
from selector_app.market_data.local_adapter import detect_vipdoc_roots, host_mount_prefix
from selector_app.market_data.local_import import (
    LocalDayImportCancelled,
    LocalDayImportError,
)
from selector_app.market_data.store import MarketDataStoreError
from selector_app.screening.jobs import (
    ActiveJobConflictError,
    ProgressReporter,
    TaskCancelledError,
    TaskUserError,
    report_progress_detail,
)

from ...schemas import LocalImportRequest
from ._shared import (
    _local_import_service,
    _reject_duplicate_market_job,
    _resolved_vipdoc_path,
    _runner,
    _storage_error_message,
)
from .precompute import _with_indicator_precompute

router = APIRouter(tags=["market-data"])

_VIPDOC_MOUNT_HINT = (
    "后端运行在 Docker 容器里，只能看到已挂载进容器的路径；"
    "直接填 mac/Windows 上的目录是找不到的。"
    "把宿主机的 vipdoc 挂到 /data/vipdoc，或把它的上级目录挂到 /host，再填容器内路径。"
)


@router.get("/local-import/detect")
def detect_local_vipdoc() -> dict[str, object]:
    """Report vipdoc trees the backend can actually see, for the import form."""

    candidates = detect_vipdoc_roots()
    return {
        "data": {
            "candidates": [
                {
                    "path": item.path,
                    "file_count": item.file_count,
                    "markets": [market.upper() for market in item.markets],
                }
                for item in candidates
            ],
            "default_path": _resolved_vipdoc_path(None),
            "mount_hint": _VIPDOC_MOUNT_HINT,
            "host_mount_prefix": host_mount_prefix() or None,
        }
    }


@router.post("/local-import", status_code=status.HTTP_202_ACCEPTED)
def create_local_import_job(payload: LocalImportRequest, request: Request) -> JSONResponse:
    """Import a local TongDaXin vipdoc tree, overwriting bars on the same date."""

    runner = _runner(request)
    _reject_duplicate_market_job(runner)
    service = _local_import_service(request)
    vipdoc_path = _resolved_vipdoc_path(payload.vipdoc_path)
    job_id_holder = [""]

    def abort_check() -> bool:
        return bool(job_id_holder[0]) and runner.is_cancel_requested(job_id_holder[0])

    def run_import(progress: ProgressReporter) -> dict[str, object]:
        try:
            result = service.import_vipdoc(
                vipdoc_path,
                universe=payload.universe,
                instrument_types=payload.instrument_types,
                boards=payload.boards,
                progress_callback=progress,
                detail_callback=lambda detail: report_progress_detail(progress, detail),
                abort_check=abort_check,
            )
        except LocalDayImportCancelled as exc:
            raise TaskCancelledError(str(exc)) from exc
        except LocalDayImportError as exc:
            raise TaskUserError(str(exc)) from exc
        except (MarketDataStoreError, duckdb.Error) as exc:
            raise TaskUserError(_storage_error_message(exc)) from exc
        except OSError as exc:
            raise TaskUserError(f"读取本地行情文件失败: {exc}") from exc
        # 导入只影响一部分标的，缓存也只补算那些缺失或过期的。
        return _with_indicator_precompute(
            request,
            result,
            vipdoc_path=vipdoc_path,
            universe=payload.universe,
            instrument_types=payload.instrument_types,
            boards=payload.boards,
            progress_callback=progress,
            abort_check=abort_check,
            only_changed=True,
        )

    try:
        job_id = runner.submit_callable(
            run_import,
            description="本地行情导入",
            exclusive_group=MARKET_SYNC_EXCLUSIVE_GROUP,
        )
        job_id_holder[0] = job_id
    except ActiveJobConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="任务执行器暂不可用") from exc
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"data": {"job_id": job_id, "status": "queued"}},
    )
