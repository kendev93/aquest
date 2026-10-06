from __future__ import annotations

import asyncio

from selector_app.web.app import _cancel_background_tasks


def test_lifespan_cleanup_drains_all_tasks_when_one_already_failed() -> None:
    async def scenario() -> None:
        started = asyncio.Event()

        async def failing() -> None:
            raise RuntimeError("scheduler failed")

        async def waiting() -> None:
            started.set()
            await asyncio.Event().wait()

        failed_task = asyncio.create_task(failing())
        waiting_task = asyncio.create_task(waiting())
        await started.wait()
        await asyncio.sleep(0)

        await _cancel_background_tasks([failed_task, waiting_task])

        assert isinstance(failed_task.exception(), RuntimeError)
        assert waiting_task.cancelled()

    asyncio.run(scenario())
