from __future__ import annotations

import time
from threading import Event, Lock

import pytest

from selector_app.screening.jobs import (
    ActiveJobConflictError,
    ScreenJobRunner,
    TaskCancelledError,
    TaskUserError,
)


def wait_until_complete(runner: ScreenJobRunner, job_id: str) -> None:
    for _ in range(100):
        state = runner.get(job_id)
        if state is not None and state.status == "completed":
            return
        time.sleep(0.005)
    raise AssertionError("job did not finish")


def test_runner_evicts_old_completed_results_but_keeps_latest() -> None:
    runner = ScreenJobRunner(max_results=1)
    try:
        first = runner.submit_callable(lambda _progress: {"value": 1}, description="test")
        wait_until_complete(runner, first)
        second = runner.submit_callable(lambda _progress: {"value": 2}, description="test")
        wait_until_complete(runner, second)

        assert runner.get(first) is None
        assert runner.get(second) is not None
    finally:
        runner.shutdown()


def test_runner_preserves_safe_user_error_for_background_tasks() -> None:
    runner = ScreenJobRunner(max_results=1)
    try:

        def fail(_progress):
            raise TaskUserError("日期范围内没有可用行情")

        job_id = runner.submit_callable(fail, description="历史回测")
        for _ in range(100):
            state = runner.get(job_id)
            if state is not None and state.status == "failed":
                break
            time.sleep(0.005)
        else:
            raise AssertionError("job did not fail")

        assert state is not None
        assert state.error == "日期范围内没有可用行情"
    finally:
        runner.shutdown()


def test_runner_exposes_live_detail_metadata_for_generic_tasks() -> None:
    runner = ScreenJobRunner(max_results=1)

    def task(progress):
        progress.report_detail(
            {
                "phase": "online_bars",
                "current_instrument": {
                    "market": "SH",
                    "code": "600000",
                    "name": "浦发银行",
                },
                "range_label": "最新 800 根日线",
            }
        )
        progress(1, 1)
        return {}

    try:
        job_id = runner.submit_callable(task, description="行情同步")
        wait_until_complete(runner, job_id)
        state = runner.get(job_id)

        assert state is not None
        assert state.phase == "completed"
        assert state.metadata["current_instrument"] == {
            "market": "SH",
            "code": "600000",
            "name": "浦发银行",
        }
        assert state.metadata["range_label"] == "最新 800 根日线"
    finally:
        runner.shutdown()


def test_runner_default_starts_eight_background_tasks_concurrently() -> None:
    runner = ScreenJobRunner(max_results=16)
    started = Event()
    release = Event()
    lock = Lock()
    active = 0
    max_active = 0

    def task(_progress):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            if active == 8:
                started.set()
        release.wait(timeout=2)
        with lock:
            active -= 1
        return {}

    try:
        job_ids = [runner.submit_callable(task, description="并发测试") for _ in range(8)]
        assert started.wait(timeout=2)
        release.set()
        for job_id in job_ids:
            wait_until_complete(runner, job_id)
        assert max_active == 8
    finally:
        release.set()
        runner.shutdown()


def test_runner_cooperatively_cancels_a_generic_task() -> None:
    runner = ScreenJobRunner(max_results=1)
    started = Event()
    job_id_holder = [""]

    def task(_progress):
        started.set()
        while not runner.is_cancel_requested(job_id_holder[0]):
            time.sleep(0.001)
        raise TaskCancelledError("任务已取消")

    try:
        job_id_holder[0] = runner.submit_callable(task, description="可取消任务")
        assert started.wait(timeout=1)
        assert runner.request_cancel(job_id_holder[0]) is True
        for _ in range(100):
            state = runner.get(job_id_holder[0])
            if state is not None and state.status == "cancelled":
                break
            time.sleep(0.005)
        else:
            raise AssertionError("job was not cancelled")
        assert state is not None
        assert state.error == "任务已取消"
    finally:
        runner.shutdown()


def test_runner_lists_active_job_snapshots_by_description() -> None:
    runner = ScreenJobRunner(max_results=2)
    started = Event()
    release = Event()

    def task(_progress):
        started.set()
        release.wait(timeout=1)
        return {}

    try:
        job_id = runner.submit_callable(task, description="行情同步")
        assert started.wait(timeout=1)

        active = runner.active_snapshots({"行情同步"})

        assert len(active) == 1
        assert active[0]["job_id"] == job_id
        assert active[0]["description"] == "行情同步"
        assert runner.active_snapshots({"策略实验室"}) == []
    finally:
        release.set()
        runner.shutdown()


def test_runner_rejects_a_second_job_in_the_same_exclusive_group() -> None:
    runner = ScreenJobRunner(max_results=2)
    release = Event()

    def task(_progress):
        release.wait(timeout=1)
        return {}

    try:
        first = runner.submit_callable(task, description="行情同步", exclusive_group="market")
        with pytest.raises(ActiveJobConflictError, match="已有任务"):
            runner.submit_callable(task, description="行情同步", exclusive_group="market")
        assert runner.get(first) is not None
    finally:
        release.set()
        runner.shutdown()
