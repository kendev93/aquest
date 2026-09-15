from __future__ import annotations

import pytest

from selector_app.concurrency import MAX_WORKERS
from selector_app.condition_research.resources import (
    WORKER_PEAK_BYTES,
    plan_research_workers,
    validate_research_workers,
)


def test_worker_plan_keeps_requested_workers_when_memory_allows() -> None:
    plan = plan_research_workers(4, memory_budget_bytes=4 * 1024**3)

    assert plan.requested_workers == 4
    assert plan.selected_workers == 4
    assert plan.worker_peak_bytes == WORKER_PEAK_BYTES


def test_worker_plan_degrades_to_single_process_under_memory_pressure() -> None:
    plan = plan_research_workers(4, memory_budget_bytes=300 * 1024**2)

    assert plan.selected_workers == 1
    assert "退化为单进程" in plan.reason


def test_worker_plan_reports_reduced_worker_count() -> None:
    plan = plan_research_workers(8, memory_budget_bytes=WORKER_PEAK_BYTES * 3)

    assert plan.selected_workers == 3
    assert "低于请求的 8" in plan.reason


def test_worker_count_validation_rejects_out_of_range_values() -> None:
    with pytest.raises(ValueError, match="worker 数"):
        validate_research_workers(0)
    with pytest.raises(ValueError, match="worker 数"):
        validate_research_workers(MAX_WORKERS + 1)


def test_worker_plan_rejects_non_positive_memory_budget() -> None:
    with pytest.raises(ValueError, match="内存预算"):
        plan_research_workers(2, memory_budget_bytes=0)
