"""Legacy JSON job state reader retained for migration and compatibility tests."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Literal, TypeGuard, cast

JobOperation = Literal[
    "discovery",
    "risk_optimization",
    "refinement",
    "replay",
    "market_strategy_pool",
    "market_strategy_pool_tracking",
]
JobStatus = Literal["queued", "running", "completed", "failed", "cancelled"]

_JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_OPERATIONS = frozenset(
    {
        "discovery",
        "risk_optimization",
        "refinement",
        "replay",
        "market_strategy_pool",
        "market_strategy_pool_tracking",
    }
)
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
DEFAULT_STALE_AFTER_SECONDS = 120.0


def is_job_operation(value: object) -> TypeGuard[JobOperation]:
    """Return whether a decoded request operation is supported."""

    return value in _OPERATIONS


class StrategyJobStore:
    """Read/write the pre-DuckDB job format; production no longer uses this store."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        job_id: str,
        *,
        operation: JobOperation,
        payload: Mapping[str, object],
        description: str,
        worker_count: int | None = None,
    ) -> None:
        self._validate_job_id(job_id)
        if operation not in _OPERATIONS:
            raise ValueError("策略任务类型无效")
        job_dir = self._job_dir(job_id)
        try:
            job_dir.mkdir(parents=False)
        except FileExistsError as exc:
            raise ValueError("策略任务 ID 已存在") from exc
        self._write_json(
            job_dir / "request.json",
            {
                "job_id": job_id,
                "operation": operation,
                "payload": dict(payload),
                "description": description,
            },
        )
        now = time.time()
        self._write_json(
            job_dir / "state.json",
            _initial_state(
                job_id,
                description=description,
                worker_count=worker_count,
                created_at=now,
            ),
        )

    def read_request(self, job_id: str) -> dict[str, object] | None:
        payload = self._read_json(self._job_dir(job_id) / "request.json")
        return payload

    def read_state(self, job_id: str) -> dict[str, object] | None:
        state = self._read_json(self._job_dir(job_id) / "state.json")
        if state is None:
            return None
        metadata = state.get("metadata")
        state["metadata"] = dict(metadata) if isinstance(metadata, Mapping) else {}
        state["cancel_requested"] = self.is_cancel_requested(job_id)
        return state

    def active_snapshots(
        self,
        descriptions: Iterable[str] | None = None,
    ) -> list[dict[str, object]]:
        """Return queued/running jobs so a new API client can resume polling."""

        allowed = frozenset(descriptions) if descriptions is not None else None
        snapshots: list[dict[str, object]] = []
        for job_dir in sorted(self.root.iterdir(), key=lambda item: item.name):
            if not job_dir.is_dir() or not _JOB_ID_PATTERN.fullmatch(job_dir.name):
                continue
            state = self.read_state(job_dir.name)
            if state is None or state.get("status") not in {"queued", "running"}:
                continue
            description = state.get("description")
            if allowed is not None and description not in allowed:
                continue
            snapshot = dict(state)
            request = self.read_request(job_dir.name)
            operation = request.get("operation") if request is not None else None
            if operation in _OPERATIONS:
                snapshot["operation"] = operation
            snapshots.append(snapshot)
        return snapshots

    def read_result(self, job_id: str) -> dict[str, object] | None:
        return self._read_json(self._job_dir(job_id) / "result.json")

    def iter_records(
        self,
    ) -> list[tuple[str, dict[str, object], dict[str, object], dict[str, object] | None]]:
        """Return complete legacy records in stable order for one-time import."""

        records: list[
            tuple[str, dict[str, object], dict[str, object], dict[str, object] | None]
        ] = []
        for job_dir in sorted(self.root.iterdir(), key=lambda item: item.name):
            if not job_dir.is_dir() or not _JOB_ID_PATTERN.fullmatch(job_dir.name):
                continue
            request = self.read_request(job_dir.name)
            state = self.read_state(job_dir.name)
            if request is None or state is None:
                raise ValueError(f"策略任务文件不完整: {job_dir.name}")
            records.append((job_dir.name, request, state, self.read_result(job_dir.name)))
        return records

    def claim_next(self) -> str | None:
        for job_dir in sorted(self.root.iterdir(), key=lambda item: item.name):
            if not job_dir.is_dir() or not _JOB_ID_PATTERN.fullmatch(job_dir.name):
                continue
            state = self.read_state(job_dir.name)
            if state is None or state.get("status") != "queued":
                continue
            claim_path = job_dir / "claim"
            try:
                fd = os.open(claim_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                continue
            try:
                with os.fdopen(fd, "w", encoding="ascii") as handle:
                    handle.write(str(time.time()))
                    handle.flush()
                    os.fsync(handle.fileno())
                self.update(
                    job_dir.name,
                    status="running",
                    phase="running",
                    heartbeat_at=time.time(),
                )
            except Exception:
                claim_path.unlink(missing_ok=True)
                raise
            return job_dir.name
        return None

    def update(
        self,
        job_id: str,
        fields: Mapping[str, object] | None = None,
        **updates: object,
    ) -> None:
        """Atomically merge a status update into the current state."""

        current = self.read_state(job_id)
        if current is None:
            raise ValueError("策略任务不存在")
        explicit_updates = {**dict(fields or {}), **updates}
        merged = {**current, **explicit_updates}
        metadata = explicit_updates.get("metadata")
        if isinstance(metadata, Mapping):
            previous = current.get("metadata")
            merged["metadata"] = {
                **(dict(previous) if isinstance(previous, Mapping) else {}),
                **dict(metadata),
            }
        if merged.get("status") in _TERMINAL_STATUSES:
            merged["finished_at"] = merged.get("finished_at") or time.time()
        elif merged.get("status") == "running" and "heartbeat_at" not in explicit_updates:
            merged["heartbeat_at"] = time.time()
        self._write_json(self._job_dir(job_id) / "state.json", merged)

    def request_cancel(self, job_id: str) -> bool:
        state = self.read_state(job_id)
        if state is None or state.get("status") in _TERMINAL_STATUSES:
            return False
        self._write_json(
            self._job_dir(job_id) / "cancel",
            {"requested_at": time.time()},
        )
        self.update(job_id, phase="cancelling")
        return True

    def is_cancel_requested(self, job_id: str) -> bool:
        return self._job_dir(job_id).joinpath("cancel").is_file()

    def write_result(self, job_id: str, result: Mapping[str, object]) -> None:
        job_dir = self._job_dir(job_id)
        self._write_json(job_dir / "result.json", dict(result))
        self.update(
            job_id,
            status="completed",
            phase="completed",
            progress=1.0,
            error=None,
            error_code=None,
        )

    def recover_stale_running(self, *, stale_after_seconds: float) -> list[str]:
        if stale_after_seconds <= 0:
            raise ValueError("策略任务 stale 时间必须大于 0")
        now = time.time()
        recovered: list[str] = []
        for job_dir in sorted(self.root.iterdir(), key=lambda item: item.name):
            if not job_dir.is_dir() or not _JOB_ID_PATTERN.fullmatch(job_dir.name):
                continue
            state = self.read_state(job_dir.name)
            heartbeat = state.get("heartbeat_at") if state else None
            if (
                not state
                or state.get("status") != "running"
                or not isinstance(heartbeat, (int, float))
            ):
                continue
            if now - float(heartbeat) <= stale_after_seconds:
                continue
            self.update(
                job_dir.name,
                status="failed",
                phase="failed",
                error="策略实验 worker 已停止，任务未自动重试",
                error_code="worker_stale",
            )
            job_dir.joinpath("claim").unlink(missing_ok=True)
            recovered.append(job_dir.name)
        return recovered

    def job_path(self, job_id: str) -> Path:
        """Return a validated job directory for worker and API integrations."""

        return self._job_dir(job_id)

    def _job_dir(self, job_id: str) -> Path:
        self._validate_job_id(job_id)
        return self.root / job_id

    @staticmethod
    def _validate_job_id(job_id: str) -> None:
        if not isinstance(job_id, str) or _JOB_ID_PATTERN.fullmatch(job_id) is None:
            raise ValueError("策略任务 ID 格式无效")

    @staticmethod
    def _write_json(path: Path, payload: Mapping[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    dict(payload),
                    handle,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _read_json(path: Path) -> dict[str, object] | None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"策略任务文件无效: {path.name}") from exc
        if not isinstance(raw, Mapping):
            raise ValueError(f"策略任务文件格式无效: {path.name}")
        return {str(key): cast(object, value) for key, value in raw.items()}


def default_strategy_job_root(database_path: str | Path) -> Path:
    """Resolve the legacy JSON directory read only during one-time migration."""

    configured = os.getenv("AQUEST_DISCOVERY_JOB_DIR")
    if configured and configured.strip():
        return Path(configured).expanduser().resolve()
    return Path(database_path).expanduser().resolve().parent / ".strategy-jobs"


def _initial_state(
    job_id: str,
    *,
    description: str,
    worker_count: int | None,
    created_at: float,
) -> dict[str, object]:
    return {
        "job_id": job_id,
        "status": "queued",
        "progress": 0.0,
        "total_candidates": 0,
        "total_scanned": 0,
        "total_signals": 0,
        "errors": 0,
        "skipped": 0,
        "error": None,
        "error_code": None,
        "result": None,
        "description": description,
        "worker_count": worker_count,
        "created_at": created_at,
        "finished_at": None,
        "phase": "queued",
        "metadata": {},
        "cancel_requested": False,
        "heartbeat_at": created_at,
    }


__all__ = [
    "JobOperation",
    "JobStatus",
    "is_job_operation",
    "StrategyJobStore",
    "DEFAULT_STALE_AFTER_SECONDS",
    "default_strategy_job_root",
]
