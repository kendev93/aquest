"""Cross-page discovery of long-running jobs."""

from __future__ import annotations

import logging
from typing import cast

from fastapi import APIRouter, Request

from selector_app.screening.jobs import ScreenJobRunner
from selector_app.strategy_discovery.job_backend import StrategyJobBackend

router = APIRouter(prefix="/jobs", tags=["jobs"])
logger = logging.getLogger(__name__)


def _runner(request: Request) -> ScreenJobRunner:
    return cast(ScreenJobRunner, request.app.state.screen_job_runner)


def _strategy_backend(request: Request) -> StrategyJobBackend | None:
    backend = getattr(request.app.state, "strategy_job_backend", None)
    return cast(StrategyJobBackend, backend) if backend is not None else None


@router.get("/active")
def active_jobs(request: Request) -> dict[str, object]:
    """Return active jobs from both the API process and strategy worker volume."""

    snapshots = _runner(request).active_snapshots()
    backend = _strategy_backend(request)
    if backend is not None:
        active_snapshot_method = getattr(backend, "active_snapshots", None)
        if not callable(active_snapshot_method):
            active_snapshot_method = None
        try:
            if active_snapshot_method is not None:
                snapshots.extend(active_snapshot_method())
        except (OSError, RuntimeError, ValueError):
            # A malformed strategy state must not hide regular backtest tasks.
            logger.exception("读取策略活动任务失败")

    unique: dict[str, dict[str, object]] = {}
    for snapshot in snapshots:
        job_id = snapshot.get("job_id")
        if isinstance(job_id, str) and job_id:
            unique[job_id] = dict(snapshot)
    ordered = sorted(
        unique.values(),
        key=lambda item: _created_at_sort_key(item.get("created_at")),
    )
    return {"data": ordered}


def _created_at_sort_key(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


__all__ = ["active_jobs", "router"]
