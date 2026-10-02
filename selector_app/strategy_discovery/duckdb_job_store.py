"""DuckDB-backed strategy task records, written only by the API process."""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

import duckdb

from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.market_data.store_base import _WRITE_LOCK

from .job_store import (
    DEFAULT_STALE_AFTER_SECONDS,
    JobOperation,
    JobStatus,
    StrategyJobStore,
    _initial_state,
    is_job_operation,
)

_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
_VALID_STATUSES = frozenset({"queued", "running", *_TERMINAL_STATUSES})
_WORKER_PROGRESS_FIELDS = frozenset(
    {
        "phase",
        "progress",
        "total_candidates",
        "total_scanned",
        "total_signals",
        "errors",
        "skipped",
        "metadata",
        "heartbeat_at",
        "status",
    }
)


class DuckDbStrategyJobStore:
    """Persist job envelopes, progress, cancellation and results in the app DB."""

    def __init__(self, store: DuckDbMarketDataStore) -> None:
        self._store = store

    def initialize(self, legacy_root: str | Path | None = None) -> int:
        """Initialize the DuckDB schema and idempotently import legacy JSON jobs."""

        with _WRITE_LOCK, self._connection() as connection:
            connection.execute("SELECT 1 FROM strategy_jobs LIMIT 0")
        if legacy_root is None:
            return 0
        return self.import_legacy_jobs(legacy_root)

    def create(
        self,
        job_id: str,
        *,
        operation: JobOperation,
        payload: Mapping[str, object],
        description: str,
        worker_count: int | None = None,
    ) -> None:
        StrategyJobStore._validate_job_id(job_id)
        if not is_job_operation(operation):
            raise ValueError("策略任务类型无效")
        now = time.time()
        request = {
            "job_id": job_id,
            "operation": operation,
            "payload": dict(payload),
            "description": description,
        }
        state = _initial_state(
            job_id,
            description=description,
            worker_count=worker_count,
            created_at=now,
        )
        with _WRITE_LOCK, self._transaction() as connection:
            existing = connection.execute(
                "SELECT 1 FROM strategy_jobs WHERE job_id = ?", [job_id]
            ).fetchone()
            if existing is not None:
                raise ValueError("策略任务 ID 已存在")
            connection.execute(
                """
                INSERT INTO strategy_jobs (
                    job_id, operation, request_json, state_json, status,
                    cancel_requested, created_at, heartbeat_at, updated_at
                ) VALUES (?, ?, ?, ?, 'queued', FALSE, ?, ?, ?)
                """,
                [
                    job_id,
                    operation,
                    _dump_json(request),
                    _dump_json(state),
                    now,
                    now,
                    now,
                ],
            )

    def read_request(self, job_id: str) -> dict[str, object] | None:
        StrategyJobStore._validate_job_id(job_id)
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT request_json FROM strategy_jobs WHERE job_id = ?", [job_id]
            ).fetchone()
        return _load_object(row[0]) if row else None

    def read_state(self, job_id: str) -> dict[str, object] | None:
        StrategyJobStore._validate_job_id(job_id)
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT state_json, cancel_requested FROM strategy_jobs WHERE job_id = ?",
                [job_id],
            ).fetchone()
        if row is None:
            return None
        state = _load_object(row[0])
        metadata = state.get("metadata")
        state["metadata"] = dict(metadata) if isinstance(metadata, Mapping) else {}
        state["cancel_requested"] = bool(row[1])
        return state

    def list_snapshots(self) -> list[dict[str, object]]:
        """Return all job states for migration checks and operator diagnostics."""

        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT operation, state_json, cancel_requested "
                "FROM strategy_jobs ORDER BY created_at, job_id"
            ).fetchall()
        snapshots: list[dict[str, object]] = []
        for operation, raw_state, cancel_requested in rows:
            state = _load_object(raw_state)
            state["operation"] = operation
            state["cancel_requested"] = bool(cancel_requested)
            snapshots.append(state)
        return snapshots

    def active_snapshots(
        self,
        descriptions: Iterable[str] | None = None,
    ) -> list[dict[str, object]]:
        allowed = frozenset(descriptions) if descriptions is not None else None
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT operation, state_json, cancel_requested
                FROM strategy_jobs
                WHERE status IN ('queued', 'running')
                ORDER BY created_at, job_id
                """
            ).fetchall()
        snapshots: list[dict[str, object]] = []
        for operation, raw_state, cancel_requested in rows:
            state = _load_object(raw_state)
            if allowed is not None and state.get("description") not in allowed:
                continue
            state["operation"] = operation
            state["cancel_requested"] = bool(cancel_requested)
            snapshots.append(state)
        return snapshots

    def read_result(self, job_id: str) -> dict[str, object] | None:
        StrategyJobStore._validate_job_id(job_id)
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT result_json FROM strategy_jobs WHERE job_id = ?", [job_id]
            ).fetchone()
        return _load_object(row[0]) if row and row[0] is not None else None

    def list_risk_optimization_history(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, object]:
        """List completed risk searches while leaving their full results in job rows."""

        if isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("风险寻优历史每页数量必须在 1 到 100 之间")
        if isinstance(offset, bool) or offset < 0:
            raise ValueError("风险寻优历史偏移量不能小于 0")
        with self._read_connection() as connection:
            total_row = connection.execute(
                """
                SELECT COUNT(*) FROM strategy_jobs
                WHERE operation = 'risk_optimization'
                  AND status = 'completed'
                  AND result_json IS NOT NULL
                """
            ).fetchone()
            rows = connection.execute(
                """
                SELECT job_id, request_json, state_json, result_json
                FROM strategy_jobs
                WHERE operation = 'risk_optimization'
                  AND status = 'completed'
                  AND result_json IS NOT NULL
                ORDER BY created_at DESC, job_id DESC
                LIMIT ? OFFSET ?
                """,
                [limit, offset],
            ).fetchall()
        items = [
            _risk_optimization_history_summary(
                str(row[0]),
                _load_object(row[1]),
                _load_object(row[2]),
                _load_object(row[3]),
            )
            for row in rows
        ]
        return {
            "items": items,
            "total": int(total_row[0]) if total_row else 0,
            "limit": limit,
            "offset": offset,
        }

    def list_market_strategy_pool_history(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, object]:
        if isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("策略池任务历史每页数量必须在 1 到 100 之间")
        if isinstance(offset, bool) or offset < 0:
            raise ValueError("策略池任务历史偏移量不能小于 0")
        with self._read_connection() as connection:
            total_row = connection.execute(
                "SELECT COUNT(*) FROM strategy_jobs WHERE operation = 'market_strategy_pool'"
            ).fetchone()
            rows = connection.execute(
                """
                SELECT job_id, request_json, state_json
                FROM strategy_jobs WHERE operation = 'market_strategy_pool'
                ORDER BY created_at DESC, job_id DESC LIMIT ? OFFSET ?
                """,
                [limit, offset],
            ).fetchall()
        items: list[dict[str, object]] = []
        for job_id, raw_request, raw_state in rows:
            request = _load_object(raw_request)
            state = _load_object(raw_state)
            envelope = request.get("payload")
            payload = envelope if isinstance(envelope, Mapping) else {}
            metadata = state.get("metadata")
            progress = metadata if isinstance(metadata, Mapping) else {}
            items.append(
                {
                    "job_id": str(job_id),
                    "status": state.get("status"),
                    "mode": payload.get("mode", "full"),
                    "objective": payload.get("objective"),
                    "market": payload.get("market"),
                    "code": payload.get("code"),
                    "created_at": state.get("created_at"),
                    "finished_at": state.get("finished_at"),
                    "completed_symbols": progress.get("completed_symbols", 0),
                    "skipped_symbols": progress.get("skipped_symbols", 0),
                    "failed_symbols": progress.get("failed_symbols", 0),
                    "total_symbols": progress.get("total_symbols", 0),
                    "error": state.get("error"),
                }
            )
        return {
            "items": items,
            "total": int(total_row[0]) if total_row else 0,
            "limit": limit,
            "offset": offset,
        }

    def claim_next(self, worker_id: str | None = None) -> str | None:
        selected_worker = _validate_worker_id(worker_id or "local-worker")
        self.recover_stale_running(stale_after_seconds=DEFAULT_STALE_AFTER_SECONDS)
        now = time.time()
        with _WRITE_LOCK, self._transaction() as connection:
            row = connection.execute(
                """
                SELECT job_id, state_json
                FROM strategy_jobs
                WHERE status = 'queued'
                ORDER BY created_at, job_id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            job_id = str(row[0])
            state = _load_object(row[1])
            state.update(status="running", phase="running", heartbeat_at=now)
            connection.execute(
                """
                UPDATE strategy_jobs
                SET status = 'running', worker_id = ?, state_json = ?,
                    heartbeat_at = ?, updated_at = ?
                WHERE job_id = ? AND status = 'queued'
                """,
                [selected_worker, _dump_json(state), now, now, job_id],
            )
            return job_id

    def read_claimed_job(self, job_id: str, worker_id: str) -> dict[str, object]:
        StrategyJobStore._validate_job_id(job_id)
        selected_worker = _validate_worker_id(worker_id)
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT request_json, state_json, cancel_requested, worker_id
                FROM strategy_jobs WHERE job_id = ?
                """,
                [job_id],
            ).fetchone()
        if row is None or row[3] != selected_worker:
            raise ValueError("策略任务 worker 不匹配")
        request = _load_object(row[0])
        state = _load_object(row[1])
        state["cancel_requested"] = bool(row[2])
        return {"job_id": job_id, "request": request, "state": state}

    def update(
        self,
        job_id: str,
        fields: Mapping[str, object] | None = None,
        **updates: object,
    ) -> None:
        StrategyJobStore._validate_job_id(job_id)
        explicit = {**dict(fields or {}), **updates}
        with _WRITE_LOCK, self._transaction() as connection:
            row = self._select_state(connection, job_id)
            if row is None:
                raise ValueError("策略任务不存在")
            self._save_state(connection, job_id, row, explicit)

    def update_from_worker(
        self,
        job_id: str,
        worker_id: str,
        fields: Mapping[str, object] | None = None,
        **updates: object,
    ) -> dict[str, object]:
        explicit = {**dict(fields or {}), **updates}
        if not explicit.keys() <= _WORKER_PROGRESS_FIELDS:
            raise ValueError("策略 worker 进度字段无效")
        if explicit.get("status", "running") != "running":
            raise ValueError("终态必须通过对应的 worker 操作提交")
        now = time.time()
        explicit["status"] = "running"
        explicit["heartbeat_at"] = now
        return self._update_from_worker(job_id, worker_id, explicit)

    def heartbeat(self, job_id: str, worker_id: str) -> dict[str, object]:
        return self._update_from_worker(job_id, worker_id, {"heartbeat_at": time.time()})

    def request_cancel(self, job_id: str) -> bool:
        StrategyJobStore._validate_job_id(job_id)
        now = time.time()
        with _WRITE_LOCK, self._transaction() as connection:
            row = self._select_state(connection, job_id)
            if row is None or str(row[1]) in _TERMINAL_STATUSES:
                return False
            state = _load_object(row[0])
            state.update(phase="cancelling", cancel_requested=True)
            connection.execute(
                """
                UPDATE strategy_jobs
                SET state_json = ?, cancel_requested = TRUE, updated_at = ?
                WHERE job_id = ?
                """,
                [_dump_json(state), now, job_id],
            )
            return True

    def is_cancel_requested(self, job_id: str) -> bool:
        StrategyJobStore._validate_job_id(job_id)
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM strategy_jobs WHERE job_id = ?", [job_id]
            ).fetchone()
        return bool(row[0]) if row else False

    def write_result(self, job_id: str, result: Mapping[str, object]) -> None:
        StrategyJobStore._validate_job_id(job_id)
        self._complete(job_id, result, worker_id=None)

    def write_result_from_worker(
        self,
        job_id: str,
        worker_id: str,
        result: Mapping[str, object],
    ) -> dict[str, object]:
        state = self._complete(job_id, result, worker_id=worker_id)
        if state is None:
            raise RuntimeError("策略任务结果未提交")
        return state

    def fail_from_worker(
        self,
        job_id: str,
        worker_id: str,
        error: str,
        error_code: str,
    ) -> dict[str, object]:
        now = time.time()
        return self._finish_from_worker(
            job_id,
            worker_id,
            status="failed",
            fields={"error": error[:500], "error_code": error_code[:80]},
            now=now,
        )

    def cancel_from_worker(self, job_id: str, worker_id: str) -> dict[str, object]:
        return self._finish_from_worker(
            job_id,
            worker_id,
            status="cancelled",
            fields={"error": "策略实验已取消", "error_code": "cancelled"},
            now=time.time(),
        )

    def recover_stale_running(self, *, stale_after_seconds: float) -> list[str]:
        if stale_after_seconds <= 0:
            raise ValueError("策略任务 stale 时间必须大于 0")
        now = time.time()
        with self._read_connection() as connection:
            heartbeats = connection.execute(
                "SELECT heartbeat_at FROM strategy_jobs WHERE status = 'running'"
            ).fetchall()
        if not any(
            isinstance(row[0], (int, float)) and now - float(row[0]) > stale_after_seconds
            for row in heartbeats
        ):
            return []

        recovered: list[str] = []
        with _WRITE_LOCK, self._transaction() as connection:
            rows = connection.execute(
                "SELECT job_id, state_json, heartbeat_at FROM strategy_jobs "
                "WHERE status = 'running' ORDER BY created_at, job_id"
            ).fetchall()
            for raw_job_id, raw_state, heartbeat_at in rows:
                if not isinstance(heartbeat_at, (int, float)):
                    continue
                if now - float(heartbeat_at) <= stale_after_seconds:
                    continue
                job_id = str(raw_job_id)
                state = _load_object(raw_state)
                state.update(
                    status="failed",
                    phase="failed",
                    error="策略实验 worker 已停止，任务未自动重试",
                    error_code="worker_stale",
                    finished_at=now,
                )
                connection.execute(
                    "UPDATE strategy_jobs SET status = 'failed', state_json = ?, updated_at = ? "
                    "WHERE job_id = ? AND status = 'running'",
                    [_dump_json(state), now, job_id],
                )
                recovered.append(job_id)
        return recovered

    def import_legacy_jobs(self, legacy_root: str | Path) -> int:
        root = Path(legacy_root).expanduser()
        if not root.is_dir():
            return 0
        legacy_store = StrategyJobStore(root)
        records = legacy_store.iter_records()
        prepared: list[tuple[object, ...]] = []
        for job_id, request, state, result in records:
            operation = request.get("operation")
            status = state.get("status")
            if not is_job_operation(operation) or status not in _VALID_STATUSES:
                raise ValueError(f"旧策略任务记录无效: {job_id}")
            if request.get("job_id") != job_id or state.get("job_id") != job_id:
                raise ValueError(f"旧策略任务 ID 不一致: {job_id}")
            created_at = _number(state.get("created_at"), time.time())
            heartbeat_at = _number(state.get("heartbeat_at"), created_at)
            prepared.append(
                (
                    job_id,
                    operation,
                    _dump_json(request),
                    _dump_json(state),
                    _dump_json(result) if result is not None else None,
                    status,
                    bool(state.get("cancel_requested")),
                    None,
                    created_at,
                    heartbeat_at,
                    time.time(),
                )
            )
        if not prepared:
            return 0
        with _WRITE_LOCK, self._transaction() as connection:
            before_row = connection.execute("SELECT COUNT(*) FROM strategy_jobs").fetchone()
            if before_row is None:
                raise RuntimeError("策略任务计数查询失败")
            before = int(before_row[0])
            connection.executemany(
                """
                INSERT INTO strategy_jobs (
                    job_id, operation, request_json, state_json, result_json,
                    status, cancel_requested, worker_id, created_at, heartbeat_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (job_id) DO NOTHING
                """,
                prepared,
            )
            after_row = connection.execute("SELECT COUNT(*) FROM strategy_jobs").fetchone()
            if after_row is None:
                raise RuntimeError("策略任务计数查询失败")
            after = int(after_row[0])
        return after - before

    def _update_from_worker(
        self,
        job_id: str,
        worker_id: str,
        fields: Mapping[str, object],
    ) -> dict[str, object]:
        StrategyJobStore._validate_job_id(job_id)
        selected_worker = _validate_worker_id(worker_id)
        now = time.time()
        with _WRITE_LOCK, self._transaction() as connection:
            row = self._select_state(connection, job_id)
            if row is None or row[3] != selected_worker or row[1] != "running":
                raise ValueError("策略任务 worker 不匹配或任务已结束")
            self._save_state(connection, job_id, row, fields)
        state = self.read_state(job_id)
        if state is None:
            raise ValueError("策略任务不存在")
        state["heartbeat_at"] = now
        return state

    def _finish_from_worker(
        self,
        job_id: str,
        worker_id: str,
        *,
        status: JobStatus,
        fields: Mapping[str, object],
        now: float,
    ) -> dict[str, object]:
        StrategyJobStore._validate_job_id(job_id)
        selected_worker = _validate_worker_id(worker_id)
        with _WRITE_LOCK, self._transaction() as connection:
            row = self._select_state(connection, job_id)
            if row is None or row[3] != selected_worker or row[1] != "running":
                raise ValueError("策略任务 worker 不匹配或任务已结束")
            cancelled_during_failure = status == "failed" and bool(row[2])
            final_status: JobStatus = "cancelled" if cancelled_during_failure else status
            final_fields = (
                {"error": "策略实验已取消", "error_code": "cancelled"}
                if cancelled_during_failure
                else dict(fields)
            )
            updates = {
                **final_fields,
                "status": final_status,
                "phase": final_status,
                "finished_at": now,
            }
            self._save_state(connection, job_id, row, updates)
        state = self.read_state(job_id)
        if state is None:
            raise ValueError("策略任务不存在")
        return state

    def _complete(
        self,
        job_id: str,
        result: Mapping[str, object],
        *,
        worker_id: str | None,
    ) -> dict[str, object] | None:
        now = time.time()
        with _WRITE_LOCK, self._transaction() as connection:
            row = self._select_state(connection, job_id)
            if row is None:
                raise ValueError("策略任务不存在")
            if worker_id is not None and (
                row[3] != _validate_worker_id(worker_id) or row[1] != "running"
            ):
                raise ValueError("策略任务 worker 不匹配或任务已结束")
            state = _load_object(row[0])
            state.update(
                status="completed",
                phase="completed",
                progress=1.0,
                error=None,
                error_code=None,
                finished_at=state.get("finished_at") or now,
            )
            connection.execute(
                """
                UPDATE strategy_jobs
                SET status = 'completed', state_json = ?, result_json = ?, updated_at = ?
                WHERE job_id = ?
                """,
                [_dump_json(state), _dump_json(result), now, job_id],
            )
        return self.read_state(job_id) if worker_id is not None else None

    @staticmethod
    def _select_state(
        connection: duckdb.DuckDBPyConnection,
        job_id: str,
    ) -> tuple[object, ...] | None:
        row = connection.execute(
            "SELECT state_json, status, cancel_requested, worker_id "
            "FROM strategy_jobs WHERE job_id = ?",
            [job_id],
        ).fetchone()
        return cast(tuple[object, ...] | None, row)

    @staticmethod
    def _save_state(
        connection: duckdb.DuckDBPyConnection,
        job_id: str,
        row: tuple[object, ...],
        updates: Mapping[str, object],
    ) -> None:
        state = _load_object(row[0])
        metadata = updates.get("metadata")
        merged = {**state, **dict(updates)}
        if isinstance(metadata, Mapping):
            previous = state.get("metadata")
            merged["metadata"] = {
                **(dict(previous) if isinstance(previous, Mapping) else {}),
                **dict(metadata),
            }
        status = str(merged.get("status") or row[1])
        if status not in _VALID_STATUSES:
            raise ValueError("策略任务状态无效")
        now = time.time()
        if status in _TERMINAL_STATUSES:
            merged["finished_at"] = merged.get("finished_at") or now
        elif status == "running" and "heartbeat_at" not in updates:
            merged["heartbeat_at"] = now
        cancel_requested = bool(row[2])
        merged["cancel_requested"] = cancel_requested
        heartbeat_at = _number(merged.get("heartbeat_at"), now)
        connection.execute(
            """
            UPDATE strategy_jobs
            SET state_json = ?, status = ?, heartbeat_at = ?, updated_at = ?
            WHERE job_id = ?
            """,
            [_dump_json(merged), status, heartbeat_at, now, job_id],
        )

    @contextmanager
    def _connection(self) -> Iterator[duckdb.DuckDBPyConnection]:
        connection = self._store._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _read_connection(self) -> Iterator[duckdb.DuckDBPyConnection]:
        connection = self._store._connect(read_only=True)
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[duckdb.DuckDBPyConnection]:
        with self._connection() as connection:
            connection.execute("BEGIN TRANSACTION")
            try:
                yield connection
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise


def _dump_json(value: Mapping[str, object]) -> str:
    try:
        return json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("策略任务内容无法序列化") from exc


def _load_object(raw: object) -> dict[str, object]:
    try:
        value = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("DuckDB 策略任务 JSON 无效") from exc
    if not isinstance(value, Mapping):
        raise ValueError("DuckDB 策略任务 JSON 格式无效")
    return {str(key): cast(object, item) for key, item in value.items()}


def _risk_optimization_history_summary(
    job_id: str,
    request_envelope: Mapping[str, object],
    state: Mapping[str, object],
    result: Mapping[str, object],
) -> dict[str, object]:
    payload = _mapping_or_empty(request_envelope.get("payload"))
    discovery_request = _mapping_or_empty(payload.get("request"))
    result_request = _mapping_or_empty(result.get("request"))
    base_config = _mapping_or_empty(
        result_request.get("base_config") or discovery_request.get("base_config")
    )
    search_metadata = _mapping_or_empty(result.get("search_metadata"))
    state_metadata = _mapping_or_empty(state.get("metadata"))
    sharpe_rows = _sequence_of_mappings(result.get("results_by_sharpe"))
    return_rows = _sequence_of_mappings(result.get("results_by_return"))
    duration = _optional_float(result.get("duration_seconds"))
    if duration is None:
        duration = _duration_between(state.get("created_at"), state.get("finished_at"))
    return {
        "job_id": job_id,
        "status": "completed",
        "started_at": _iso_timestamp(result.get("started_at") or state.get("created_at")),
        "finished_at": _iso_timestamp(result.get("finished_at") or state.get("finished_at")),
        "duration_seconds": duration,
        "start_date": base_config.get("start_date"),
        "end_date": base_config.get("end_date"),
        "requested_trials": _optional_int(
            search_metadata.get("requested_trials")
            or state_metadata.get("requested_trials")
            or discovery_request.get("max_trials")
        ),
        "evaluated_trials": _optional_int(
            search_metadata.get("evaluated_trials") or state_metadata.get("evaluated_trials")
        ),
        "candidate_count": max(len(sharpe_rows), len(return_rows)),
        "validation_best_sharpe": _candidate_metric(sharpe_rows, "sharpe"),
        "validation_best_annual_return": _candidate_metric(return_rows, "annual_return"),
    }


def _mapping_or_empty(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _sequence_of_mappings(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _candidate_metric(candidates: list[Mapping[str, object]], metric: str) -> float | None:
    if not candidates:
        return None
    metrics = _mapping_or_empty(candidates[0].get("metrics"))
    validation = _mapping_or_empty(metrics.get("validation"))
    return _optional_float(validation.get(metric))


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


def _optional_int(value: object) -> int | None:
    parsed = _optional_float(value)
    return int(parsed) if parsed is not None else None


def _duration_between(start: object, end: object) -> float | None:
    start_value = _optional_float(start)
    end_value = _optional_float(end)
    if start_value is None or end_value is None or end_value < start_value:
        return None
    return end_value - start_value


def _iso_timestamp(value: object) -> str | None:
    if isinstance(value, str):
        return value
    timestamp = _optional_float(value)
    if timestamp is None:
        return None
    return (
        datetime.fromtimestamp(timestamp, timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _number(value: object, fallback: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    return float(value)


def _validate_worker_id(value: str) -> str:
    selected = str(value).strip()
    if not selected or len(selected) > 128:
        raise ValueError("策略 worker ID 无效")
    return selected


__all__ = ["DuckDbStrategyJobStore"]
