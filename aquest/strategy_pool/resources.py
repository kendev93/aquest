"""Runtime memory budgeting for bounded per-symbol strategy research."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path

from aquest.concurrency import visible_cpu_count
from aquest.strategy_discovery.resources import resolve_discovery_workers

MiB = 1024**2
GiB = 1024**3
MIN_MEMORY_HEADROOM_BYTES = 384 * MiB
MEMORY_HEADROOM_RATIO = 0.20
DEFAULT_POOL_WORKERS = "1"
POOL_RAMP_INTERVAL_SECONDS = 30.0
BASE_TICKER_WORKING_SET_BYTES = 512 * MiB
EVALUATION_WORKER_MEMORY_BYTES = 128 * MiB
CPU_HIGH_PRESSURE_RATIO = 1.50
CPU_ELEVATED_PRESSURE_RATIO = 1.25


@dataclass(frozen=True)
class MemoryStatus:
    total_bytes: int | None
    available_bytes: int | None
    source: str


def memory_status() -> MemoryStatus:
    """Read the memory ceiling and remaining bytes visible to this process."""

    cgroup_limit = _read_number(
        (Path("/sys/fs/cgroup/memory.max"), Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"))
    )
    cgroup_current = _read_number(
        (Path("/sys/fs/cgroup/memory.current"), Path("/sys/fs/cgroup/memory/memory.usage_in_bytes"))
    )
    if cgroup_limit is not None and cgroup_limit < 1 << 60:
        current = cgroup_current or 0
        return MemoryStatus(
            total_bytes=cgroup_limit,
            available_bytes=max(0, cgroup_limit - current),
            source="cgroup",
        )

    host_memory = _read_meminfo()
    if host_memory is not None:
        return MemoryStatus(
            total_bytes=host_memory[0],
            available_bytes=host_memory[1],
            source="host",
        )

    try:
        pages = int(os.sysconf("SC_PHYS_PAGES"))
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        total = pages * page_size
    except (AttributeError, OSError, ValueError):
        return MemoryStatus(None, None, "unknown")
    return MemoryStatus(total, total // 2, "fallback-estimate")


def pool_worker_cap() -> int:
    """Derive the stock ceiling from visible CPUs, with an optional user override."""

    cpus = visible_cpu_count()
    raw = os.getenv("AQUEST_STRATEGY_POOL_MAX_WORKERS", DEFAULT_POOL_WORKERS).strip()
    if raw.lower() in {"", "auto"}:
        return cpus
    try:
        requested = int(raw)
    except ValueError as exc:
        raise ValueError("全市场策略池并发上限必须是 auto 或正整数") from exc
    if requested < 1:
        raise ValueError("全市场策略池并发上限必须是 auto 或正整数")
    return min(requested, cpus)


def pool_evaluation_budget_capacity(in_use_workers: int = 0) -> int:
    raw = os.getenv("AQUEST_DISCOVERY_WORKERS", "1").strip().lower()
    cpu_budget = (
        visible_cpu_count()
        if raw in {"", "auto"}
        else min(visible_cpu_count(), resolve_discovery_workers())
    )
    status = memory_status()
    if status.available_bytes is None:
        return 1
    headroom = max(
        MIN_MEMORY_HEADROOM_BYTES,
        int((status.total_bytes or 0) * MEMORY_HEADROOM_RATIO),
    )
    # Reserve half of usable memory for parent stock research and snapshots.
    process_memory = (
        max(0, status.available_bytes - headroom + in_use_workers * EVALUATION_WORKER_MEMORY_BYTES)
        // 2
    )
    return max(1, min(cpu_budget, process_memory // EVALUATION_WORKER_MEMORY_BYTES))


def pool_evaluation_worker_count() -> int:
    """Per-stock request; actual process counts are leased from a shared budget."""

    return resolve_discovery_workers()


class PoolConcurrencyRamp:
    """Grow one slot per observation interval; pressure resets the growth timer."""

    def __init__(self, *, interval_seconds: float = POOL_RAMP_INTERVAL_SECONDS) -> None:
        if interval_seconds < 0:
            raise ValueError("并发观察间隔不能小于 0")
        self.interval_seconds = interval_seconds
        self._target = 1
        self._next_growth: float | None = None
        self._initial_available: int | None = None
        self.observed_worker_bytes = 0

    def observe_memory(
        self, status: MemoryStatus, active_workers: int, shared_worker_bytes: int = 0
    ) -> None:
        if status.available_bytes is None:
            return
        if self._initial_available is None:
            self._initial_available = status.available_bytes
        if active_workers:
            growth = max(0, self._initial_available - status.available_bytes - shared_worker_bytes)
            self.observed_worker_bytes = max(self.observed_worker_bytes, growth // active_workers)

    def target(self, *, resource_limit: int, active_workers: int, now: float) -> int:
        limit = max(1, active_workers, resource_limit)
        if self._next_growth is None:
            self._next_growth = now + self.interval_seconds
            return max(active_workers, self._target)
        if resource_limit <= self._target:
            self._target = min(self._target, limit)
            self._next_growth = now + self.interval_seconds
        elif now >= self._next_growth:
            self._target = min(limit, self._target + 1)
            self._next_growth = now + self.interval_seconds
        return max(active_workers, self._target)


def get_cpu_load_average_1m() -> float | None:
    """Return the system one-minute load average when the platform exposes it."""

    get_load_average = getattr(os, "getloadavg", None)
    if get_load_average is None:
        return None
    try:
        load_average = float(get_load_average()[0])
    except (OSError, ValueError, TypeError, IndexError):
        return None
    return load_average if math.isfinite(load_average) and load_average >= 0 else None


def cpu_bounded_worker_count(
    *,
    load_average_1m: float | None,
    visible_cpus: int,
    maximum_workers: int,
    active_workers: int = 0,
) -> int:
    """Limit dispatches when system load is high, without interrupting active tickers.

    Full CPU utilization is expected. Only sustained load materially above the
    visible CPU count holds new dispatches; running stocks are never cancelled.
    """

    if visible_cpus < 1 or maximum_workers < 1 or active_workers < 0:
        raise ValueError("全市场策略池 CPU 并发估算参数无效")
    cpu_ceiling = min(maximum_workers, visible_cpus)
    if load_average_1m is None or not math.isfinite(load_average_1m):
        return max(active_workers, cpu_ceiling)
    if load_average_1m < 0:
        raise ValueError("全市场策略池 CPU 负载不能小于 0")

    pressure = load_average_1m / visible_cpus
    if pressure >= CPU_HIGH_PRESSURE_RATIO:
        target_workers = active_workers
    elif pressure >= CPU_ELEVATED_PRESSURE_RATIO:
        target_workers = active_workers + 1
    else:
        target_workers = cpu_ceiling
    return max(1, active_workers, min(cpu_ceiling, target_workers))


def estimate_ticker_working_set_bytes(
    bar_count: int,
    *,
    value_count: int,
    signal_count: int,
) -> int:
    """Conservative per-symbol allowance for bars, features, labels, and temporaries."""

    if min(bar_count, value_count, signal_count) < 0:
        raise ValueError("全市场策略池资源估算输入不能小于 0")
    bytes_per_bar = value_count * 32 + signal_count * 16 + 2_048
    return BASE_TICKER_WORKING_SET_BYTES + bar_count * bytes_per_bar


def memory_bounded_worker_count(
    *,
    status: MemoryStatus,
    estimated_worker_bytes: int,
    maximum_workers: int,
    active_workers: int = 0,
    reserved_bytes: int = 0,
) -> int:
    """Return a safe total active-worker target, preserving running work under pressure."""

    if estimated_worker_bytes <= 0 or maximum_workers < 1 or active_workers < 0:
        raise ValueError("全市场策略池并发估算参数无效")
    if status.available_bytes is None:
        return min(maximum_workers, max(1, active_workers))
    headroom = MIN_MEMORY_HEADROOM_BYTES
    if status.total_bytes is not None:
        headroom = max(headroom, int(status.total_bytes * MEMORY_HEADROOM_RATIO))
    budget = max(0, status.available_bytes - headroom - reserved_bytes)
    additional_workers = budget // estimated_worker_bytes
    target = active_workers + int(additional_workers)
    if target <= active_workers:
        target = max(1, active_workers)
    return min(maximum_workers, target)


def _read_number(paths: tuple[Path, ...]) -> int | None:
    for path in paths:
        try:
            raw = path.read_text(encoding="ascii").strip()
        except OSError:
            continue
        if raw in {"", "max"}:
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        if value > 0:
            return value
    return None


def _read_meminfo() -> tuple[int, int] | None:
    try:
        lines = Path("/proc/meminfo").read_text(encoding="ascii").splitlines()
    except OSError:
        return None
    values: dict[str, int] = {}
    for line in lines:
        key, separator, remainder = line.partition(":")
        if not separator:
            continue
        parts = remainder.split()
        if not parts:
            continue
        try:
            values[key] = int(parts[0]) * 1024
        except ValueError:
            continue
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if total is None or available is None:
        return None
    return total, available


__all__ = [
    "MemoryStatus",
    "PoolConcurrencyRamp",
    "pool_evaluation_budget_capacity",
    "pool_evaluation_worker_count",
    "cpu_bounded_worker_count",
    "estimate_ticker_working_set_bytes",
    "get_cpu_load_average_1m",
    "memory_bounded_worker_count",
    "memory_status",
    "pool_worker_cap",
]
