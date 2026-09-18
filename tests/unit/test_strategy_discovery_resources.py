from __future__ import annotations

import pytest

from selector_app.strategy_discovery import resources
from selector_app.strategy_discovery.resources import (
    estimate_discovery_resources,
    resolve_discovery_memory_budget,
    resolve_discovery_workers,
)


def test_default_column_counts_follow_the_catalog_not_a_stale_constant() -> None:
    """快照列数写死会低估每行成本，预检会放过本该 422 拒绝的运行。"""

    from selector_app.strategy_discovery.catalog import default_indicator_catalog

    catalog = default_indicator_catalog()
    estimate = estimate_discovery_resources(
        row_count=1_000,
        requested_workers=2,
        memory_budget_bytes=10 * 1024**3,
    )

    assert estimate.value_count == len(catalog.value_ids)
    assert estimate.signal_count == len(catalog.signal_ids)


def test_snapshot_estimate_uses_the_current_float64_layout() -> None:
    estimate = estimate_discovery_resources(
        row_count=1_000,
        value_count=21,
        signal_count=15,
        requested_workers=2,
        memory_budget_bytes=10 * 1024**3,
    )

    assert estimate.snapshot_bytes == 233_000
    assert estimate.selected_workers == 2
    assert estimate.within_budget is True


def test_resource_plan_reduces_workers_to_fit_the_budget() -> None:
    estimate = estimate_discovery_resources(
        row_count=10_000_000,
        value_count=21,
        signal_count=15,
        requested_workers=8,
        memory_budget_bytes=5 * 1024**3,
    )

    assert estimate.requested_workers == 8
    assert 1 <= estimate.selected_workers < 8
    assert estimate.within_budget is True
    assert estimate.reason == "worker_count_reduced"


def test_resource_plan_rejects_data_that_does_not_fit_one_worker() -> None:
    estimate = estimate_discovery_resources(
        row_count=100_000_000,
        value_count=21,
        signal_count=15,
        requested_workers=2,
        memory_budget_bytes=1 * 1024**3,
    )

    assert estimate.selected_workers == 1
    assert estimate.within_budget is False
    assert estimate.reason == "resource_budget_exceeded"


def test_worker_configuration_auto_uses_the_available_cpu_count(monkeypatch) -> None:
    monkeypatch.delenv("SELECTOR_DISCOVERY_WORKERS", raising=False)
    monkeypatch.setattr(resources.os, "cpu_count", lambda: 4)
    assert resolve_discovery_workers() == 4

    monkeypatch.setenv("SELECTOR_DISCOVERY_WORKERS", "auto")
    assert resolve_discovery_workers() == 4

    monkeypatch.setenv("SELECTOR_DISCOVERY_WORKERS", "1")
    assert resolve_discovery_workers() == 1

    monkeypatch.setenv("SELECTOR_DISCOVERY_WORKERS", "0")
    with pytest.raises(ValueError, match="worker"):
        resolve_discovery_workers()


def test_auto_worker_request_expands_when_the_memory_budget_allows(monkeypatch) -> None:
    monkeypatch.setenv("SELECTOR_DISCOVERY_WORKERS", "auto")
    monkeypatch.setattr(resources.os, "cpu_count", lambda: 4)

    estimate = estimate_discovery_resources(
        row_count=1_000,
        requested_workers=None,
        memory_budget_bytes=10 * 1024**3,
    )

    assert estimate.requested_workers == 4
    assert estimate.selected_workers == 4


def test_auto_memory_budget_uses_the_worker_cgroup_limit(monkeypatch) -> None:
    monkeypatch.setenv("SELECTOR_DISCOVERY_MEMORY_BUDGET_MB", "auto")
    monkeypatch.setattr(resources, "_read_cgroup_memory_limit", lambda: 10 * 1024**3)

    assert resolve_discovery_memory_budget() == 8 * 1024**3
