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
DEFAULT_BARS_PER_STOCK = 1_250

# 实测（独立进程，1,490 / 4,434 / 8,622 根 K 线）：worker 峰值稳定在 195-205 MB，
# 主要来自解释器与 pandas/numpy/duckdb 底座，与 K 线数几乎无关；每根 K 线只增加
# 约 2 KB 的行情与指标数组。
WORKER_BASE_BYTES = 224 * 1024**2
WORKER_BYTES_PER_BAR = 2 * 1024

# 父进程要攒到汇总阶段才能算指标，这部分随股票数、候选数和成交数增长。
# 实测（618 只、10 候选）：峰值 339 MB，其中约 240 MB 随规模增长。
PARENT_RUNTIME_BASE_BYTES = 160 * 1024**2
PARENT_BYTES_PER_STOCK = 384 * 1024
PARENT_BYTES_PER_STOCK_CANDIDATE = 8 * 1024


@dataclass(frozen=True)
class ResearchWorkerPlan:
    """Resolved worker count for one condition-research run."""

    requested_workers: int
    selected_workers: int
    worker_peak_bytes: int
    memory_budget_bytes: int
    reason: str


@dataclass(frozen=True)
class ResearchResourceEstimate:
    """Data-aware memory estimate for one condition-research run.

    Worker memory is nearly flat in the universe size (one stock at a time),
    while the parent accumulates every stock's compact reports and mining
    samples until summaries run, so the parent term is what scales with data.
    """

    stock_count: int
    candidates_per_stock: int
    bars_per_stock: int
    requested_workers: int
    selected_workers: int
    worker_bytes: int
    parent_bytes: int
    estimated_peak_bytes: int
    memory_budget_bytes: int
    within_budget: bool
    reason: str


class ResearchResourceBudgetError(ValueError):
    """Raised when even one worker cannot run inside the memory budget."""

    def __init__(self, estimate: ResearchResourceEstimate) -> None:
        self.estimate = estimate
        super().__init__(
            "买卖条件研究预计需要 "
            f"{estimate.estimated_peak_bytes / 1024**3:.1f} GiB 内存"
            f"（研究进程 {estimate.parent_bytes / 1024**3:.1f} GiB + "
            f"1 个 worker {estimate.worker_bytes / 1024**2:.0f} MiB），"
            f"当前预算仅 {estimate.memory_budget_bytes / 1024**3:.1f} GiB；"
            "请缩小市场或板块范围、减少候选数量，或调高 "
            "SELECTOR_CONDITION_RESEARCH_MEMORY_BUDGET_MB"
        )


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


def estimate_research_resources(
    stock_count: int,
    *,
    candidates_per_stock: int,
    bars_per_stock: int = DEFAULT_BARS_PER_STOCK,
    requested_workers: int | None = None,
    memory_budget_bytes: int | None = None,
) -> ResearchResourceEstimate:
    """Estimate peak memory and pick the largest worker count that fits."""

    _require_non_negative("stock_count", stock_count)
    _require_non_negative("candidates_per_stock", candidates_per_stock)
    _require_non_negative("bars_per_stock", bars_per_stock)
    requested = resolve_research_workers() if requested_workers is None else requested_workers
    if not 1 <= requested <= MAX_WORKERS:
        raise ValueError(f"买卖条件研究 worker 数必须在 1 到 {MAX_WORKERS} 之间")
    budget = (
        resolve_research_memory_budget() if memory_budget_bytes is None else memory_budget_bytes
    )
    if budget <= 0:
        raise ValueError("买卖条件研究内存预算必须大于 0")

    worker_bytes = WORKER_BASE_BYTES + bars_per_stock * WORKER_BYTES_PER_BAR
    parent_bytes = PARENT_RUNTIME_BASE_BYTES + stock_count * (
        PARENT_BYTES_PER_STOCK + candidates_per_stock * PARENT_BYTES_PER_STOCK_CANDIDATE
    )

    selected = 1
    for candidate_workers in range(requested, 0, -1):
        if parent_bytes + candidate_workers * worker_bytes <= budget:
            selected = candidate_workers
            break
    estimated_peak = parent_bytes + selected * worker_bytes
    within_budget = estimated_peak <= budget
    if not within_budget:
        reason = (
            f"预算 {budget / 1024**2:.0f} MiB 连单进程都放不下"
            f"（预计 {estimated_peak / 1024**2:.0f} MiB）"
        )
    elif selected < requested:
        reason = (
            f"预算 {budget / 1024**2:.0f} MiB 只允许 {selected} 个 worker"
            f"（研究进程占 {parent_bytes / 1024**2:.0f} MiB），低于请求的 {requested}"
        )
    else:
        reason = f"按请求使用 {selected} 个 worker"
    return ResearchResourceEstimate(
        stock_count=stock_count,
        candidates_per_stock=candidates_per_stock,
        bars_per_stock=bars_per_stock,
        requested_workers=requested,
        selected_workers=selected,
        worker_bytes=worker_bytes,
        parent_bytes=parent_bytes,
        estimated_peak_bytes=estimated_peak,
        memory_budget_bytes=budget,
        within_budget=within_budget,
        reason=reason,
    )


def plan_research_workers(
    requested_workers: int | None = None,
    *,
    memory_budget_bytes: int | None = None,
) -> ResearchWorkerPlan:
    """Data-free default used before the universe is known.

    ``run`` re-plans with :func:`estimate_research_resources` once the stock
    count and candidate count are available.
    """

    requested = resolve_research_workers() if requested_workers is None else requested_workers
    if not 1 <= requested <= MAX_WORKERS:
        raise ValueError(f"买卖条件研究 worker 数必须在 1 到 {MAX_WORKERS} 之间")
    budget = (
        resolve_research_memory_budget() if memory_budget_bytes is None else memory_budget_bytes
    )
    if budget <= 0:
        raise ValueError("买卖条件研究内存预算必须大于 0")

    worker_bytes = WORKER_BASE_BYTES
    parent_bytes = PARENT_RUNTIME_BASE_BYTES
    selected = 1
    for candidate_workers in range(requested, 0, -1):
        if parent_bytes + candidate_workers * worker_bytes <= budget:
            selected = candidate_workers
            break
    estimated_peak = parent_bytes + selected * worker_bytes
    if estimated_peak > budget:
        reason = (
            f"预算 {budget / 1024**2:.0f} MiB 连单进程都放不下，退化为单进程"
            if requested > 1
            else "已请求单进程执行"
        )
    elif selected < requested:
        reason = (
            f"预算 {budget / 1024**2:.0f} MiB 只允许 {selected} 个 worker，低于请求的 {requested}"
        )
    else:
        reason = f"按请求使用 {selected} 个 worker"
    return ResearchWorkerPlan(
        requested_workers=requested,
        selected_workers=selected,
        worker_peak_bytes=worker_bytes,
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


def _require_non_negative(name: str, value: int) -> None:
    if isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} 不能为负数")


__all__ = [
    "AUTO_RESEARCH_WORKERS",
    "ResearchResourceBudgetError",
    "ResearchResourceEstimate",
    "ResearchWorkerPlan",
    "estimate_research_resources",
    "plan_research_workers",
    "resolve_research_memory_budget",
    "resolve_research_workers",
    "validate_research_workers",
]
