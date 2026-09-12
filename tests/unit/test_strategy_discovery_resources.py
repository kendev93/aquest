from __future__ import annotations

import pytest

from selector_app.strategy_discovery.resources import (
    DEFAULT_DISCOVERY_WORKERS,
    estimate_discovery_resources,
    resolve_discovery_workers,
)


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


def test_worker_configuration_defaults_to_two_and_validates_values(monkeypatch) -> None:
    monkeypatch.delenv("SELECTOR_DISCOVERY_WORKERS", raising=False)
    assert resolve_discovery_workers() == DEFAULT_DISCOVERY_WORKERS == 2

    monkeypatch.setenv("SELECTOR_DISCOVERY_WORKERS", "1")
    assert resolve_discovery_workers() == 1

    monkeypatch.setenv("SELECTOR_DISCOVERY_WORKERS", "0")
    with pytest.raises(ValueError, match="worker"):
        resolve_discovery_workers()
