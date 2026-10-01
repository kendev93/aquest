"""Swing research endpoints.

Two steps, deliberately kept apart. ``/overview`` cuts the market and reports
distributions so a threshold can be picked with data rather than taste;
``/indicator-study`` then scores individual indicators and a bounded set of
two-indicator features against those labels on chronological train, validation
and holdout splits.

Each step has a synchronous endpoint and a ``/stream`` twin. The twin is what
the UI uses: a full-market study is tens of seconds to minutes of work, and a
request that only answers at the end leaves the button looking broken. The
stream variant pushes Server-Sent Events - one ``progress`` event whenever the
percentage moves, then a single ``result`` (or ``error``) event.

Both twins run the same service call, so there is no second code path to keep
in sync.

A finished study is persisted on its own - the same contract as the strategy
lab - and the id is handed back so the page can show it in its history list
without guessing. Step one is archived with it: the calibration form and table
only ever live in the browser, so without them a loaded history would restore
the second step and leave the first blank.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime
from typing import Protocol, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from selector_app.market_data.store import DuckDbMarketDataStore, MarketDataStoreError
from selector_app.swing_research.models import SwingIndicatorConfig, SwingOverviewConfig
from selector_app.swing_research.service import ProgressCallback, StudyProgressCallback

from ..backtest_history import save_completed_backtest_history, utc_now
from ..schemas import SwingIndicatorRequest, SwingOverviewRequest

router = APIRouter(prefix="/swing-research", tags=["swing-research"])

logger = logging.getLogger(__name__)

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


def _store(request: Request) -> DuckDbMarketDataStore:
    return cast(DuckDbMarketDataStore, request.app.state.market_data_store)


def _study_request_snapshot(payload: SwingIndicatorRequest) -> dict[str, object]:
    """The stored request: study parameters plus step one's archived state."""

    return cast(dict[str, object], payload.model_dump(mode="json"))


def _save_study_history(
    request: Request,
    payload: SwingIndicatorRequest,
    result: Mapping[str, object],
    started_at: datetime,
    finished_at: datetime,
) -> str:
    """Persist one finished study and return its history id.

    Best-effort like every other auto-saved history: a locked database must not
    throw away minutes of computation. The id is returned regardless so the
    caller can report it - a failed write simply leaves nothing to load.
    """

    history_id = uuid.uuid4().hex
    save_completed_backtest_history(
        _store(request),
        history_id=history_id,
        backtest_type="swing_research",
        request=_study_request_snapshot(payload),
        result=result,
        started_at=started_at,
        finished_at=finished_at,
    )
    return history_id


@router.post("/overview")
def swing_overview(payload: SwingOverviewRequest, request: Request) -> dict[str, object]:
    try:
        result = _service(request).overview(payload.to_config())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"data": result}


@router.post("/indicator-study")
def swing_indicator_study(payload: SwingIndicatorRequest, request: Request) -> dict[str, object]:
    started_at = utc_now()
    try:
        result = _service(request).indicator_study(payload.to_config())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    history_id = _save_study_history(request, payload, result, started_at, utc_now())
    return {"data": result, "history_id": history_id}


@router.get("/history")
def swing_research_history(request: Request) -> dict[str, object]:
    try:
        return {"data": _store(request).list_backtest_history("swing_research")}
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="波段研究历史暂不可用") from exc


@router.get("/history/{history_id}")
def swing_research_history_detail(history_id: str, request: Request) -> dict[str, object]:
    try:
        record = _store(request).read_backtest_history(history_id)
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="波段研究历史暂不可用") from exc
    if record is None or record.get("backtest_type") != "swing_research":
        raise HTTPException(status_code=404, detail="波段研究历史不存在")
    return {"data": record}


@router.delete("/history/{history_id}")
def delete_swing_research_history(history_id: str, request: Request) -> dict[str, object]:
    try:
        record = _store(request).read_backtest_history(history_id)
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="波段研究历史暂不可用") from exc
    if record is None or record.get("backtest_type") != "swing_research":
        return {"data": {"deleted": False}}
    deleted = _store(request).delete_backtest_history(history_id)
    return {"data": {"deleted": deleted}}


def _sse(event: dict[str, object]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _stream_events(
    runner: Callable[[Callable[[int, int, str], None]], dict[str, object]],
    persist: Callable[[Mapping[str, object]], str] | None = None,
) -> Iterator[str]:
    """Run ``runner`` on a worker thread and yield its progress as SSE text.

    The callback cannot yield - it is called from inside the computation - so
    progress lands on a queue and this generator (which starlette iterates on a
    thread pool) drains it. Events are throttled to one per changed percentage
    or stage: a 5,000-stock scan reports per batch, which would otherwise be
    thousands of frames for a bar nobody can read that precisely.

    ``persist`` runs on the worker thread once the result is in, before the
    final event, so a study that took two minutes is never lost to a history
    write that happens after the client already disconnected.
    """

    events: queue.Queue[dict[str, object] | None] = queue.Queue()
    outcome: dict[str, object] = {}

    def progress(current: int, total: int, stage: str) -> None:
        events.put({"type": "progress", "current": current, "total": total, "stage": stage})

    def worker() -> None:
        try:
            data = runner(progress)
            outcome["data"] = data
            if persist is not None:
                outcome["history_id"] = persist(data)
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
        event: dict[str, object] = {"type": "result", "data": outcome.get("data")}
        history_id = outcome.get("history_id")
        if isinstance(history_id, str) and history_id:
            event["history_id"] = history_id
        yield _sse(event)


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
    started_at = utc_now()

    def runner(report: Callable[[int, int, str], None]) -> dict[str, object]:
        return service.indicator_study(config, report)

    def persist(result: Mapping[str, object]) -> str:
        return _save_study_history(request, payload, result, started_at, utc_now())

    return StreamingResponse(
        _stream_events(runner, persist),
        media_type="text/event-stream",
        headers=_STREAM_HEADERS,
    )
