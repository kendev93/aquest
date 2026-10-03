"""Fair, cancellable process leases shared by concurrent stock research."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import Condition

from selector_app.screening.jobs import TaskCancelledError


class EvaluationWorkerBudget:
    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("评估进程预算必须大于 0")
        self.capacity = capacity
        self._available = capacity
        self._stocks = 0
        self._waiting: deque[object] = deque()
        self._condition = Condition()

    @property
    def available_workers(self) -> int:
        with self._condition:
            return max(0, self._available)

    @property
    def in_use_workers(self) -> int:
        with self._condition:
            return self.capacity - self._available

    def set_capacity(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("评估进程预算必须大于 0")
        with self._condition:
            # A negative balance drains existing leases before admitting new work.
            self._available += capacity - self.capacity
            self.capacity = capacity
            self._condition.notify_all()

    @contextmanager
    def stock(self) -> Iterator[None]:
        with self._condition:
            self._stocks += 1
        try:
            yield
        finally:
            with self._condition:
                self._stocks -= 1
                self._condition.notify_all()

    @contextmanager
    def lease(self, requested: int, abort_check: Callable[[], bool] | None = None) -> Iterator[int]:
        if requested < 1:
            raise ValueError("请求的评估进程数必须大于 0")
        ticket = object()
        allocated = 0
        with self._condition:
            self._waiting.append(ticket)
        try:
            while True:
                if abort_check is not None and abort_check():
                    raise TaskCancelledError("等待风险评估进程期间已取消")
                with self._condition:
                    if self._waiting[0] is ticket and self._available > 0:
                        share = max(1, self.capacity // max(1, self._stocks))
                        allocated = min(requested, share, self._available)
                        self._available -= allocated
                        self._waiting.popleft()
                        self._condition.notify_all()
                        break
                    self._condition.wait(timeout=0.25)
            yield allocated
        finally:
            with self._condition:
                if ticket in self._waiting:
                    self._waiting.remove(ticket)
                self._available += allocated
                self._condition.notify_all()
