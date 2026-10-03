"""Shared concurrency limits for local, network, and derived-data jobs."""

from __future__ import annotations

import os
from pathlib import Path

MAX_WORKERS = 8


def visible_cpu_count() -> int:
    """CPUs this process may actually use, not just what the host reports.

    Under ``docker --cpuset-cpus`` / ``taskset`` / cgroup CPU quotas
    ``os.cpu_count()`` still reports every host core, so worker pools sized from
    it oversubscribe a constrained container while the memory pre-flight check
    (which *is* cgroup-aware) happily allows it.
    """

    # ``os.sched_getaffinity`` 只在 Linux 存在（Windows/macOS 没有）。这里用 getattr
    # 而不是直接访问 + 行内忽略注释：直接访问时 mypy 在 macOS 需要忽略、在 Linux
    # （CI）又会因 strict 模式的 warn_unused_ignores 报"多余"，无法同时满足。
    quota = _cgroup_cpu_quota()
    affinity = getattr(os, "sched_getaffinity", None)
    if affinity is not None:
        try:
            allowed = max(1, len(affinity(0)))
            return min(allowed, quota) if quota is not None else allowed
        except OSError:  # pragma: no cover - 取决于容器/procfs 权限
            pass
    if quota is not None:
        return quota
    return max(1, int(os.cpu_count() or 1))


def _cgroup_cpu_quota() -> int | None:
    """Read the v2 ``cpu.max`` quota, rounded up to whole CPUs."""

    for path in (Path("/sys/fs/cgroup/cpu.max"), Path("/sys/fs/cgroup/cpu/cpu.max")):
        try:
            raw = path.read_text(encoding="utf-8").split()
        except OSError:
            continue
        if len(raw) != 2:
            continue
        try:
            quota = int(raw[0])
            period = int(raw[1])
        except ValueError:
            continue
        if quota <= 0 or period <= 0:  # "max" or unset
            continue
        return max(1, -(-quota // period))
    return None
