"""Worker and memory planning for cross-stock buy/sell condition research."""

from __future__ import annotations

import os
from dataclasses import dataclass

from selector_app.concurrency import MAX_WORKERS
from selector_app.strategy_discovery.resources import (
    _read_cgroup_memory_limit,
    _read_host_memory_limit,
)

AUTO_RESEARCH_WORKERS = "auto"
DEFAULT_RESEARCH_WORKERS = MAX_WORKERS
DEFAULT_MEMORY_BUDGET_BYTES = 4 * 1024**3
MEMORY_HEADROOM_RATIO = 0.8
# 实测：一个研究 worker 处理单只股票的峰值 RSS 约 235 MB（近五年全市场库）。
WORKER_PEAK_BYTES = 256 * 1024**2


@dataclass(frozen=True)
class ResearchWorkerPlan:
    """Resolved worker count for one condition-research run."""

    requested_workers: int
    selected_workers: int
    worker_peak_bytes: int
    memory_budget_bytes: int
    reason: str


def resolve_research_workers() -> int:
    """Resolve the configured worker count or an automatic CPU-derived cap."""

    raw = os.getenv("SELECTOR_CONDITION_RESEARCH_WORKERS", AUTO_RESEARCH_WORKERS).strip()
    if raw.lower() in {"", AUTO_RESEARCH_WORKERS}:
        return _auto_worker_count()
    try:
        workers = int(raw)
    except ValueError as exc:
        raise ValueError("买卖条件研究 worker 数必须是整数") from exc
    return validate_research_workers(workers)


def validate_research_workers(workers: int) -> int:
    """Validate an explicitly supplied worker count."""

    if isinstance(workers, bool) or not 1 <= workers <= MAX_WORKERS:
        raise ValueError(f"买卖条件研究 worker 数必须在 1 到 {MAX_WORKERS} 之间")
    return workers


def resolve_research_memory_budget() -> int:
    """Return the configured or automatically detected memory budget."""

    raw_mb = os.getenv("SELECTOR_CONDITION_RESEARCH_MEMORY_BUDGET_MB")
    if raw_mb is not None and raw_mb.strip().lower() not in {"", AUTO_RESEARCH_WORKERS}:
        try:
            budget_mb = int(raw_mb.strip())
        except ValueError as exc:
            raise ValueError("买卖条件研究内存预算必须是整数 MiB") from exc
        if budget_mb <= 0:
            raise ValueError("买卖条件研究内存预算必须大于 0")
        return budget_mb * 1024**2

    limit = _read_cgroup_memory_limit() or _read_host_memory_limit()
    if limit is not None:
        return max(1, int(limit * MEMORY_HEADROOM_RATIO))
    return DEFAULT_MEMORY_BUDGET_BYTES


def plan_research_workers(
    requested_workers: int | None = None,
    *,
    memory_budget_bytes: int | None = None,
) -> ResearchWorkerPlan:
    """Pick a worker count that fits the memory budget.

    Each worker holds one stock's prepared bars and indicator arrays, so the
    guardrail is linear in the worker count rather than sized by the whole
    universe.
    """

    requested = resolve_research_workers() if requested_workers is None else requested_workers
    if not 1 <= requested <= MAX_WORKERS:
        raise ValueError(f"买卖条件研究 worker 数必须在 1 到 {MAX_WORKERS} 之间")
    budget = (
        resolve_research_memory_budget() if memory_budget_bytes is None else memory_budget_bytes
    )
    if budget <= 0:
        raise ValueError("买卖条件研究内存预算必须大于 0")

    selected = 1
    for candidate_workers in range(requested, 0, -1):
        if candidate_workers * WORKER_PEAK_BYTES <= budget:
            selected = candidate_workers
            break
    if selected <= 1:
        reason = (
            f"内存预算 {budget / 1024**2:.0f} MiB 不足以并行，退化为单进程"
            if requested > 1
            else "已请求单进程执行"
        )
    elif selected < requested:
        reason = (
            f"内存预算 {budget / 1024**2:.0f} MiB 只允许 {selected} 个 worker，"
            f"低于请求的 {requested}"
        )
    else:
        reason = f"按请求使用 {selected} 个 worker"
    return ResearchWorkerPlan(
        requested_workers=requested,
        selected_workers=selected,
        worker_peak_bytes=WORKER_PEAK_BYTES,
        memory_budget_bytes=budget,
        reason=reason,
    )


def _auto_worker_count() -> int:
    """Half the visible CPUs, capped by the shared worker limit.

    Measured on an 8-CPU host: four research workers finish the same batch in
    3.2s, eight take 4.0s. DuckDB already runs multi-threaded inside each
    worker, so matching the CPU count oversubscribes the machine.
    """

    try:
        visible = len(os.sched_getaffinity(0))  # type: ignore[attr-defined]
    except AttributeError:
        visible = os.cpu_count() or 1
    return max(1, min(MAX_WORKERS, visible // 2))


__all__ = [
    "AUTO_RESEARCH_WORKERS",
    "ResearchWorkerPlan",
    "WORKER_PEAK_BYTES",
    "plan_research_workers",
    "resolve_research_memory_budget",
    "resolve_research_workers",
    "validate_research_workers",
]
