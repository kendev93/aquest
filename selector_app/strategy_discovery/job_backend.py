"""Backend implementations for persistent strategy discovery jobs."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Literal, Protocol, cast

from selector_app.market_data.adapter import DuckDbMarketDataAdapter
from selector_app.market_data.models import InstrumentBoard, InstrumentType
from selector_app.market_data.store import DuckDbMarketDataStore, MarketDataStoreError
from selector_app.screening.jobs import ScreenJobRunner, TaskCancelledError, TaskUserError

from .job_store import DEFAULT_STALE_AFTER_SECONDS, JobOperation, StrategyJobStore
from .models import DiscoveryRequest
from .refinement import discovery_request_from_dict
from .replay import ReplayLeaderboard
from .replay_service import ReplayProgressCallback
from .resources import (
    DiscoveryResourceBudgetError,
    DiscoveryResourceEstimate,
    estimate_discovery_resources,
)
from .service import ProgressCallback
from .snapshot import snapshot_window

logger = logging.getLogger(__name__)


class StrategyJobBackend(Protocol):
    """Common interface consumed by the strategy discovery API router."""

    def preflight(self, request: DiscoveryRequest) -> DiscoveryResourceEstimate | None: ...

    def preflight_operation(
        self,
        operation: JobOperation,
        payload: Mapping[str, object],
    ) -> DiscoveryResourceEstimate | None: ...

    def submit(
        self,
        operation: JobOperation,
        payload: Mapping[str, object],
        *,
        description: str,
        worker_count: int | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> str: ...

    def get(self, job_id: str) -> dict[str, object] | None: ...

    def result(self, job_id: str) -> dict[str, object] | None: ...

    def cancel(self, job_id: str) -> bool: ...


class FileStrategyJobBackend:
    """Submit jobs to the shared-volume queue used by ``strategy-worker``."""

    def __init__(
        self,
        store: DuckDbMarketDataStore,
        job_store: StrategyJobStore,
        *,
        worker_count: int | None = None,
    ) -> None:
        self._store = store
        self._job_store = job_store
        self._worker_count = worker_count

    def preflight(self, request: DiscoveryRequest) -> DiscoveryResourceEstimate:
        return self._preflight_request(request)

    def preflight_operation(
        self,
        operation: JobOperation,
        payload: Mapping[str, object],
    ) -> DiscoveryResourceEstimate:
        if operation == "discovery":
            raw_request = payload.get("request")
            if not isinstance(raw_request, Mapping):
                raise ValueError("策略实验请求内容无效")
            request = discovery_request_from_dict(raw_request)
        elif operation == "refinement":
            experiment = _mapping(payload.get("experiment"), "父实验")
            raw_request = experiment.get("request")
            if not isinstance(raw_request, Mapping):
                raise ValueError("父实验请求内容无效")
            request = discovery_request_from_dict(raw_request)
        elif operation == "replay":
            experiment = _mapping(payload.get("experiment"), "复核实验")
            raw_request = experiment.get("request")
            if not isinstance(raw_request, Mapping):
                raise ValueError("复核实验请求内容无效")
            request = _replay_resource_request(
                discovery_request_from_dict(raw_request),
                self._store,
            )
        else:
            raise ValueError("策略任务类型无效")
        return self._preflight_request(request)

    def _preflight_request(self, request: DiscoveryRequest) -> DiscoveryResourceEstimate:
        base = request.base_config
        instrument_types = cast(
            tuple[InstrumentType, ...] | None,
            _string_tuple(base.get("instrument_types")),
        )
        boards = cast(
            tuple[InstrumentBoard, ...] | None,
            _string_tuple(base.get("boards")),
        )
        adapter = DuckDbMarketDataAdapter(self._store)
        refs = adapter.list_stock_refs(
            str(base.get("vipdoc_path") or ""),
            str(base.get("universe") or "all"),
            str(base["universe_file"]) if base.get("universe_file") else None,
            instrument_types=instrument_types,
            boards=boards,
        )
        if not refs:
            raise ValueError("本地 DuckDB 没有可用行情，请先在“本地行情”页面导入日线数据")
        window = snapshot_window(request)
        row_count = adapter.count_many_stocks(
            refs,
            start_date=window.start,
            end_date=window.end,
        )
        if row_count <= 0:
            raise ValueError("本地 DuckDB 没有可用行情，请先在“本地行情”页面导入日线数据")
        estimate = estimate_discovery_resources(
            row_count,
            requested_workers=self._worker_count,
        )
        if not estimate.within_budget:
            raise DiscoveryResourceBudgetError(estimate)
        return estimate

    def submit(
        self,
        operation: JobOperation,
        payload: Mapping[str, object],
        *,
        description: str,
        worker_count: int | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> str:
        job_id = uuid.uuid4().hex
        self._job_store.create(
            job_id,
            operation=operation,
            payload=payload,
            description=description,
            worker_count=worker_count if worker_count is not None else self._worker_count,
        )
        if metadata:
            self._job_store.update(job_id, metadata=metadata)
        return job_id

    def get(self, job_id: str) -> dict[str, object] | None:
        self._job_store.recover_stale_running(stale_after_seconds=DEFAULT_STALE_AFTER_SECONDS)
        state = self._job_store.read_state(job_id)
        if state is None:
            return None
        self._reconcile_completed(job_id, state)
        refreshed = self._job_store.read_state(job_id)
        return refreshed or state

    def result(self, job_id: str) -> dict[str, object] | None:
        self._job_store.recover_stale_running(stale_after_seconds=DEFAULT_STALE_AFTER_SECONDS)
        state = self._job_store.read_state(job_id)
        if state is None:
            return None
        self._reconcile_completed(job_id, state)
        return self._job_store.read_result(job_id)

    def cancel(self, job_id: str) -> bool:
        return self._job_store.request_cancel(job_id)

    def _reconcile_completed(self, job_id: str, state: Mapping[str, object]) -> None:
        if state.get("status") != "completed":
            return
        metadata = state.get("metadata")
        if isinstance(metadata, Mapping) and metadata.get("history_persisted") is True:
            return
        request = self._job_store.read_request(job_id)
        result = self._job_store.read_result(job_id)
        if request is None or result is None:
            return
        operation = request.get("operation")
        if operation not in {"discovery", "refinement", "replay"}:
            return
        try:
            normalized = decorate_result(
                job_id,
                operation,
                result,
                started_at=result.get("started_at"),
            )
            _persist_result(self._store, operation, job_id, normalized)
            self._job_store.update(
                job_id,
                metadata={"history_persisted": True, "history_persistence_error": None},
            )
        except (MarketDataStoreError, OSError, RuntimeError, ValueError) as exc:
            logger.exception("策略任务结果持久化失败: %s", job_id)
            self._job_store.update(
                job_id,
                metadata={"history_persistence_error": str(exc)[:500]},
            )


class InProcessStrategyJobBackend:
    """Compatibility backend for tests that inject a fake discovery service."""

    def __init__(
        self,
        store: DuckDbMarketDataStore,
        runner: ScreenJobRunner,
        service: StrategyDiscoveryServiceLike,
    ) -> None:
        self._store = store
        self._runner = runner
        self._service = service

    def preflight(self, request: DiscoveryRequest) -> None:
        return None

    def preflight_operation(
        self,
        operation: JobOperation,
        payload: Mapping[str, object],
    ) -> None:
        return None

    def submit(
        self,
        operation: JobOperation,
        payload: Mapping[str, object],
        *,
        description: str,
        worker_count: int | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> str:
        job_id = uuid.uuid4().hex

        def task(progress: Callable[[int, int], None]) -> dict[str, object]:
            started_at = _utc_now_text()

            def report_progress(
                current: int,
                total: int,
                phase: str,
                metadata: Mapping[str, object] | None = None,
            ) -> None:
                progress(current, total)
                raw_progress = (metadata or {}).get("overall_progress")
                self._runner.update_callable_state(
                    job_id,
                    phase=phase,
                    metadata=dict(metadata or {}),
                    progress=(
                        float(raw_progress) if isinstance(raw_progress, (int, float)) else None
                    ),
                )

            try:
                result = _dispatch(
                    self._service,
                    operation,
                    payload,
                    progress_callback=report_progress,
                    abort_check=lambda: self._runner.is_cancel_requested(job_id),
                )
                normalized = decorate_result(job_id, operation, result, started_at=started_at)
                completion = completion_state(operation, normalized)
                self._runner.update_callable_state(
                    job_id,
                    phase="completed",
                    metadata=cast(dict[str, object], completion["metadata"]),
                    progress=1.0,
                    errors=cast(int | None, completion.get("errors")),
                    total_candidates=cast(int | None, completion.get("total_candidates")),
                    total_scanned=cast(int | None, completion.get("total_scanned")),
                )
                _persist_result(self._store, operation, job_id, normalized)
                return normalized
            except RuntimeError as exc:
                if self._runner.is_cancel_requested(job_id):
                    raise TaskCancelledError("策略实验已取消") from exc
                raise TaskUserError(str(exc)) from exc
            except (ValueError, MarketDataStoreError, OSError) as exc:
                if self._runner.is_cancel_requested(job_id):
                    raise TaskCancelledError("策略实验已取消") from exc
                raise TaskUserError(str(exc)) from exc

        self._runner.submit_callable(
            task,
            description=description,
            worker_count=1,
            job_id=job_id,
        )
        if metadata:
            self._runner.update_callable_state(job_id, metadata=dict(metadata))
        return job_id

    def get(self, job_id: str) -> dict[str, object] | None:
        state = self._runner.get(job_id)
        if state is None:
            return None
        snapshot = state.snapshot()
        snapshot["result"] = None
        snapshot.setdefault("error_code", None)
        return snapshot

    def result(self, job_id: str) -> dict[str, object] | None:
        state = self._runner.get(job_id)
        if state is None or state.status != "completed":
            return None
        return dict(state.result_payload) if state.result_payload is not None else None

    def cancel(self, job_id: str) -> bool:
        return self._runner.request_cancel(job_id)


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


def _dispatch(
    service: StrategyDiscoveryServiceLike,
    operation: JobOperation,
    payload: Mapping[str, object],
    *,
    progress_callback: ProgressCallback,
    abort_check: Callable[[], bool],
) -> dict[str, object]:
    if operation == "discovery":
        raw_request = payload.get("request")
        if not isinstance(raw_request, Mapping):
            raise ValueError("策略实验请求内容无效")
        return dict(
            service.run(
                discovery_request_from_dict(raw_request),
                progress_callback=progress_callback,
                abort_check=abort_check,
            )
        )
    if operation == "refinement":
        experiment = _mapping(payload.get("experiment"), "父实验")
        parent_hash = _required_string(payload.get("parent_candidate_hash"), "父候选 hash")
        radius = str(payload.get("radius") or "medium")
        if radius != "medium":
            raise ValueError("策略二次优化范围无效")
        return dict(
            service.refine(
                experiment,
                parent_hash,
                max_trials=_optional_int(payload.get("max_trials")),
                seed=_optional_int(payload.get("seed")),
                radius="medium",
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
            service.replay(
                experiment,
                cast(ReplayLeaderboard, leaderboard),
                progress_callback=progress_callback,
                abort_check=abort_check,
            )
        )
    raise ValueError("策略任务类型无效")


def decorate_result(
    job_id: str,
    operation: JobOperation,
    result: Mapping[str, object],
    *,
    started_at: object | None = None,
) -> dict[str, object]:
    """Add stable task/history identifiers and timestamps to worker results."""

    normalized = dict(result)
    started = str(started_at or normalized.get("started_at") or _utc_now_text())
    finished = str(normalized.get("finished_at") or _utc_now_text())
    normalized["started_at"] = started
    normalized["finished_at"] = finished
    if operation in {"discovery", "refinement"}:
        normalized["history_id"] = job_id
        raw_duration = normalized.get("duration_seconds")
        if not isinstance(raw_duration, (int, float)):
            normalized["duration_seconds"] = _duration_seconds(started, finished)
    else:
        normalized["replay_id"] = job_id
        raw_duration = normalized.get("duration_seconds")
        if not isinstance(raw_duration, (int, float)):
            normalized["duration_seconds"] = _duration_seconds(started, finished)
    return normalized


def completion_state(operation: JobOperation, result: Mapping[str, object]) -> dict[str, object]:
    """Build the public job metadata emitted when an operation completes."""

    if operation in {"discovery", "refinement"}:
        search_metadata = result.get("search_metadata")
        search = search_metadata if isinstance(search_metadata, Mapping) else {}
        metadata: dict[str, object] = {
            "overall_progress": 1.0,
            "estimated_seconds_remaining": 0.0,
            "requested_trials": search.get("requested_trials"),
            "evaluated_trials": search.get("evaluated_trials"),
            "unique_evaluated_trials": search.get("unique_evaluated_trials"),
            "duplicate_trials": search.get("duplicate_trials"),
            "stage_durations_seconds": result.get("stage_durations_seconds"),
        }
        if operation == "refinement":
            metadata["refinement"] = True
        return {
            "metadata": metadata,
            "total_candidates": _nonnegative_int(search.get("requested_trials")),
            "total_scanned": _nonnegative_int(search.get("evaluated_trials")),
        }
    diagnostics = result.get("diagnostics")
    raw_candidate_count = result.get("candidate_count")
    candidate_count = _nonnegative_int(raw_candidate_count)
    return {
        "metadata": {
            "overall_progress": 1.0,
            "candidate_count": candidate_count,
            "data_changed": result.get("data_changed", False),
            "finished_at": result.get("finished_at"),
            "duration_seconds": result.get("duration_seconds", 0.0),
        },
        "errors": len(diagnostics) if isinstance(diagnostics, Mapping) else 0,
        "total_candidates": candidate_count,
        "total_scanned": candidate_count,
    }


def _persist_result(
    store: DuckDbMarketDataStore,
    operation: JobOperation,
    job_id: str,
    result: Mapping[str, object],
) -> None:
    if operation in {"discovery", "refinement"}:
        store.save_strategy_discovery_history(
            job_id,
            result,
            started_at=str(result.get("started_at") or ""),
            finished_at=str(result.get("finished_at") or ""),
        )
        return
    store.save_strategy_discovery_replay_history(
        job_id,
        result,
        started_at=str(result.get("started_at") or ""),
        finished_at=str(result.get("finished_at") or ""),
        duration_seconds=_duration_value(result.get("duration_seconds")),
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
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("策略任务整数参数无效") from exc


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat()


def _duration_seconds(started_at: str, finished_at: str) -> float:
    try:
        started = datetime.fromisoformat(started_at)
        finished = datetime.fromisoformat(finished_at)
        return max(0.0, (finished - started).total_seconds())
    except ValueError:
        return 0.0


def _duration_value(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value)) if isinstance(value, (int, float, str)) else 0
    except (TypeError, ValueError, OverflowError):
        return 0


__all__ = [
    "FileStrategyJobBackend",
    "InProcessStrategyJobBackend",
    "StrategyDiscoveryServiceLike",
    "StrategyJobBackend",
    "completion_state",
    "decorate_result",
]


def _string_tuple(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, (list, tuple)):
        return None
    values = tuple(str(item).strip() for item in value if str(item).strip())
    return values or None


def _replay_resource_request(
    request: DiscoveryRequest,
    store: DuckDbMarketDataStore,
) -> DiscoveryRequest:
    store_status = store.status()
    start = _parse_date(request.base_config.get("start_date"))
    end = _parse_date(store_status.data_end)
    if start is None:
        start = _parse_date(store_status.data_start)
    if start is None or end is None:
        raise ValueError("本地 DuckDB 没有可用于全量复核的已完成日线")
    if start > end:
        raise ValueError("全量复核开始日期晚于本地最新行情日")
    base = {**request.base_config, "start_date": start.isoformat(), "end_date": end.isoformat()}
    return replace(
        request,
        base_config=base,
        splits={"search": {"start_date": start.isoformat(), "end_date": end.isoformat()}},
    )


def _parse_date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None
