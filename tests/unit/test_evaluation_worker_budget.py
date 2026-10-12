from __future__ import annotations

import pytest

from aquest.screening.jobs import TaskCancelledError
from aquest.strategy_discovery.evaluation_budget import EvaluationWorkerBudget


def test_evaluation_budget_shares_processes_and_releases_leases_on_errors() -> None:
    budget = EvaluationWorkerBudget(8)
    with budget.stock():
        with budget.lease(8) as first:
            assert first == 8
            assert budget.available_workers == 0
        with budget.stock():
            with budget.lease(8) as first, budget.lease(8) as second:
                assert (first, second) == (4, 4)
                assert budget.available_workers == 0
            with pytest.raises(RuntimeError), budget.lease(8):
                raise RuntimeError("failed simulation")
            assert budget.available_workers == 8
    assert budget.available_workers == 8


def test_waiting_for_evaluation_capacity_is_cancellable() -> None:
    budget = EvaluationWorkerBudget(1)
    with budget.stock(), budget.lease(1):
        with pytest.raises(TaskCancelledError), budget.lease(1, abort_check=lambda: True):
            pytest.fail("must not enter cancelled work")
    with budget.lease(1) as workers:
        assert workers == 1


def test_discovery_executor_uses_shared_lease_and_restores_requested_workers(
    tmp_path, monkeypatch
) -> None:
    from aquest.market_data.store import DuckDbMarketDataStore
    from aquest.strategy_discovery.service import StrategyDiscoveryService

    created = []

    class FakeExecutor:
        def __init__(self, **kwargs):
            created.append(kwargs["max_workers"])

        def shutdown(self, **kwargs):
            assert kwargs == {"wait": True, "cancel_futures": True}

    monkeypatch.setattr("aquest.strategy_discovery.service.ProcessPoolExecutor", FakeExecutor)
    budget = EvaluationWorkerBudget(4)
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    first = StrategyDiscoveryService(
        store=store, evaluation_workers=8, evaluation_worker_budget=budget
    )
    second = StrategyDiscoveryService(
        store=store, evaluation_workers=8, evaluation_worker_budget=budget
    )
    with budget.stock(), budget.stock():
        with first._evaluation_executor(), second._evaluation_executor():
            assert created == [2, 2]
            assert first._evaluation_workers == second._evaluation_workers == 2
            assert budget.available_workers == 0
    assert first._evaluation_workers == second._evaluation_workers == 8
    assert budget.available_workers == 4


def test_executor_start_failure_does_not_leak_shared_process_capacity(
    tmp_path, monkeypatch
) -> None:
    from aquest.market_data.store import DuckDbMarketDataStore
    from aquest.strategy_discovery.service import StrategyDiscoveryService

    def fail(**kwargs):
        raise OSError("failed to start process")

    monkeypatch.setattr("aquest.strategy_discovery.service.ProcessPoolExecutor", fail)
    budget = EvaluationWorkerBudget(2)
    service = StrategyDiscoveryService(
        store=DuckDbMarketDataStore(tmp_path / "market.duckdb"),
        evaluation_workers=8,
        evaluation_worker_budget=budget,
    )
    with pytest.raises(OSError), budget.stock(), service._evaluation_executor():
        pytest.fail("executor must not start")
    assert budget.available_workers == 2
    assert service._evaluation_workers == 8


def test_reduced_budget_drains_running_processes_before_granting_more() -> None:
    budget = EvaluationWorkerBudget(8)
    with budget.lease(8) as workers:
        assert workers == 8
        budget.set_capacity(2)
        assert budget.available_workers == 0
        assert budget.in_use_workers == 8
        with pytest.raises(TaskCancelledError), budget.lease(1, abort_check=lambda: True):
            pytest.fail("must not start under pressure")
    assert budget.available_workers == 2
    with budget.lease(8) as workers:
        assert workers == 2
    budget.set_capacity(16)
    assert budget.available_workers == 16
