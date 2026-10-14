from __future__ import annotations

import os
from pathlib import Path

from aquest.concurrency import (
    _cgroup_v1_cpu_quota,
    _cgroup_v2_cpu_quota,
    visible_cpu_count,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_cgroup_v1_quota_reads_the_cfs_pair(tmp_path: Path) -> None:
    """cgroup v1 的配额是 cpu.cfs_quota_us + cpu.cfs_period_us 两个文件。

    以前只找 ``cpu/cpu.max``（v1 里根本不存在），v1 容器内配额被完全忽略、worker 池
    按宿主核数放大——正是这个模块要防的过订阅。
    """

    _write(tmp_path / "cpu" / "cpu.cfs_quota_us", "150000\n")
    _write(tmp_path / "cpu" / "cpu.cfs_period_us", "100000\n")
    assert _cgroup_v1_cpu_quota(tmp_path) == 2  # 1.5 核向上取整


def test_cgroup_v1_quota_treats_minus_one_as_unlimited(tmp_path: Path) -> None:
    _write(tmp_path / "cpu" / "cpu.cfs_quota_us", "-1\n")
    _write(tmp_path / "cpu" / "cpu.cfs_period_us", "100000\n")
    assert _cgroup_v1_cpu_quota(tmp_path) is None


def test_cgroup_v1_quota_is_none_without_the_controller(tmp_path: Path) -> None:
    assert _cgroup_v1_cpu_quota(tmp_path) is None


def test_cgroup_v2_quota_reads_cpu_max(tmp_path: Path) -> None:
    _write(tmp_path / "cpu.max", "400000 100000\n")
    assert _cgroup_v2_cpu_quota(tmp_path) == 4

    _write(tmp_path / "cpu.max", "max 100000\n")
    assert _cgroup_v2_cpu_quota(tmp_path) is None


def test_visible_cpu_count_prefers_process_affinity(monkeypatch) -> None:
    """cpuset/taskset 限制下必须按允许的核心数，而不是宿主核数。"""

    monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: {0, 1}, raising=False)
    monkeypatch.setattr("aquest.concurrency._cgroup_cpu_quota", lambda: 8)

    assert visible_cpu_count() == 2


def test_visible_cpu_count_falls_back_to_cgroup_quota(monkeypatch) -> None:
    monkeypatch.delattr(os, "sched_getaffinity", raising=False)
    monkeypatch.setattr("aquest.concurrency._cgroup_cpu_quota", lambda: 3)

    assert visible_cpu_count() == 3


def test_visible_cpu_count_falls_back_to_cpu_count(monkeypatch) -> None:
    monkeypatch.delattr(os, "sched_getaffinity", raising=False)
    monkeypatch.setattr("aquest.concurrency._cgroup_cpu_quota", lambda: None)
    monkeypatch.setattr(os, "cpu_count", lambda: 6)

    assert visible_cpu_count() == 6


def test_visible_cpu_count_is_at_least_one(monkeypatch) -> None:
    monkeypatch.delattr(os, "sched_getaffinity", raising=False)
    monkeypatch.setattr("aquest.concurrency._cgroup_cpu_quota", lambda: None)
    monkeypatch.setattr(os, "cpu_count", lambda: None)

    assert visible_cpu_count() == 1


def test_visible_cpu_count_honors_quota_even_with_a_larger_affinity(monkeypatch) -> None:
    monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: set(range(32)), raising=False)
    monkeypatch.setattr("aquest.concurrency._cgroup_cpu_quota", lambda: 2)
    assert visible_cpu_count() == 2
