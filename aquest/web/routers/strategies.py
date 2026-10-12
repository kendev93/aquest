"""Saved strategy library: persist, load, rename, delete, and track."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import date
from typing import Literal, cast

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse

from aquest.market_data.auto_sync import ACTIVE_DATA_JOB_DESCRIPTIONS
from aquest.market_data.models import DataStoreStatus
from aquest.market_data.store import DuckDbMarketDataStore, MarketDataStoreError
from aquest.portfolio_backtest.auto_tracking import (
    TRACKING_DESCRIPTION,
    TRACKING_EXCLUSIVE_GROUP,
)
from aquest.portfolio_backtest.service import PortfolioBacktestService
from aquest.portfolio_backtest.tracking import StrategyTrackingService
from aquest.screening.jobs import (
    ActiveJobConflictError,
    JobState,
    ProgressReporter,
    ScreenJobRunner,
    TaskUserError,
)

from ..date_ranges import resolve_date_range
from ..market_source import resolve_market_status
from ..schemas import (
    PortfolioBacktestRequest,
    SavedStrategyCreateRequest,
    SavedStrategyUpdateRequest,
)

router = APIRouter(prefix="/strategies", tags=["strategies"])

#: 列表页要展示的标量字段。其余一律不存。
_SUMMARY_SCALAR_KEYS = (
    "start_date",
    "end_date",
    "bars",
    "max_positions",
    "ranking_value",
    "rank_order",
    "universe",
    "total_candidates",
)
_PERFORMANCE_KEYS = (
    "total_return",
    "annual_return",
    "max_drawdown",
    "sharpe",
    "volatility",
    "calmar",
    "total_trades",
    "win_rate",
)


def _store(request: Request) -> DuckDbMarketDataStore:
    return cast(DuckDbMarketDataStore, request.app.state.market_data_store)


def _runner(request: Request) -> ScreenJobRunner:
    return cast(ScreenJobRunner, request.app.state.screen_job_runner)


def _tracking_service(request: Request) -> StrategyTrackingService:
    """一次完整的重跑是分钟级（要重算全历史），所以追踪全部走后台任务。"""

    return StrategyTrackingService(
        store=_store(request),
        runner=cast(PortfolioBacktestService, request.app.state.portfolio_backtest_service),
    )


def _numeric_performance(performance: Mapping[str, object]) -> dict[str, float | None]:
    """Keep only comparable numeric metrics, and only the ones we display."""

    kept: dict[str, float | None] = {}
    for key in _PERFORMANCE_KEYS:
        if key not in performance:
            continue
        value = performance[key]
        if value is None:
            kept[key] = None
        elif isinstance(value, bool):
            continue
        elif isinstance(value, (int, float)):
            kept[key] = float(value)
    return kept


def _strategy_summary(summary: Mapping[str, object]) -> dict[str, object]:
    """Trim a backtest result down to what the strategy list actually shows.

    The full result carries ``equity_curve`` / ``trades`` / ``states`` that run
    to tens of thousands of rows.  Copying them into every saved strategy would
    inflate the database and make each list request deserialize megabytes.
    """

    kept: dict[str, object] = {}
    for key in _SUMMARY_SCALAR_KEYS:
        if key in summary:
            kept[key] = summary[key]
    performance = summary.get("performance")
    if isinstance(performance, Mapping):
        kept["performance"] = _numeric_performance(performance)
    benchmark = summary.get("benchmark")
    if isinstance(benchmark, Mapping):
        benchmark_performance = benchmark.get("performance")
        if isinstance(benchmark_performance, Mapping):
            kept["benchmark_performance"] = _numeric_performance(benchmark_performance)
    return kept


def _freeze_dates(
    payload: PortfolioBacktestRequest,
    status_snapshot: DataStoreStatus,
) -> PortfolioBacktestRequest:
    """Resolve relative ranges ("近一年") into the absolute dates seen today.

    A strategy is meant to be replayed later.  Storing ``recent5`` would make a
    tracking rerun silently slide its window forward, so the sample would never
    be the one the user judged the strategy on.
    """

    if payload.start_date is not None and payload.end_date is not None:
        return payload.model_copy(update={"date_mode": "custom"})
    start_date, end_date = resolve_date_range(
        mode=payload.date_mode,
        requested_start=payload.start_date,
        requested_end=payload.end_date,
        available_start=status_snapshot.data_start,
        available_end=status_snapshot.data_end,
    )
    return payload.model_copy(
        update={"date_mode": "custom", "start_date": start_date, "end_date": end_date}
    )


@router.post("", status_code=status.HTTP_201_CREATED)
def create_strategy(payload: SavedStrategyCreateRequest, request: Request) -> dict[str, object]:
    try:
        frozen = _freeze_dates(payload.request, resolve_market_status(request))
        record = _store(request).save_strategy(
            uuid.uuid4().hex,
            name=payload.name,
            note=payload.note,
            request=cast(dict[str, object], frozen.model_dump(mode="json")),
            summary=_strategy_summary(payload.summary),
            source_history_id=payload.source_history_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="策略库暂不可用") from exc
    return {"data": record}


@router.get("")
def list_strategies(request: Request) -> dict[str, object]:
    try:
        return {"data": _store(request).list_strategies()}
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="策略库暂不可用") from exc


@router.get("/tracking")
def list_strategy_tracking(request: Request) -> dict[str, object]:
    """Every saved strategy together with how it has behaved since going online."""

    try:
        service = _tracking_service(request)
        rows: list[dict[str, object]] = []
        for record in _store(request).list_strategies():
            snapshot = service.snapshot(str(record["strategy_id"])).to_dict()
            # 成交明细几百笔起步，乘上策略数会把列表接口撑大；它只在展开某一个
            # 策略时才需要，走 /tracking/{id} 单独取。明日计划很小且列表页就要
            # 用（一眼看到今天哪些策略要调仓），所以留着。
            snapshot["trades"] = None
            snapshot["current_state"] = None
            snapshot["today_candidates"] = None
            snapshot["name"] = record["name"]
            snapshot["note"] = record["note"]
            rows.append(snapshot)
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="策略追踪暂不可用") from exc
    return {"data": rows}


@router.get("/tracking/jobs/{job_id}")
def tracking_job_state(job_id: str, request: Request) -> dict[str, object]:
    """Poll one tracking job. The result is only attached once it succeeded."""

    state = _job_state_or_404(job_id, request)
    snapshot = state.snapshot()
    if state.status != "completed":
        snapshot["result"] = None
    return {"data": snapshot}


@router.get("/tracking/{strategy_id}")
def read_strategy_tracking(strategy_id: str, request: Request) -> dict[str, object]:
    """One strategy's full tracking snapshot, including the observation curve."""

    try:
        snapshot = _tracking_service(request).snapshot(strategy_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="策略追踪暂不可用") from exc
    return {"data": snapshot.to_dict()}


@router.get("/tracking/{strategy_id}/trades")
def page_strategy_tracking_trades(
    strategy_id: str,
    request: Request,
    phase: Literal["pre", "post"] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    """Page through actual fills with optional segment and date filters."""

    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(status_code=422, detail="成交日期范围无效")
    try:
        if _store(request).read_strategy(strategy_id) is None:
            raise HTTPException(status_code=404, detail="策略不存在")
        page = _store(request).page_tracking_trades(
            strategy_id,
            phase=phase,
            date_from=(int(date_from.strftime("%Y%m%d")) if date_from else None),
            date_to=(int(date_to.strftime("%Y%m%d")) if date_to else None),
            limit=limit,
            offset=offset,
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="策略追踪暂不可用") from exc
    return {"data": page}


@router.post("/tracking/{strategy_id}/launch")
def launch_strategy_tracking(strategy_id: str, request: Request) -> Response:
    """Put a strategy online. Replays its frozen window once to set the yardstick."""

    record = _require_strategy(strategy_id, request)
    if _tracking_status(record) == "active":
        # 重新上线会把研究段再跑一遍、覆盖已冻结的基线。要更新观察段用 rerun。
        raise HTTPException(status_code=409, detail="策略已上线，要更新请用「立即重算」")
    return _submit_tracking_job(strategy_id, request, launch=True)


@router.post("/tracking/{strategy_id}/rerun")
def rerun_strategy_tracking(strategy_id: str, request: Request) -> Response:
    """Replay up to the newest market day. Idempotent, and it backfills gaps."""

    record = _require_strategy(strategy_id, request)
    if _tracking_status(record) != "active":
        # 停止之后的策略不该再被任何入口推进，否则「停止」这个动作形同虚设。
        raise HTTPException(status_code=409, detail="策略未上线，请先开始追踪")
    return _submit_tracking_job(strategy_id, request, launch=False)


@router.post("/tracking/{strategy_id}/stop", status_code=status.HTTP_200_OK)
def stop_strategy_tracking(strategy_id: str, request: Request) -> dict[str, object]:
    """Take it offline, keeping whatever it already observed."""

    try:
        snapshot = _tracking_service(request).stop(strategy_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="策略追踪暂不可用") from exc
    return {"data": snapshot.to_dict()}


def _submit_tracking_job(strategy_id: str, request: Request, *, launch: bool) -> Response:
    service = _tracking_service(request)
    runner = _runner(request)
    job_id = uuid.uuid4().hex

    def run_tracking(progress: ProgressReporter) -> dict[str, object]:
        del progress
        try:
            snapshot = service.launch(strategy_id) if launch else service.refresh(strategy_id)
        except ValueError as exc:
            raise TaskUserError(str(exc)) from exc
        return snapshot.to_dict()

    # 两道守卫，和调度器对齐：一次重算要跑全历史（分钟级），行情同步进行中拿到的
    # 还是同步前的行情，算出来是份看着正常、实际过时的结果；而两个重算并发跑会同
    # 时写 saved_strategies 与 tracking_points 的同一批行。
    if runner.has_active_jobs(ACTIVE_DATA_JOB_DESCRIPTIONS):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="行情同步或指标预计算正在运行，等它跑完再重算，否则用的是旧行情",
        )
    try:
        runner.submit_callable(
            run_tracking,
            description=TRACKING_DESCRIPTION,
            job_id=job_id,
            exclusive_group=TRACKING_EXCLUSIVE_GROUP,
        )
    except ActiveJobConflictError as exc:
        # 它是 RuntimeError 的子类，不单独接住会落到下面的兜底变成 503。
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="已有策略追踪任务正在运行，请等它跑完",
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="任务执行器暂不可用") from exc
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"data": {"job_id": job_id, "status": "queued"}},
    )


def _require_strategy(strategy_id: str, request: Request) -> Mapping[str, object]:
    try:
        record = _store(request).read_strategy(strategy_id)
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="策略库暂不可用") from exc
    if record is None:
        raise HTTPException(status_code=404, detail="策略不存在")
    return record


def _tracking_status(record: Mapping[str, object]) -> str:
    value = record.get("tracking_status")
    return str(value) if value is not None else "off"


def _job_state_or_404(job_id: str, request: Request) -> JobState:
    state = _runner(request).get(job_id.strip())
    if state is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return state


@router.get("/{strategy_id}")
def read_strategy(strategy_id: str, request: Request) -> dict[str, object]:
    try:
        record = _store(request).read_strategy(strategy_id)
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="策略库暂不可用") from exc
    if record is None:
        raise HTTPException(status_code=404, detail="策略不存在")
    return {"data": record}


@router.patch("/{strategy_id}")
def update_strategy(
    strategy_id: str,
    payload: SavedStrategyUpdateRequest,
    request: Request,
) -> dict[str, object]:
    try:
        record = _store(request).update_strategy(
            strategy_id,
            name=payload.name,
            note=payload.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="策略库暂不可用") from exc
    if record is None:
        raise HTTPException(status_code=404, detail="策略不存在")
    return {"data": record}


@router.delete("/{strategy_id}")
def delete_strategy(strategy_id: str, request: Request) -> dict[str, object]:
    try:
        deleted = _store(request).delete_strategy(strategy_id)
    except (MarketDataStoreError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="策略库暂不可用") from exc
    return {"data": {"deleted": deleted}}
