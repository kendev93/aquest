from __future__ import annotations

import os

from selector_app.concurrency import visible_cpu_count


def test_visible_cpu_count_prefers_process_affinity(monkeypatch) -> None:
    """cpuset/taskset 限制下必须按允许的核心数，而不是宿主核数。"""

    monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: {0, 1}, raising=False)
    monkeypatch.setattr("selector_app.concurrency._cgroup_cpu_quota", lambda: 8)

    assert visible_cpu_count() == 2


def test_visible_cpu_count_falls_back_to_cgroup_quota(monkeypatch) -> None:
    monkeypatch.delattr(os, "sched_getaffinity", raising=False)
    monkeypatch.setattr("selector_app.concurrency._cgroup_cpu_quota", lambda: 3)

    assert visible_cpu_count() == 3


def test_visible_cpu_count_falls_back_to_cpu_count(monkeypatch) -> None:
    monkeypatch.delattr(os, "sched_getaffinity", raising=False)
    monkeypatch.setattr("selector_app.concurrency._cgroup_cpu_quota", lambda: None)
    monkeypatch.setattr(os, "cpu_count", lambda: 6)

    assert visible_cpu_count() == 6


def test_visible_cpu_count_is_at_least_one(monkeypatch) -> None:
    monkeypatch.delattr(os, "sched_getaffinity", raising=False)
    monkeypatch.setattr("selector_app.concurrency._cgroup_cpu_quota", lambda: None)
    monkeypatch.setattr(os, "cpu_count", lambda: None)

    assert visible_cpu_count() == 1
