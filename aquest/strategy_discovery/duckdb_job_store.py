"""DuckDB-backed strategy task records, written only by the API process."""

from __future__ import annotations

import json
import statistics
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

import duckdb

from aquest.market_data.store import DuckDbMarketDataStore
from aquest.market_data.store_base import _WRITE_LOCK
from aquest.market_data.sync_session import shanghai_now

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
#: 「认领后从未续租」的任务最多退回队列几次。超过就按原来的方式判死：连续几次都拿不到
#: 心跳说明 worker 与后端之间的通道一直坏着，继续重排只会让队列里堆一堆没人能跑的任务。
MAX_CLAIM_REQUEUES = 3
#: 任务时长统计每个模式最多采样多少条终态任务。跟踪轮询每十分钟写一条信号扫描
#: 记录，全市场研究跑几小时也只有几条，不限窗口就会把样本全花在信号扫描上。
POOL_STATS_SAMPLE_LIMIT = 60
#: 信号扫描结果保留多少个交易日。跟踪通道每十分钟就写一次全量结果，实测一次约 2 MB：
#: 2026-10-19 库里 3,063 条已完成的跟踪结果共 2.58 GB，占整库（4.1 GB）的 62%，而回看只需要
#: 每个交易日那一份，留 10 个交易日也就 20 MB。任务行本身不动，耗时与历史统计照旧。
POOL_SIGNAL_HISTORY_DAYS = 10
POOL_STATS_MODES = ("full", "single", "signals", "portfolio")
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

    def latest_pool_signal_attempt(self, start: float, end: float) -> dict[str, object] | None:
        """Read any persisted signal attempt in a Shanghai calendar day, regardless of status."""

        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT job_id, state_json FROM strategy_jobs
                WHERE operation IN ('market_strategy_pool','market_strategy_pool_tracking')
                  AND json_extract_string(request_json, '$.payload.mode') = 'signals'
                  AND created_at >= ? AND created_at < ?
                ORDER BY created_at DESC, job_id DESC LIMIT 1
                """,
                [start, end],
            ).fetchone()
        if row is None:
            return None
        return {"job_id": str(row[0]), "status": _load_object(row[1]).get("status")}

    def pool_signal_days(self, *, days: int = POOL_SIGNAL_HISTORY_DAYS) -> list[dict[str, object]]:
        """有信号结果可看的交易日：每个交易日取最后一次成功扫描，最多 ``days`` 天。

        给的是 ``as_of_date``（信号判定的那一天），不是任务提交时间——同一个交易日里跑的
        多轮扫描都归到同一天，页面上选到的日期和「买卖规则在那一天收盘后判定」是同一件事。
        日期取自提交时的 payload（回放的目标日），不解析结果本身：结果一份约 2 MB，全表
        2.58 GB，线上按结果解析会直接把查询内存吃爆。

        列表只收交易日：未来日期与周末是旧版本写入或本地导入留下的脏行，放进下拉会被
        当成「最新」那一天，连带把默认视图和后续刷新都指错日子。
        """

        today = shanghai_now().date()
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                WITH candidates AS (
                    SELECT job_id,
                        TRY_CAST(
                            json_extract_string(request_json, '$.payload.as_of_date') AS DATE
                        ) AS signal_date,
                        updated_at
                    FROM strategy_jobs
                    WHERE operation = 'market_strategy_pool_tracking'
                      AND status = 'completed'
                      AND result_json IS NOT NULL
                      AND json_extract_string(request_json, '$.payload.mode') = 'signals'
                      AND json_extract_string(request_json, '$.payload.market') IS NULL
                ), ranked AS (
                    SELECT job_id, signal_date,
                        DENSE_RANK() OVER (ORDER BY signal_date DESC NULLS LAST) AS day_rank,
                        ROW_NUMBER() OVER (
                            PARTITION BY signal_date ORDER BY updated_at DESC, job_id DESC
                        ) AS within_day
                    FROM candidates
                    WHERE signal_date IS NOT NULL
                      AND signal_date <= ?
                      AND isodow(signal_date) BETWEEN 1 AND 5
                )
                SELECT signal_date, job_id FROM ranked
                WHERE within_day = 1 AND day_rank <= ?
                ORDER BY signal_date DESC
                """,
                [today, days],
            ).fetchall()
        return [
            {
                "as_of_date": row[0].isoformat() if hasattr(row[0], "isoformat") else str(row[0]),
                "job_id": str(row[1]),
            }
            for row in rows
        ]

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
                "SELECT COUNT(*) FROM strategy_jobs WHERE operation "
                "IN ('market_strategy_pool','market_strategy_pool_tracking')"
            ).fetchone()
            # 跟踪轮询每十分钟就写一条记录，纯按时间倒序的第一页很快全是
            # tracking 记录，跑了几小时的全市场研究会被挤出窗口。前端靠这份
            # 列表恢复运行中任务的进度，找不到就整个进度面板都不渲染。所以
            # 第一页先把非终态任务全部带上，剩余名额再按时间补终态历史。
            rows: list[tuple[object, ...]] = []
            history_limit = limit
            if offset == 0:
                active_rows = connection.execute(
                    """
                    SELECT job_id, request_json, state_json
                    FROM strategy_jobs WHERE operation
                    IN ('market_strategy_pool','market_strategy_pool_tracking')
                    AND status IN ('queued', 'running')
                    ORDER BY created_at DESC, job_id DESC LIMIT ?
                    """,
                    [limit],
                ).fetchall()
                rows.extend(active_rows)
                history_limit = limit - len(active_rows)
            if history_limit > 0:
                terminal_filter = " AND status NOT IN ('queued', 'running')" if offset == 0 else ""
                rows.extend(
                    connection.execute(
                        """
                        SELECT job_id, request_json, state_json
                        FROM strategy_jobs WHERE operation
                        IN ('market_strategy_pool','market_strategy_pool_tracking')
                        """
                        + terminal_filter
                        + """
                        ORDER BY created_at DESC, job_id DESC LIMIT ? OFFSET ?
                        """,
                        [history_limit, offset],
                    ).fetchall()
                )
        items = [
            _pool_history_summary(str(job_id), _load_object(raw_request), _load_object(raw_state))
            for job_id, raw_request, raw_state in rows
        ]
        return {
            "items": items,
            "total": int(total_row[0]) if total_row else 0,
            "limit": limit,
            "offset": offset,
        }

    def market_pool_job_stats(self) -> dict[str, object]:
        """Average and median durations of recent pool tasks, grouped by mode."""

        with self._read_connection() as connection:
            return _pool_mode_stats(connection)

    def claim_next(
        self, worker_id: str | None = None, operations: list[str] | None = None
    ) -> str | None:
        selected_worker = _validate_worker_id(worker_id or "local-worker")
        self.recover_stale_running(stale_after_seconds=DEFAULT_STALE_AFTER_SECONDS)
        now = time.time()
        with _WRITE_LOCK, self._transaction() as connection:
            operation_clause = (
                (" AND operation IN (" + ",".join("?" for _ in operations) + ")")
                if operations
                else ""
            )
            # 先把"认领响应丢失"的那一次重放出去：worker 只在空闲时才 claim，所以绑在它
            # 名下、状态还是 running 且**从未续租**的任务必然是后端已提交认领、响应没回到
            # worker 的那一次。不先把它交回去，这个任务就永远没人执行（后续 claim 只找 queued），
            # 最后被 stale 扫描判成「worker 已停止」，用户看到的是一次几分钟的空等加一次手动重试。
            # 只认"从未续租"的：同一个容器里可能跑着多个共用 hostname 的 worker 进程，正在被
            # 正常执行的任务（已经续过租）不能重复发给第二个进程。已被要求取消的也不重放——
            # 它注定要结束，占着一次 claim 会把队列里真正的任务饿住（取消由 stale 回收退回队列
            # 后，领到它的人按普通任务一样收尾）。
            replay_rows = connection.execute(
                "SELECT job_id, state_json FROM strategy_jobs "
                "WHERE status='running' AND worker_id=? AND NOT cancel_requested"
                + operation_clause
                + " ORDER BY heartbeat_at,job_id",
                [selected_worker, *(operations or [])],
            ).fetchall()
            for replay_row in replay_rows:
                replay_state = _load_object(replay_row[1])
                if replay_state.get("renewed_at") is None:
                    return str(replay_row[0])
            row = connection.execute(
                "SELECT job_id,state_json FROM strategy_jobs WHERE status='queued'"
                + operation_clause
                + " ORDER BY created_at,job_id LIMIT 1",
                operations or [],
            ).fetchone()
            if row is None:
                return None
            job_id = str(row[0])
            state = _load_object(row[1])
            # ``claimed_at`` 记下这次认领的时刻；``renewed_at`` 只有在 worker 真的回来说过话
            # （心跳或进度）才会被填上。stale 回收靠「认领后从未续租」区分"响应丢在网络上"
            # 的孤儿任务与"跑一半 worker 死了"的任务，前者退回队列，后者照旧判死。
            state.update(
                status="running",
                phase="running",
                heartbeat_at=now,
                claimed_at=now,
                renewed_at=None,
            )
            if _optional_float(state.get("started_at")) is None:
                # 认领时刻就是研究起点：created_at 起算会把排队等 worker 的时间
                # 也算进任务时长，自动跟踪任务堆在全市场研究后面时能虚高几小时。
                state["started_at"] = now
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
                requeues = int(_number(state.get("claim_requeues"), 0.0))
                # 认领后一次都没续租：这不是"跑一半死了"，而是认领的响应丢在网络上——后端已经
                # 把任务标成 running 并绑了 worker_id，worker 却没拿到 job_id，于是这个任务永远
                # 没人执行（后续 claim 只找 queued），到点被判死，用户白等几分钟再手动重提。
                # 退回队列让任何 worker 重新领走；超过上限才判死，避免网络持续故障时的无限循环。
                if state.get("renewed_at") is None and requeues < MAX_CLAIM_REQUEUES:
                    state.update(
                        status="queued",
                        phase="queued",
                        claimed_at=None,
                        heartbeat_at=now,
                        finished_at=None,
                        claim_requeues=requeues + 1,
                        note="上一次认领的响应没有回到 worker，任务已退回队列重新排队",
                    )
                    connection.execute(
                        "UPDATE strategy_jobs SET status = 'queued', worker_id = NULL, "
                        "state_json = ?, heartbeat_at = ?, updated_at = ? "
                        "WHERE job_id = ? AND status = 'running'",
                        [_dump_json(state), now, now, job_id],
                    )
                    recovered.append(job_id)
                    continue
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
            # 能走到这一行就说明 worker 还活着、并且在做这个任务：把续租痕迹写进状态，
            # stale 回收据此区分"真的开始跑过"与"认领响应根本没回到 worker"。
            self._save_state(connection, job_id, row, {**fields, "renewed_at": now})
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
            cancel_requested = bool(row[2])
            final_status = "cancelled" if cancel_requested else "completed"
            state.update(
                status=final_status,
                phase=final_status,
                cancel_requested=cancel_requested,
                error=None,
                error_code=None,
                finished_at=state.get("finished_at") or now,
            )
            if not cancel_requested:
                state["progress"] = 1.0
            connection.execute(
                """
                UPDATE strategy_jobs
                SET status = ?, state_json = ?, result_json = ?, updated_at = ?
                WHERE job_id = ?
                """,
                [
                    final_status,
                    _dump_json(state),
                    None if cancel_requested else _dump_json(result),
                    now,
                    job_id,
                ],
            )
        if final_status == "completed":
            self._prune_signal_results(job_id)
        return self.read_state(job_id) if worker_id is not None else None

    def _prune_signal_results(
        self, keep_job_id: str, *, days: int = POOL_SIGNAL_HISTORY_DAYS
    ) -> int:
        """把每个交易日重复的信号结果压到只剩最后一次，并且只留最近 ``days`` 个交易日。

        只摘 ``result_json``：任务行、状态与耗时统计全部留着，历史列表和平均值不受影响。
        ``keep_job_id`` 是刚写完的那一条，无论它落在哪个档位都保住，不会自裁。
        """

        with _WRITE_LOCK, self._transaction() as connection:
            rows = connection.execute(
                """
                WITH candidates AS (
                    -- 交易日从提交时的 payload 读：那里写的就是这次回放的目标日，而且只有
                    -- 几十字节。早先版本这里解析 result_json（一份约 2 MB、全表 2.58 GB），
                    -- 线上第一次查询就把 DuckDB 的内存吃爆了（500）。
                    SELECT job_id,
                        TRY_CAST(
                            json_extract_string(request_json, '$.payload.as_of_date') AS DATE
                        ) AS signal_date,
                        updated_at
                    FROM strategy_jobs
                    WHERE operation = 'market_strategy_pool_tracking'
                      AND status = 'completed'
                      AND result_json IS NOT NULL
                      AND json_extract_string(request_json, '$.payload.mode') = 'signals'
                      -- 只裁全市场那一份：个股级增量结果才几 KB，而且当天面板要把它们合并
                      -- 到全市场结果上，裁掉就把最新一天的显示弄坏了。
                      AND json_extract_string(request_json, '$.payload.market') IS NULL
                ), ranked AS (
                    SELECT job_id, signal_date,
                        DENSE_RANK() OVER (ORDER BY signal_date DESC NULLS LAST) AS day_rank,
                        ROW_NUMBER() OVER (
                            PARTITION BY signal_date ORDER BY updated_at DESC, job_id DESC
                        ) AS within_day,
                        ROW_NUMBER() OVER (ORDER BY updated_at DESC, job_id DESC) AS newest_rank
                    FROM candidates
                )
                UPDATE strategy_jobs AS jobs
                SET result_json = NULL
                FROM ranked
                WHERE jobs.job_id = ranked.job_id
                  AND jobs.job_id <> ?
                  -- 最新那一条永远不裁：当天面板就靠它（它也刚被写进来，这道判断是兜底）。
                  AND ranked.newest_rank > 1
                  -- 解析不出目标日的记录不进列表也不裁（日期为空时 day_rank 也是空，
                  -- 下面的比较不成立），宁可留着也不误伤当天那份。
                  AND (ranked.day_rank > ? OR ranked.within_day > 1)
                RETURNING jobs.job_id
                """,
                [keep_job_id, days],
            ).fetchall()
        return len(rows)

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
        elif status == "running":
            if merged.get("started_at") is None:
                merged["started_at"] = now
            if "heartbeat_at" not in updates:
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
        """读策略只有一份：市场库的 `read_session`（只读优先 + 被自己打回就退回读写）。

        以前这里只重试、不退路：研究进程占着库时，池表读能退回读写继续服务，而任务
        表读等满预算直接抛错——同一个锁压力下两种行为。
        """

        with self._store.read_session() as connection:
            yield connection

    @contextmanager
    def _transaction(self) -> Iterator[duckdb.DuckDBPyConnection]:
        with self._store.write_transaction() as connection:
            yield connection


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


def _pool_history_summary(
    job_id: str,
    request: Mapping[str, object],
    state: Mapping[str, object],
) -> dict[str, object]:
    """Flatten one pool task row into the fields the task history list shows."""

    payload = _mapping_or_empty(request.get("payload"))
    progress = _mapping_or_empty(state.get("metadata"))
    completed = _optional_int(progress.get("completed_symbols")) or 0
    skipped = _optional_int(progress.get("skipped_symbols")) or 0
    started_at = _optional_float(state.get("started_at"))
    # started_at 由 worker 认领时写入；更早入库的任务没有它，只能从创建时间起算，
    # 于是时长里含排队。duration_includes_queue 让页面能把这个口径说清楚。
    duration = _duration_between(
        started_at if started_at is not None else _optional_float(state.get("created_at")),
        _optional_float(state.get("finished_at")),
    )
    return {
        "job_id": job_id,
        "status": state.get("status"),
        "mode": payload.get("mode", "full"),
        "rebuild": payload.get("rebuild") is True,
        "automatic": payload.get("automatic") is True,
        "dual_objectives": payload.get("mode") == "signals"
        and request.get("operation") == "market_strategy_pool_tracking",
        "objective": payload.get("objective"),
        "market": payload.get("market"),
        "code": payload.get("code"),
        "created_at": state.get("created_at"),
        "started_at": started_at,
        "finished_at": state.get("finished_at"),
        "duration_seconds": duration,
        "duration_includes_queue": started_at is None,
        "completed_symbols": completed,
        "skipped_symbols": skipped,
        "failed_symbols": _optional_int(progress.get("failed_symbols")) or 0,
        # completed_symbols 把复用标的也算进去了，真正研究了几个要自己减。
        "newly_researched_symbols": max(0, completed - skipped),
        "total_symbols": _optional_int(progress.get("total_symbols")) or 0,
        "maximum_parallelism": _optional_int(progress.get("maximum_parallelism")),
        "phase_label": progress.get("phase_label"),
        "error": state.get("error"),
        "error_code": state.get("error_code"),
    }


def _pool_mode_stats(connection: duckdb.DuckDBPyConnection) -> dict[str, object]:
    """Summarize recent terminal pool tasks per mode without loading every result blob."""

    modes: dict[str, object] = {}
    for mode in POOL_STATS_MODES:
        rows = connection.execute(
            """
            SELECT status, created_at, updated_at,
                   TRY_CAST(json_extract_string(state_json, '$.started_at') AS DOUBLE),
                   TRY_CAST(json_extract_string(state_json, '$.finished_at') AS DOUBLE),
                   TRY_CAST(
                       json_extract_string(state_json, '$.metadata.completed_symbols') AS BIGINT),
                   TRY_CAST(
                       json_extract_string(state_json, '$.metadata.skipped_symbols') AS BIGINT),
                   TRY_CAST(
                       json_extract_string(state_json, '$.metadata.maximum_parallelism') AS BIGINT)
            FROM strategy_jobs
            WHERE operation IN ('market_strategy_pool', 'market_strategy_pool_tracking')
              AND status IN ('completed', 'failed', 'cancelled')
              AND json_extract_string(request_json, '$.payload.mode') = ?
            ORDER BY created_at DESC, job_id DESC
            LIMIT ?
            """,
            [mode, POOL_STATS_SAMPLE_LIMIT],
        ).fetchall()
        durations: list[float] = []
        started_samples = 0
        per_symbol: list[float] = []
        per_symbol_weights: list[int] = []
        parallelism: list[float] = []
        for (
            status,
            created_at,
            updated_at,
            raw_started_at,
            raw_finished_at,
            raw_completed,
            raw_skipped,
            raw_parallelism,
        ) in rows:
            started = _optional_float(raw_started_at)
            # 终态行的 updated_at 就是写下终态的那一刻；早期从 JSON 文件导入的行没有 finished_at。
            finished = _optional_float(raw_finished_at)
            if finished is None:
                finished = _optional_float(updated_at)
            elapsed = _duration_between(
                started if started is not None else _optional_float(created_at), finished
            )
            terminal = str(status)
            if terminal == "completed" and elapsed is not None:
                durations.append(elapsed)
                if started is not None:
                    started_samples += 1
            # 被取消的轮次只跑了一半，总时长没有意义，但「每只新研究标的花了多久」
            # 仍然是这一轮吞吐量的实测值，所以放进吞吐量样本而不是时长样本。
            # 失败任务两者都不进：它可能在第一只标的就断了，样本没有代表性。
            if terminal not in {"completed", "cancelled"}:
                continue
            workers = _optional_int(raw_parallelism)
            if workers:
                parallelism.append(float(workers))
            researched = (_optional_int(raw_completed) or 0) - (_optional_int(raw_skipped) or 0)
            if elapsed is not None and researched > 0:
                per_symbol.append(elapsed / researched)
                # 按这一轮真正研究的只数加权：只研究 1 只的小任务会把「平均每只」
                # 拉成整轮墙钟，全市场重建的真实边际被它盖掉。
                per_symbol_weights.append(researched)
        modes[mode] = {
            "samples": len(durations),
            "avg_duration_seconds": _round_mean(durations),
            "median_duration_seconds": _round_median(durations),
            "started_at_samples": started_samples,
            "throughput_samples": len(per_symbol),
            "avg_seconds_per_symbol": _round_weighted_mean(per_symbol, per_symbol_weights),
            "median_seconds_per_symbol": _round_weighted_median(per_symbol, per_symbol_weights),
            "avg_parallelism": _round_mean(parallelism),
        }
    return {"sample_limit": POOL_STATS_SAMPLE_LIMIT, "modes": modes}


def _round_mean(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 1)


def _round_median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return round(float(statistics.median(values)), 1)


def _round_weighted_mean(values: Sequence[float], weights: Sequence[int]) -> float | None:
    total = sum(weights)
    if not values or total <= 0:
        return None
    return round(sum(value * weight for value, weight in zip(values, weights)) / total, 1)


def _round_weighted_median(values: Sequence[float], weights: Sequence[int]) -> float | None:
    """按权重取中位：每个任务的比值按它研究的只数计入。"""

    total = sum(weights)
    if not values or total <= 0:
        return None
    cumulative = 0
    for value, weight in sorted(zip(values, weights)):
        cumulative += weight
        if cumulative * 2 >= total:
            return round(float(value), 1)
    return None


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
