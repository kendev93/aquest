"""Independent executor for file-backed strategy discovery jobs."""

from __future__ import annotations

import logging
import signal
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from threading import Event
from typing import Literal, Protocol, cast

from selector_app.market_data.store import DuckDbMarketDataStore

from .job_backend import completion_state, decorate_result
from .job_store import StrategyJobStore, default_strategy_job_root
from .models import DiscoveryRequest
from .refinement import discovery_request_from_dict
from .replay import ReplayLeaderboard
from .replay_service import ReplayProgressCallback
from .resources import resolve_discovery_workers
from .service import ProgressCallback, StrategyDiscoveryService

logger = logging.getLogger(__name__)
DEFAULT_WORKER_POLL_INTERVAL_SECONDS = 0.5
DEFAULT_STALE_AFTER_SECONDS = 120.0


class StrategyDiscoveryServiceLike(Protocol):
    def run(
        self,
        request: DiscoveryRequest,
        *,
        progress_callback: ProgressCallback | None = None,
        abort_check: Callable[[], bool] | None = None,
    ) -> dict[str, object]: ...

    def refine(
        self,
        experiment: Mapping[str, object],
        parent_candidate_hash: str,
        *,
        max_trials: int | None = None,
        seed: int | None = None,
        radius: Literal["medium"] = "medium",
        progress_callback: ProgressCallback | None = None,
        abort_check: Callable[[], bool] | None = None,
    ) -> dict[str, object]: ...

    def replay(
        self,
        experiment: Mapping[str, object],
        leaderboard: ReplayLeaderboard,
        *,
        progress_callback: ReplayProgressCallback | None = None,
        abort_check: Callable[[], bool] | None = None,
    ) -> dict[str, object]: ...


class StrategyDiscoveryWorker:
    """Claim and execute at most one strategy job at a time."""

    def __init__(
        self,
        job_store: StrategyJobStore,
        service: StrategyDiscoveryServiceLike,
        *,
        service_factory: Callable[[int | None], StrategyDiscoveryServiceLike] | None = None,
        poll_interval_seconds: float = DEFAULT_WORKER_POLL_INTERVAL_SECONDS,
        stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("策略 worker 轮询间隔必须大于 0")
        if stale_after_seconds <= poll_interval_seconds:
            raise ValueError("策略 worker stale 时间必须大于轮询间隔")
        self._job_store = job_store
        self._service = service
        self._service_factory = service_factory
        self._poll_interval_seconds = poll_interval_seconds
        self._stale_after_seconds = stale_after_seconds

    def run_once(self) -> str | None:
        self._job_store.recover_stale_running(stale_after_seconds=self._stale_after_seconds)
        job_id = self._job_store.claim_next()
        if job_id is None:
            return None
        try:
            if self._job_store.is_cancel_requested(job_id):
                self._mark_cancelled(job_id)
                return job_id
            request = self._job_store.read_request(job_id)
            if request is None:
                raise ValueError("策略任务请求文件不存在")
            operation = request.get("operation")
            if operation not in {"discovery", "refinement", "replay"}:
                raise ValueError("策略任务类型无效")
            started_at = datetime.now(timezone.utc).isoformat()
            state = self._job_store.read_state(job_id) or {}
            raw_worker_count = state.get("worker_count")
            worker_count = (
                int(raw_worker_count)
                if isinstance(raw_worker_count, (int, float))
                and not isinstance(raw_worker_count, bool)
                else None
            )
            service = (
                self._service_factory(worker_count)
                if self._service_factory is not None
                else self._service
            )
            result = self._dispatch(job_id, request, service=service)
            normalized = decorate_result(
                job_id,
                operation,
                result,
                started_at=started_at,
            )
            completion = completion_state(operation, normalized)
            self._job_store.update(job_id, {"status": "running", **completion})
            self._job_store.write_result(
                job_id,
                normalized,
            )
        except Exception as exc:  # noqa: BLE001 - worker must keep serving later jobs
            if self._job_store.is_cancel_requested(job_id):
                self._mark_cancelled(job_id)
            else:
                logger.exception("策略 worker 执行任务失败: %s", job_id)
                self._job_store.update(
                    job_id,
                    status="failed",
                    phase="failed",
                    error=str(exc).strip()[:500] or "策略 worker 执行失败",
                    error_code=_error_code(exc),
                )
        return job_id

    def run_forever(self, *, stop_event: Event | None = None) -> None:
        while stop_event is None or not stop_event.is_set():
            if self.run_once() is None:
                time.sleep(self._poll_interval_seconds)

    def _dispatch(
        self,
        job_id: str,
        request: Mapping[str, object],
        *,
        service: StrategyDiscoveryServiceLike | None = None,
    ) -> dict[str, object]:
        selected_service = service or self._service
        operation = request.get("operation")
        payload = request.get("payload")
        if not isinstance(payload, Mapping):
            raise ValueError("策略任务 payload 格式无效")

        def abort_check() -> bool:
            return self._job_store.is_cancel_requested(job_id)

        def progress_callback(
            current: int,
            total: int,
            phase: str,
            metadata: Mapping[str, object] | None = None,
        ) -> None:
            details = dict(metadata or {})
            raw_progress = details.get("overall_progress")
            progress = (
                float(raw_progress)
                if isinstance(raw_progress, (int, float))
                else current / total
                if total > 0
                else 1.0
            )
            self._job_store.update(
                job_id,
                phase=phase,
                progress=progress,
                total_candidates=total,
                total_scanned=current,
                heartbeat_at=time.time(),
                metadata=details,
            )

        if operation == "discovery":
            raw_request = payload.get("request")
            if not isinstance(raw_request, Mapping):
                raise ValueError("策略实验请求内容无效")
            discovery_request = discovery_request_from_dict(raw_request)
            return dict(
                    selected_service.run(
                    discovery_request,
                    progress_callback=progress_callback,
                    abort_check=abort_check,
                )
            )
        if operation == "refinement":
            experiment = _mapping(payload.get("experiment"), "父实验")
            parent_hash = _required_string(payload.get("parent_candidate_hash"), "父候选 hash")
            return dict(
                selected_service.refine(
                    experiment,
                    parent_hash,
                    max_trials=_optional_int(payload.get("max_trials")),
                    seed=_optional_int(payload.get("seed")),
                    radius=cast(Literal["medium"], str(payload.get("radius") or "medium")),
                    progress_callback=progress_callback,
                    abort_check=abort_check,
                )
            )
        if operation == "replay":
            experiment = _mapping(payload.get("experiment"), "复核实验")
            leaderboard = str(payload.get("leaderboard") or "")
            if leaderboard not in {"sharpe", "return"}:
                raise ValueError("策略复核榜单类型无效")
            return dict(
                selected_service.replay(
                    experiment,
                    cast(ReplayLeaderboard, leaderboard),
                    progress_callback=progress_callback,
                    abort_check=abort_check,
                )
            )
        raise ValueError("策略任务类型无效")

    def _mark_cancelled(self, job_id: str) -> None:
        self._job_store.update(
            job_id,
            status="cancelled",
            phase="cancelled",
            error="策略实验已取消",
            error_code="cancelled",
        )


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label}内容无效")
    return value


def _required_string(value: object, label: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{label}不能为空")
    return result


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("策略任务整数参数无效")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("策略任务整数参数无效") from exc


def _error_code(exc: Exception) -> str:
    message = str(exc).lower()
    if "resource_budget_exceeded" in message or "内存预算" in message:
        return "resource_budget_exceeded"
    if "取消" in str(exc):
        return "cancelled"
    return "worker_failed"


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    store = DuckDbMarketDataStore()
    job_store = StrategyJobStore(default_strategy_job_root(store.database_path))
    worker = StrategyDiscoveryWorker(
        job_store,
        StrategyDiscoveryService(
            store=store,
            evaluation_workers=resolve_discovery_workers(),
        ),
        service_factory=lambda worker_count: StrategyDiscoveryService(
            store=store,
            evaluation_workers=worker_count,
        ),
    )
    stop_event = Event()

    def stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    worker.run_forever(stop_event=stop_event)


if __name__ == "__main__":
    main()


__all__ = ["StrategyDiscoveryWorker", "main"]
