"""Swing research endpoints.

Two steps, deliberately kept apart. ``/overview`` cuts the market and reports
distributions so a threshold can be picked with data rather than taste;
``/indicator-study`` then scores every dimensionless technical output against
the labels that segmentation implies.

Each step has a synchronous endpoint and a ``/stream`` twin. The twin is what
the UI uses: a full-market study is tens of seconds to minutes of work, and a
request that only answers at the end leaves the button looking broken. The
stream variant pushes Server-Sent Events - one ``progress`` event whenever the
percentage moves, then a single ``result`` (or ``error``) event.

Both twins run the same service call, so there is no second code path to keep
in sync.
"""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Callable, Iterator
from typing import Protocol, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from selector_app.swing_research.models import SwingIndicatorConfig, SwingOverviewConfig
from selector_app.swing_research.service import ProgressCallback, StudyProgressCallback

from ..schemas import SwingIndicatorRequest, SwingOverviewRequest

router = APIRouter(prefix="/swing-research", tags=["swing-research"])

#: nginx buffers proxied responses by default, which would hold every event
#: until the run finished - the one thing the stream exists to avoid.
_STREAM_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


class SwingResearchServiceLike(Protocol):
    def overview(
        self,
        config: SwingOverviewConfig,
        progress_callback: ProgressCallback = None,
    ) -> dict[str, object]: ...

    def indicator_study(
        self,
        config: SwingIndicatorConfig,
        progress_callback: StudyProgressCallback = None,
    ) -> dict[str, object]: ...


def _service(request: Request) -> SwingResearchServiceLike:
    return cast(SwingResearchServiceLike, request.app.state.swing_research_service)


@router.post("/overview")
def swing_overview(payload: SwingOverviewRequest, request: Request) -> dict[str, object]:
    try:
        result = _service(request).overview(payload.to_config())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"data": result}


@router.post("/indicator-study")
def swing_indicator_study(
    payload: SwingIndicatorRequest, request: Request
) -> dict[str, object]:
    try:
        result = _service(request).indicator_study(payload.to_config())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"data": result}


def _sse(event: dict[str, object]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _stream_events(
    runner: Callable[[Callable[[int, int, str], None]], dict[str, object]],
) -> Iterator[str]:
    """Run ``runner`` on a worker thread and yield its progress as SSE text.

    The callback cannot yield - it is called from inside the computation - so
    progress lands on a queue and this generator (which starlette iterates on a
    thread pool) drains it. Events are throttled to one per changed percentage
    or stage: a 5,000-stock scan reports per batch, which would otherwise be
    thousands of frames for a bar nobody can read that precisely.
    """

    events: queue.Queue[dict[str, object] | None] = queue.Queue()
    outcome: dict[str, object] = {}

    def progress(current: int, total: int, stage: str) -> None:
        events.put({"type": "progress", "current": current, "total": total, "stage": stage})

    def worker() -> None:
        try:
            outcome["data"] = runner(progress)
        except ValueError as exc:
            outcome["error"] = str(exc)
        except Exception as exc:  # pragma: no cover - defensive
            outcome["error"] = f"波段研究失败：{exc}"
        finally:
            events.put(None)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    seen: tuple[int, str] | None = None
    while True:
        item = events.get()
        if item is None:
            break
        total = int(str(item.get("total") or 1))
        percent = int(int(str(item["current"])) * 100 / max(total, 1))
        stage = str(item.get("stage") or "")
        if (percent, stage) == seen:
            continue
        seen = (percent, stage)
        yield _sse(item)

    if "error" in outcome:
        yield _sse({"type": "error", "detail": str(outcome["error"])})
    else:
        yield _sse({"type": "result", "data": outcome.get("data")})


@router.post("/overview/stream")
def swing_overview_stream(payload: SwingOverviewRequest, request: Request) -> StreamingResponse:
    service = _service(request)
    config = payload.to_config()

    def runner(report: Callable[[int, int, str], None]) -> dict[str, object]:
        return service.overview(
            config, lambda current, total: report(current, total, "正在扫描与切分股票")
        )

    return StreamingResponse(
        _stream_events(runner),
        media_type="text/event-stream",
        headers=_STREAM_HEADERS,
    )


@router.post("/indicator-study/stream")
def swing_indicator_study_stream(
    payload: SwingIndicatorRequest, request: Request
) -> StreamingResponse:
    service = _service(request)
    config = payload.to_config()

    def runner(report: Callable[[int, int, str], None]) -> dict[str, object]:
        return service.indicator_study(config, report)

    return StreamingResponse(
        _stream_events(runner),
        media_type="text/event-stream",
        headers=_STREAM_HEADERS,
    )
