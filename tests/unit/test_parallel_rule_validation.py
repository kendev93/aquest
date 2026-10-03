from concurrent.futures import Future

import pytest

from selector_app.individual_research.parallel_validation import evaluate_rule_pairs
from selector_app.screening.jobs import TaskCancelledError
from selector_app.strategy_discovery.evaluation_budget import EvaluationWorkerBudget


def test_rule_validation_limits_pending_work_and_closing_refunds_capacity(monkeypatch) -> None:
    submitted = []
    shutdown = []

    class FakeExecutor:
        def __init__(self, **kwargs):
            assert kwargs["max_workers"] == 2

        def submit(self, function, job):
            future = Future()
            if not submitted:
                future.set_result(({"result": "first"}, {}))
            submitted.append(future)
            return future

        def shutdown(self, **kwargs):
            shutdown.append(kwargs)

    monkeypatch.setattr(
        "selector_app.individual_research.parallel_validation.ProcessPoolExecutor", FakeExecutor
    )
    budget = EvaluationWorkerBudget(2)
    results = evaluate_rule_pairs(
        "unused", iter([("ref", {}, {})] * 10), budget=budget, requested_workers=8, abort_check=None
    )
    assert next(results) == ({"result": "first"}, {})
    assert len(submitted) == 2
    assert budget.available_workers == 0
    results.close()
    assert submitted[1].cancelled()
    assert shutdown == [{"wait": True, "cancel_futures": True}]
    assert budget.available_workers == 2


def test_rule_validation_cancellation_refunds_capacity(monkeypatch) -> None:
    pending = []
    checks = 0

    class FakeExecutor:
        def __init__(self, **kwargs):
            pass

        def submit(self, function, job):
            future = Future()
            pending.append(future)
            return future

        def shutdown(self, **kwargs):
            pass

    def cancelled():
        nonlocal checks
        checks += 1
        return checks > 1

    monkeypatch.setattr(
        "selector_app.individual_research.parallel_validation.ProcessPoolExecutor", FakeExecutor
    )
    budget = EvaluationWorkerBudget(2)
    with pytest.raises(TaskCancelledError):
        list(
            evaluate_rule_pairs(
                "unused",
                iter([("ref", {}, {})] * 10),
                budget=budget,
                requested_workers=2,
                abort_check=cancelled,
            )
        )
    assert len(pending) == 2
    assert all(future.cancelled() for future in pending)
    assert budget.available_workers == 2
