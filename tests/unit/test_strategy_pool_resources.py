from __future__ import annotations

import pytest

from selector_app.strategy_pool.resources import (
    GiB,
    MemoryStatus,
    cpu_bounded_worker_count,
    estimate_ticker_working_set_bytes,
    get_cpu_load_average_1m,
    memory_bounded_worker_count,
    pool_worker_cap,
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

    assert memory_bounded_worker_count(
        status=status,
        estimated_worker_bytes=512 * 1024**2,
        maximum_workers=4,
        active_workers=2,
    ) == 2
    assert memory_bounded_worker_count(
        status=status,
        estimated_worker_bytes=512 * 1024**2,
        maximum_workers=4,
        active_workers=0,
    ) == 1


def test_pool_worker_cap_defaults_to_six_and_respects_cpu_affinity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SELECTOR_STRATEGY_POOL_MAX_WORKERS", raising=False)
    monkeypatch.setattr("selector_app.strategy_pool.resources.visible_cpu_count", lambda: 8)
    assert pool_worker_cap() == 6

    monkeypatch.setenv("SELECTOR_STRATEGY_POOL_MAX_WORKERS", "8")
    assert pool_worker_cap() == 8

    monkeypatch.setattr("selector_app.strategy_pool.resources.visible_cpu_count", lambda: 4)
    assert pool_worker_cap() == 4


def test_cpu_budget_uses_idle_capacity_but_reserves_one_core() -> None:
    assert cpu_bounded_worker_count(
        load_average_1m=0.0,
        visible_cpus=8,
        maximum_workers=6,
    ) == 6
    assert cpu_bounded_worker_count(
        load_average_1m=0.0,
        visible_cpus=2,
        maximum_workers=8,
    ) == 1


def test_cpu_pressure_stops_dispatching_new_symbols_without_cancelling_active_work() -> None:
    assert cpu_bounded_worker_count(
        load_average_1m=6.5,
        visible_cpus=8,
        maximum_workers=6,
        active_workers=4,
    ) == 5
    assert cpu_bounded_worker_count(
        load_average_1m=7.5,
        visible_cpus=8,
        maximum_workers=6,
        active_workers=4,
    ) == 4


def test_cpu_load_reader_falls_back_when_platform_does_not_expose_load_average(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable() -> tuple[float, float, float]:
        raise OSError("unsupported")

    monkeypatch.setattr("selector_app.strategy_pool.resources.os.getloadavg", unavailable)
    assert get_cpu_load_average_1m() is None
