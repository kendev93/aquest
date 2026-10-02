from __future__ import annotations

from selector_app.strategy_pool.resources import (
    GiB,
    MemoryStatus,
    estimate_ticker_working_set_bytes,
    memory_bounded_worker_count,
)


def test_ticker_memory_estimate_scales_with_bars_and_formula_catalog_size() -> None:
    small = estimate_ticker_working_set_bytes(1_000, value_count=80, signal_count=30)
    large = estimate_ticker_working_set_bytes(8_000, value_count=180, signal_count=70)

    assert large > small


def test_concurrency_uses_detected_remaining_memory_and_keeps_safety_headroom() -> None:
    status = MemoryStatus(total_bytes=8 * GiB, available_bytes=5 * GiB, source="cgroup")

    workers = memory_bounded_worker_count(
        status=status,
        estimated_worker_bytes=1 * GiB,
        maximum_workers=8,
    )

    assert workers == 3


def test_scheduler_pauses_new_work_under_memory_pressure_but_never_stalls_empty_pool() -> None:
    status = MemoryStatus(total_bytes=4 * GiB, available_bytes=256 * 1024**2, source="cgroup")
    options = {
        "status": status,
        "estimated_worker_bytes": 512 * 1024**2,
        "maximum_workers": 4,
    }

    assert memory_bounded_worker_count(**options, active_workers=2) == 2
    assert memory_bounded_worker_count(**options, active_workers=0) == 1
