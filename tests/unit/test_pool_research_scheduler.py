"""到期策略自动增量研究的调度单测。

调度器只在「有池子 + 有到期工作 + 没有任务在跑」时提交一次全市场增量任务；
研究口径、到期判定不在这里重写，直接复用 estimate_incremental。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from aquest.strategy_discovery.job_store import JobOperation
from aquest.strategy_pool.research_scheduler import PoolResearchScheduler


class _Store:
    def __init__(self, entries: int = 1, settings: Mapping[str, object] | None = None) -> None:
        self.entries = entries
        self.settings = settings

    def list_entries(self) -> list[object]:
        return [object()] * self.entries

    def read_research_settings(self) -> Mapping[str, object] | None:
        return self.settings


class _Runner:
    def __init__(self, active: bool = False) -> None:
        self.active = active

    def has_active_jobs(self, descriptions: Iterable[str] | None = None) -> bool:
        return self.active


class _Backend:
    def __init__(self, active: list[dict[str, object]] | None = None) -> None:
        self.active = active or []
        self.calls: list[dict[str, Any]] = []

    def submit(
        self,
        operation: JobOperation,
        payload: Mapping[str, object],
        *,
        description: str,
        worker_count: int | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> str:
        self.calls.append(
            dict(
                operation=operation,
                payload=dict(payload),
                description=description,
                worker_count=worker_count,
                metadata=dict(metadata or {}),
            )
        )
        return f"job-{len(self.calls)}"

    def active_snapshots(self) -> list[dict[str, object]]:
        return self.active


class _Service:
    def __init__(self, pending: int = 0, error: Exception | None = None) -> None:
        self.pending = pending
        self.error = error
        self.payloads: list[dict[str, object]] = []

    def estimate_incremental(self, payload: Mapping[str, object]) -> Mapping[str, object]:
        self.payloads.append(dict(payload))
        if self.error is not None:
            raise self.error
        return {"pending_symbols": self.pending}


def _scheduler(
    *,
    store: _Store | None = None,
    backend: _Backend | None = None,
    runner: _Runner | None = None,
    service: _Service | None = None,
    now: list[float] | None = None,
    **intervals: float,
) -> tuple[PoolResearchScheduler, _Service, _Backend, list[float]]:
    service = service or _Service()
    backend = backend or _Backend()
    clock = now if now is not None else [1_000.0]
    scheduler = PoolResearchScheduler(
        store or _Store(),
        backend,
        runner or _Runner(),
        lambda: service,
        clock=lambda: clock[0],
        **{
            "check_interval_seconds": 0.0,
            "min_interval_seconds": 0.0,
            "idle_interval_seconds": 0.0,
            "busy_retry_seconds": 0.0,
            "failure_retry_seconds": 0.0,
            **intervals,
        },
    )
    return scheduler, service, backend, clock


def test_submits_once_when_there_is_due_work() -> None:
    scheduler, service, backend, _ = _scheduler(service=_Service(pending=3))

    assert scheduler.submit_if_due() == "job-1"
    assert len(service.payloads) == 1
    call = backend.calls[0]
    assert call["operation"] == "market_strategy_pool"
    assert call["payload"]["mode"] == "full"
    assert call["payload"]["automatic"] is True
    assert call["payload"]["boards"] == ["main", "chinext", "star"]
    assert call["metadata"]["automatic"] is True


def test_uses_saved_research_scope() -> None:
    scheduler, service, backend, _ = _scheduler(
        store=_Store(settings={"boards": ["main"], "exclude_st": True}),
        service=_Service(pending=1),
    )

    assert scheduler.submit_if_due() == "job-1"
    assert service.payloads[0]["boards"] == ["main"]
    assert service.payloads[0]["exclude_st"] is True
    assert backend.calls[0]["payload"]["exclude_st"] is True


def test_skips_when_pool_is_empty_or_nothing_is_due() -> None:
    empty, service, backend, _ = _scheduler(store=_Store(entries=0), service=_Service(pending=5))
    assert empty.submit_if_due() is None
    assert service.payloads == []
    assert backend.calls == []

    idle, service, backend, _ = _scheduler(service=_Service(pending=0))
    assert idle.submit_if_due() is None
    assert service.payloads
    assert backend.calls == []


def test_skips_while_data_or_pool_jobs_are_running() -> None:
    busy_data, service, backend, _ = _scheduler(runner=_Runner(active=True), service=_Service(5))
    assert busy_data.submit_if_due() is None
    assert service.payloads == []
    assert backend.calls == []

    busy_pool, service, backend, _ = _scheduler(
        backend=_Backend(active=[{"operation": "market_strategy_pool_tracking"}]),
        service=_Service(5),
    )
    assert busy_pool.submit_if_due() is None
    assert service.payloads == []
    assert backend.calls == []


def test_submit_failure_backs_off_without_raising() -> None:
    class _BrokenBackend(_Backend):
        def submit(
            self,
            operation: JobOperation,
            payload: Mapping[str, object],
            *,
            description: str,
            worker_count: int | None = None,
            metadata: Mapping[str, object] | None = None,
        ) -> str:
            raise RuntimeError("db locked")

    clock = [3_000.0]
    scheduler, service, backend, _ = _scheduler(
        backend=_BrokenBackend(),
        service=_Service(pending=1),
        now=clock,
        failure_retry_seconds=3_600,
    )

    assert scheduler.submit_if_due() is None
    assert service.payloads
    clock[0] += 3_599
    assert scheduler.submit_if_due() is None


def test_respects_daily_interval_and_estimate_failure_backoff() -> None:
    now = [1_000.0]
    scheduler, service, backend, _ = _scheduler(
        service=_Service(pending=1), now=now, min_interval_seconds=86_400
    )
    assert scheduler.submit_if_due() == "job-1"
    now[0] += 86_399
    assert scheduler.submit_if_due() is None
    now[0] += 1
    assert scheduler.submit_if_due() == "job-2"

    failing, service, backend, clock = _scheduler(
        service=_Service(error=RuntimeError("db locked")), now=[2_000.0]
    )
    assert failing.submit_if_due() is None
    assert backend.calls == []
    clock[0] += 3_599
    assert failing.submit_if_due() is None
