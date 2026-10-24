"""Shared concurrency limits for local, network, and derived-data jobs."""

from __future__ import annotations

import os
from pathlib import Path

MAX_WORKERS = 8
#: 重计算任务的全局互斥组。全站共用同一个执行器，两个重活叠加时各自的 8 路池一起排队，
#: 内存预检却只按单个任务的规模算——跨任务族也要排成一条队。
HEAVY_RESEARCH_EXCLUSIVE_GROUP = "heavy-research"
_CGROUP_ROOT = Path("/sys/fs/cgroup")


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
    """CPU 配额：先 cgroup v2 的 ``cpu.max``，再退到 v1 的 cfs 两兄弟文件。"""

    return _cgroup_v2_cpu_quota(_CGROUP_ROOT) or _cgroup_v1_cpu_quota(_CGROUP_ROOT)


def _cpu_cores(quota: int | None, period: int | None) -> int | None:
    if quota is None or period is None or quota <= 0 or period <= 0:  # "max" 或未设置
        return None
    return max(1, -(-quota // period))


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _cgroup_v2_cpu_quota(root: Path) -> int | None:
    """v2：``cpu.max`` 一行两列（``<quota> <period>``，不限时是 ``max``）。"""

    for path in (root / "cpu.max", root / "cpu" / "cpu.max"):
        try:
            raw = path.read_text(encoding="utf-8").split()
        except OSError:
            continue
        if len(raw) != 2:
            continue
        try:
            cores = _cpu_cores(int(raw[0]), int(raw[1]))
        except ValueError:
            continue
        if cores is not None:
            return cores
    return None


def _cgroup_v1_cpu_quota(root: Path) -> int | None:
    """v1 没有 ``cpu/cpu.max``：配额拆在 ``cpu.cfs_quota_us``（-1 = 不限）与 ``cpu.cfs_period_us``。

    以前只找 ``cpu.max``，v1 容器里等于什么都没读到，worker 池按宿主核数放大——
    恰好是这个函数注释声称要防的过订阅。
    """

    return _cpu_cores(
        _read_int(root / "cpu" / "cpu.cfs_quota_us"),
        _read_int(root / "cpu" / "cpu.cfs_period_us"),
    )
