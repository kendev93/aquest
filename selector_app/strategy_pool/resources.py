"""Runtime memory budgeting for bounded per-symbol strategy research."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path

from selector_app.concurrency import MAX_WORKERS, visible_cpu_count

MiB = 1024**2
GiB = 1024**3
MIN_MEMORY_HEADROOM_BYTES = 384 * MiB
MEMORY_HEADROOM_RATIO = 0.20
DEFAULT_POOL_WORKERS = 6
BASE_TICKER_WORKING_SET_BYTES = 256 * MiB
CPU_HIGH_PRESSURE_RATIO = 0.90
CPU_ELEVATED_PRESSURE_RATIO = 0.75


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
    """Return a conservative CPU ceiling before applying the memory budget."""

    raw = os.getenv("SELECTOR_STRATEGY_POOL_MAX_WORKERS", str(DEFAULT_POOL_WORKERS)).strip()
    try:
        requested = int(raw)
    except ValueError as exc:
        raise ValueError("全市场策略池最大 worker 数必须是整数") from exc
    if not 1 <= requested <= MAX_WORKERS:
        raise ValueError(f"全市场策略池最大 worker 数必须在 1 到 {MAX_WORKERS} 之间")
    return min(requested, visible_cpu_count())


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

    One visible CPU is kept available for the OS and interactive work. Load average
    is a pressure signal rather than a CPU percentage, so it only throttles dispatch
    near saturation and never cancels already-running work.
    """

    if visible_cpus < 1 or maximum_workers < 1 or active_workers < 0:
        raise ValueError("全市场策略池 CPU 并发估算参数无效")
    cpu_ceiling = min(maximum_workers, max(1, visible_cpus - 1))
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
) -> int:
    """Return a safe total active-worker target, preserving running work under pressure."""

    if estimated_worker_bytes <= 0 or maximum_workers < 1 or active_workers < 0:
        raise ValueError("全市场策略池并发估算参数无效")
    if status.available_bytes is None:
        return min(maximum_workers, max(1, active_workers))
    headroom = MIN_MEMORY_HEADROOM_BYTES
    if status.total_bytes is not None:
        headroom = max(headroom, int(status.total_bytes * MEMORY_HEADROOM_RATIO))
    budget = max(0, status.available_bytes - headroom)
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
    "cpu_bounded_worker_count",
    "estimate_ticker_working_set_bytes",
    "get_cpu_load_average_1m",
    "memory_bounded_worker_count",
    "memory_status",
    "pool_worker_cap",
]
