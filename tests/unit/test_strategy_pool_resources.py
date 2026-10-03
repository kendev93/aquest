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

    assert (
        memory_bounded_worker_count(
            status=status,
            estimated_worker_bytes=512 * 1024**2,
            maximum_workers=4,
            active_workers=2,
        )
        == 2
    )
    assert (
        memory_bounded_worker_count(
            status=status,
            estimated_worker_bytes=512 * 1024**2,
            maximum_workers=4,
            active_workers=0,
        )
        == 1
    )


def test_pool_worker_cap_defaults_to_one_and_respects_cpu_affinity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SELECTOR_STRATEGY_POOL_MAX_WORKERS", raising=False)
    monkeypatch.setattr("selector_app.strategy_pool.resources.visible_cpu_count", lambda: 8)
    assert pool_worker_cap() == 1

    monkeypatch.setenv("SELECTOR_STRATEGY_POOL_MAX_WORKERS", "8")
    assert pool_worker_cap() == 8

    monkeypatch.setattr("selector_app.strategy_pool.resources.visible_cpu_count", lambda: 4)
    assert pool_worker_cap() == 4

    monkeypatch.setattr("selector_app.strategy_pool.resources.visible_cpu_count", lambda: 1)
    assert pool_worker_cap() == 1


def test_cpu_budget_allows_full_cpu_use_without_a_fixed_core_limit() -> None:
    assert (
        cpu_bounded_worker_count(
            load_average_1m=0.0,
            visible_cpus=8,
            maximum_workers=6,
        )
        == 6
    )
    assert (
        cpu_bounded_worker_count(
            load_average_1m=0.0,
            visible_cpus=2,
            maximum_workers=8,
        )
        == 2
    )


def test_cpu_pressure_stops_dispatching_new_symbols_without_cancelling_active_work() -> None:
    assert (
        cpu_bounded_worker_count(
            load_average_1m=10.5,
            visible_cpus=8,
            maximum_workers=6,
            active_workers=4,
        )
        == 5
    )
    assert (
        cpu_bounded_worker_count(
            load_average_1m=12.5,
            visible_cpus=8,
            maximum_workers=6,
            active_workers=4,
        )
        == 4
    )


def test_cpu_load_reader_falls_back_when_platform_does_not_expose_load_average(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable() -> tuple[float, float, float]:
        raise OSError("unsupported")

    monkeypatch.setattr("selector_app.strategy_pool.resources.os.getloadavg", unavailable)
    assert get_cpu_load_average_1m() is None


def test_inner_evaluation_workers_share_the_configured_total_budget(monkeypatch) -> None:
    monkeypatch.setattr(
        "selector_app.strategy_pool.resources.memory_status",
        lambda: MemoryStatus(16 * GiB, 16 * GiB, "test"),
    )
    from selector_app.strategy_pool.resources import pool_evaluation_worker_count

    monkeypatch.setattr("selector_app.strategy_pool.resources.visible_cpu_count", lambda: 8)
    monkeypatch.setenv("SELECTOR_DISCOVERY_WORKERS", "8")
    monkeypatch.setenv("SELECTOR_STRATEGY_POOL_MAX_WORKERS", "1")
    assert pool_evaluation_worker_count() == 8
    monkeypatch.setenv("SELECTOR_STRATEGY_POOL_MAX_WORKERS", "2")
    assert pool_evaluation_worker_count() == 8
    monkeypatch.setenv("SELECTOR_DISCOVERY_WORKERS", "1")
    assert pool_evaluation_worker_count() == 1


def test_auto_stock_concurrency_has_no_fixed_two_or_eight_limit(monkeypatch) -> None:
    monkeypatch.setenv("SELECTOR_STRATEGY_POOL_MAX_WORKERS", "auto")
    monkeypatch.setattr("selector_app.strategy_pool.resources.visible_cpu_count", lambda: 32)
    assert pool_worker_cap() == 32


def test_pool_ramp_starts_one_and_adds_only_one_after_observation() -> None:
    from selector_app.strategy_pool.resources import PoolConcurrencyRamp

    ramp = PoolConcurrencyRamp(interval_seconds=30)
    assert ramp.target(resource_limit=12, active_workers=0, now=0) == 1
    assert ramp.target(resource_limit=12, active_workers=1, now=29) == 1
    assert ramp.target(resource_limit=12, active_workers=1, now=30) == 2
    assert ramp.target(resource_limit=12, active_workers=2, now=30) == 2
    assert ramp.target(resource_limit=12, active_workers=2, now=60) == 3
    assert ramp.target(resource_limit=3, active_workers=3, now=90) == 3
    assert ramp.target(resource_limit=12, active_workers=3, now=119) == 3
    assert ramp.target(resource_limit=12, active_workers=3, now=120) == 4


def test_pool_ramp_resets_growth_after_pressure_without_interrupting_active_work() -> None:
    from selector_app.strategy_pool.resources import PoolConcurrencyRamp

    ramp = PoolConcurrencyRamp(interval_seconds=30)
    ramp.target(resource_limit=10, active_workers=0, now=0)
    assert ramp.target(resource_limit=10, active_workers=1, now=30) == 2
    assert ramp.target(resource_limit=1, active_workers=2, now=40) == 2
    assert ramp.target(resource_limit=10, active_workers=2, now=69) == 2
    assert ramp.target(resource_limit=10, active_workers=2, now=70) == 3


def test_shared_evaluation_budget_is_derived_from_cpu_and_memory_not_fixed_eight(
    monkeypatch,
) -> None:
    from selector_app.strategy_pool.resources import pool_evaluation_budget_capacity

    monkeypatch.setenv("SELECTOR_DISCOVERY_WORKERS", "auto")
    monkeypatch.setattr("selector_app.strategy_pool.resources.visible_cpu_count", lambda: 64)
    monkeypatch.setattr(
        "selector_app.strategy_pool.resources.memory_status",
        lambda: MemoryStatus(64 * GiB, 60 * GiB, "test"),
    )
    assert pool_evaluation_budget_capacity() == 64
    monkeypatch.setattr(
        "selector_app.strategy_pool.resources.memory_status",
        lambda: MemoryStatus(4 * GiB, 4 * GiB, "test"),
    )
    assert pool_evaluation_budget_capacity() == 12


def test_learned_stock_memory_excludes_shared_evaluation_process_reservation() -> None:
    from selector_app.strategy_pool.resources import PoolConcurrencyRamp

    ramp = PoolConcurrencyRamp()
    ramp.observe_memory(MemoryStatus(16 * GiB, 12 * GiB, "test"), 0)
    ramp.observe_memory(MemoryStatus(16 * GiB, 9 * GiB, "test"), 2, shared_worker_bytes=GiB)
    assert ramp.observed_worker_bytes == GiB
    ramp.observe_memory(MemoryStatus(16 * GiB, 11 * GiB, "test"), 2)
    assert ramp.observed_worker_bytes == GiB


def test_process_budget_does_not_double_charge_memory_already_used_by_leases(monkeypatch) -> None:
    from selector_app.strategy_pool.resources import pool_evaluation_budget_capacity

    monkeypatch.setenv("SELECTOR_DISCOVERY_WORKERS", "auto")
    monkeypatch.setattr("selector_app.strategy_pool.resources.visible_cpu_count", lambda: 64)
    monkeypatch.setattr(
        "selector_app.strategy_pool.resources.memory_status",
        lambda: MemoryStatus(4 * GiB, 4 * GiB, "test"),
    )
    before = pool_evaluation_budget_capacity()
    monkeypatch.setattr(
        "selector_app.strategy_pool.resources.memory_status",
        lambda: MemoryStatus(4 * GiB, 3 * GiB, "test"),
    )
    assert pool_evaluation_budget_capacity(in_use_workers=8) == before
