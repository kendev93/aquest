from __future__ import annotations

import pytest

from aquest.concurrency import MAX_WORKERS
from aquest.condition_research.resources import (
    PARENT_BYTES_PER_STOCK,
    PARENT_BYTES_PER_STOCK_CANDIDATE,
    WORKER_BYTES_PER_BAR,
    ResearchResourceBudgetError,
    estimate_research_resources,
    plan_research_workers,
    validate_research_workers,
)

GIB = 1024**3


def test_estimate_keeps_requested_workers_when_budget_allows() -> None:
    estimate = estimate_research_resources(
        600,
        candidates_per_stock=10,
        requested_workers=4,
        memory_budget_bytes=4 * GIB,
    )

    assert estimate.selected_workers == 4
    assert estimate.within_budget is True
    assert estimate.estimated_peak_bytes <= estimate.memory_budget_bytes


def test_estimate_degrades_when_budget_only_fits_part_of_the_workers() -> None:
    estimate = estimate_research_resources(
        5_000,
        candidates_per_stock=10,
        requested_workers=4,
        memory_budget_bytes=3 * GIB,
    )

    assert estimate.selected_workers == 2
    assert estimate.within_budget is True
    assert "只允许 2 个 worker" in estimate.reason


def test_estimate_refuses_a_universe_that_does_not_fit_even_one_worker() -> None:
    estimate = estimate_research_resources(
        5_000,
        candidates_per_stock=10,
        requested_workers=4,
        memory_budget_bytes=2 * GIB,
    )

    assert estimate.selected_workers == 1
    assert estimate.within_budget is False
    assert "连单进程都放不下" in estimate.reason


def test_budget_error_message_tells_the_operator_what_to_change() -> None:
    estimate = estimate_research_resources(
        5_000,
        candidates_per_stock=10,
        requested_workers=4,
        memory_budget_bytes=2 * GIB,
    )

    message = str(ResearchResourceBudgetError(estimate))

    assert "GiB" in message
    assert "AQUEST_CONDITION_RESEARCH_MEMORY_BUDGET_MB" in message


def test_worker_estimate_grows_with_bars_per_stock() -> None:
    def worker_bytes(bars: int) -> int:
        return estimate_research_resources(
            10,
            candidates_per_stock=0,
            bars_per_stock=bars,
            requested_workers=1,
            memory_budget_bytes=8 * GIB,
        ).worker_bytes

    assert worker_bytes(10_000) - worker_bytes(100) == (10_000 - 100) * WORKER_BYTES_PER_BAR


def test_parent_estimate_grows_with_stocks_and_candidates() -> None:
    def parent_bytes(stocks: int, candidates: int) -> int:
        return estimate_research_resources(
            stocks,
            candidates_per_stock=candidates,
            requested_workers=1,
            memory_budget_bytes=8 * GIB,
        ).parent_bytes

    baseline = parent_bytes(100, 0)
    assert parent_bytes(200, 0) - baseline == 100 * PARENT_BYTES_PER_STOCK
    assert parent_bytes(100, 10) - baseline == 100 * 10 * PARENT_BYTES_PER_STOCK_CANDIDATE


def test_estimate_rejects_negative_inputs() -> None:
    with pytest.raises(ValueError, match="stock_count"):
        estimate_research_resources(-1, candidates_per_stock=0)
    with pytest.raises(ValueError, match="candidates_per_stock"):
        estimate_research_resources(1, candidates_per_stock=-1)
    with pytest.raises(ValueError, match="内存预算"):
        estimate_research_resources(1, candidates_per_stock=0, memory_budget_bytes=0)


def test_data_free_plan_uses_cpu_cap_and_degrades_under_pressure() -> None:
    roomy = plan_research_workers(4, memory_budget_bytes=4 * GIB)
    tight = plan_research_workers(4, memory_budget_bytes=300 * 1024**2)

    assert roomy.selected_workers == 4
    assert tight.selected_workers == 1
    assert "退化为单进程" in tight.reason


def test_worker_count_validation_rejects_out_of_range_values() -> None:
    with pytest.raises(ValueError, match="worker 数"):
        validate_research_workers(0)
    with pytest.raises(ValueError, match="worker 数"):
        validate_research_workers(MAX_WORKERS + 1)
