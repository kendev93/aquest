"""Memory-aware resource planning for strategy discovery jobs."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from aquest.concurrency import MAX_WORKERS, visible_cpu_count

AUTO_DISCOVERY_WORKERS = "auto"
DEFAULT_DISCOVERY_WORKERS = MAX_WORKERS
DEFAULT_MEMORY_BUDGET_BYTES = 4 * 1024**3
MEMORY_HEADROOM_RATIO = 0.8
_BYTES_PER_FLOAT64 = 8
_SNAPSHOT_BAR_ARRAYS = 5
_SNAPSHOT_BOOL_ARRAYS = 2
_SNAPSHOT_BUILD_OVERHEAD_BYTES = 512 * 1024**2
_MIN_WORKER_OVERHEAD_BYTES = 128 * 1024**2


@dataclass(frozen=True)
class DiscoveryResourceEstimate:
    """Conservative memory estimate for one strategy discovery run."""

    row_count: int
    value_count: int
    signal_count: int
    requested_workers: int
    selected_workers: int
    snapshot_bytes: int
    worker_overhead_bytes: int
    estimated_peak_bytes: int
    memory_budget_bytes: int
    within_budget: bool
    reason: str


class DiscoveryResourceBudgetError(ValueError):
    """Raised when even one discovery worker exceeds the memory budget."""

    def __init__(self, estimate: DiscoveryResourceEstimate) -> None:
        self.estimate = estimate
        super().__init__(
            "策略实验预计需要 "
            f"{estimate.estimated_peak_bytes / 1024**3:.1f} GiB，"
            f"当前预算仅 {estimate.memory_budget_bytes / 1024**3:.1f} GiB；"
            "请缩小证券范围或日期范围后重试"
        )


def resolve_discovery_workers() -> int:
    """Resolve the configured maximum worker count or an automatic CPU cap."""

    raw = os.getenv("AQUEST_DISCOVERY_WORKERS", "1").strip()
    if raw.lower() in {"", AUTO_DISCOVERY_WORKERS}:
        return _auto_worker_count()
    try:
        workers = int(raw)
    except ValueError as exc:
        raise ValueError("strategy discovery worker 数必须是整数") from exc
    return validate_discovery_workers(workers)


def validate_discovery_workers(workers: int) -> int:
    """Validate an explicitly supplied discovery worker count."""

    if isinstance(workers, bool) or not 1 <= workers <= MAX_WORKERS:
        raise ValueError(f"strategy discovery worker 数必须在 1 到 {MAX_WORKERS} 之间")
    return workers


def resolve_discovery_memory_budget() -> int:
    """Return the configured or automatically detected memory budget."""

    raw_mb = os.getenv("AQUEST_DISCOVERY_MEMORY_BUDGET_MB")
    if raw_mb is not None and raw_mb.strip().lower() not in {"", "auto"}:
        try:
            budget_mb = int(raw_mb.strip())
        except ValueError as exc:
            raise ValueError("strategy discovery memory budget 必须是整数 MiB") from exc
        if budget_mb <= 0:
            raise ValueError("strategy discovery memory budget 必须大于 0")
        return budget_mb * 1024**2

    limit = _read_cgroup_memory_limit() or _read_host_memory_limit()
    if limit is not None:
        return max(1, int(limit * MEMORY_HEADROOM_RATIO))
    return DEFAULT_MEMORY_BUDGET_BYTES


def _snapshot_column_counts() -> tuple[int, int]:
    """Value and signal column counts the snapshot actually allocates.

    Derived from the catalog instead of hardcoded: the snapshot warms every
    numeric output (``value_ids``), not just the rankable subset, and a stale
    constant under-estimates the per-row cost by ~40%, letting oversized runs
    past the pre-flight check.
    """

    from aquest.strategy_discovery.catalog import default_indicator_catalog

    catalog = default_indicator_catalog()
    return len(catalog.value_ids), len(catalog.signal_ids)


def estimate_discovery_resources(
    row_count: int,
    *,
    value_count: int | None = None,
    signal_count: int | None = None,
    requested_workers: int | None = None,
    memory_budget_bytes: int | None = None,
) -> DiscoveryResourceEstimate:
    """Estimate a safe worker count before materializing a discovery snapshot.

    The snapshot layout is intentionally kept explicit here.  The estimate is
    conservative: it includes the on-disk float64/bool arrays, a fixed build
    overhead, and a bounded per-worker working-set allowance.  It is a guardrail
    rather than a replacement for runtime RSS measurements.
    """

    if value_count is None or signal_count is None:
        default_values, default_signals = _snapshot_column_counts()
        value_count = default_values if value_count is None else value_count
        signal_count = default_signals if signal_count is None else signal_count
    _require_non_negative("row_count", row_count)
    _require_non_negative("value_count", value_count)
    _require_non_negative("signal_count", signal_count)
    workers = resolve_discovery_workers() if requested_workers is None else requested_workers
    if not 1 <= workers <= MAX_WORKERS:
        raise ValueError(f"strategy discovery worker 数必须在 1 到 {MAX_WORKERS} 之间")
    budget = (
        resolve_discovery_memory_budget() if memory_budget_bytes is None else memory_budget_bytes
    )
    if budget <= 0:
        raise ValueError("strategy discovery memory budget 必须大于 0")

    snapshot_bytes = row_count * (
        (_SNAPSHOT_BAR_ARRAYS + value_count) * _BYTES_PER_FLOAT64
        + _BYTES_PER_FLOAT64
        + signal_count
        + _SNAPSHOT_BOOL_ARRAYS
    )
    worker_overhead = max(_MIN_WORKER_OVERHEAD_BYTES, int(snapshot_bytes * 0.15))

    selected_workers = 1
    for candidate_workers in range(workers, 0, -1):
        estimated_peak = _estimated_peak_bytes(
            snapshot_bytes,
            worker_overhead,
            candidate_workers,
        )
        if estimated_peak <= budget:
            selected_workers = candidate_workers
            break
    estimated_peak = _estimated_peak_bytes(snapshot_bytes, worker_overhead, selected_workers)
    within_budget = estimated_peak <= budget
    if not within_budget:
        reason = "resource_budget_exceeded"
    elif selected_workers < workers:
        reason = "worker_count_reduced"
    else:
        reason = "within_budget"
    return DiscoveryResourceEstimate(
        row_count=row_count,
        value_count=value_count,
        signal_count=signal_count,
        requested_workers=workers,
        selected_workers=selected_workers,
        snapshot_bytes=snapshot_bytes,
        worker_overhead_bytes=worker_overhead,
        estimated_peak_bytes=estimated_peak,
        memory_budget_bytes=budget,
        within_budget=within_budget,
        reason=reason,
    )


def _estimated_peak_bytes(snapshot_bytes: int, worker_overhead: int, workers: int) -> int:
    return snapshot_bytes + _SNAPSHOT_BUILD_OVERHEAD_BYTES + worker_overhead * workers


def _require_non_negative(name: str, value: int) -> None:
    if isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} 必须是非负整数")


def _auto_worker_count() -> int:
    cpu_count = visible_cpu_count()
    return validate_discovery_workers(min(DEFAULT_DISCOVERY_WORKERS, cpu_count))


def _read_cgroup_memory_limit() -> int | None:
    for path in (
        Path("/sys/fs/cgroup/memory.max"),
        Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
    ):
        try:
            raw = path.read_text(encoding="ascii").strip()
        except (FileNotFoundError, OSError):
            continue
        if not raw or raw == "max":
            continue
        try:
            limit = int(raw)
        except ValueError:
            continue
        if limit > 0 and limit < 1 << 60:
            return limit
    return None


def _read_host_memory_limit() -> int | None:
    try:
        pages = int(os.sysconf("SC_PHYS_PAGES"))
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
    except (AttributeError, OSError, ValueError):
        return None
    total = pages * page_size
    return total if total > 0 else None


__all__ = [
    "AUTO_DISCOVERY_WORKERS",
    "DEFAULT_DISCOVERY_WORKERS",
    "DiscoveryResourceBudgetError",
    "DiscoveryResourceEstimate",
    "estimate_discovery_resources",
    "resolve_discovery_memory_budget",
    "resolve_discovery_workers",
    "validate_discovery_workers",
]
