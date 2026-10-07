"""API for the full-market per-security strategy pool."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, time, timedelta
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from selector_app.market_data.store import DuckDbMarketDataStore, MarketDataStoreError
from selector_app.market_data.store_base import _WRITE_LOCK
from selector_app.strategy_discovery.duckdb_job_store import DuckDbStrategyJobStore
from selector_app.strategy_discovery.job_backend import StrategyJobBackend
from selector_app.strategy_pool.store import DuckDbStrategyPoolStore, Objective
from selector_app.strategy_pool.tracking_scheduler import TRACKING_OPERATION, latest_completed_day
from selector_app.strategy_pool.tracking_store import PoolTrackingStore

router = APIRouter(prefix="/strategy-pool", tags=["strategy-pool"])
JOB_DESCRIPTION = "全市场策略池研究"


class PoolJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["full", "single", "signals", "portfolio"] = "full"
    market: Literal["SH", "SZ"] | None = None
    code: str | None = Field(default=None, pattern=r"^\d{6}$")
    objective: Literal["annual_return", "sharpe"] | None = None
    automatic: bool = False
    rebuild: bool = False
    risk_trials: Literal[1_000, 2_000, 3_000] = 3_000
    #: 最长持仓**交易日**数，逐股研究时下发给每个候选（None = 不限制）。
    max_hold_days: int | None = Field(default=20, ge=1, le=10_000)
    reversal_multiple: float = Field(default=2.0, gt=0, le=20)
    atr_window: int = Field(default=14, ge=1, le=250)
    min_bars: int = Field(default=5, ge=1, le=1_000)
    horizon: int = Field(default=10, ge=1, le=250)

    @model_validator(mode="after")
    def validate_mode(self) -> PoolJobRequest:
        if self.automatic and (self.mode != "signals" or self.market or self.code):
            raise ValueError("每日自动扫描只支持全市场当天信号")
        if self.rebuild and self.mode != "full":
            raise ValueError("只有全市场任务支持清空重建")
        if self.mode == "single" and (self.market is None or self.code is None):
            raise ValueError("单股重跑必须提供市场和 6 位证券代码")
        if self.mode == "full" and (self.market is not None or self.code is not None):
            raise ValueError("全市场任务不应指定单只证券")
        if self.mode in {"signals", "portfolio"} and self.objective is None:
            raise ValueError("信号扫描或组合回测必须选择年化收益或夏普目标")
        if self.mode not in {"signals", "portfolio"} and self.objective is not None:
            raise ValueError("年化/夏普目标只用于信号扫描或组合回测")
        if self.mode == "signals" and (self.market is None) != (self.code is None):
            raise ValueError("定向信号扫描必须同时提供市场和证券代码")
        if self.mode == "portfolio" and (self.market is not None or self.code is not None):
            raise ValueError("组合回测不应指定单只证券")
        return self


class PoolCandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate: dict[str, object]
    benchmark: dict[str, object] | None = None

    @model_validator(mode="after")
    def validate_replayable(self) -> PoolCandidateRequest:
        if not isinstance(self.candidate.get("buy_rule"), Mapping):
            raise ValueError("策略候选缺少买入规则")
        if not isinstance(self.candidate.get("sell_rule"), Mapping):
            raise ValueError("策略候选缺少卖出规则")
        return self


def _pool_store(request: Request) -> DuckDbStrategyPoolStore:
    return cast(DuckDbStrategyPoolStore, request.app.state.strategy_pool_store)


def _market_store(request: Request) -> DuckDbMarketDataStore:
    return cast(DuckDbMarketDataStore, request.app.state.market_data_store)


def _job_store(request: Request) -> DuckDbStrategyJobStore:
    return cast(DuckDbStrategyJobStore, request.app.state.strategy_job_store)


def _job_backend(request: Request) -> StrategyJobBackend:
    return cast(StrategyJobBackend, request.app.state.strategy_job_backend)


def _objective(value: str) -> Objective:
    if value not in {"annual_return", "sharpe"}:
        raise HTTPException(status_code=422, detail="优化目标必须是 annual_return 或 sharpe")
    return cast(Objective, value)


def _entry_or_404(market: str, code: str, request: Request) -> dict[str, object]:
    item = _pool_store(request).get(market, code)
    if item is None:
        raise HTTPException(status_code=404, detail="策略池中找不到这只股票")
    return item


def _pool_job_state(job_id: str, request: Request) -> dict[str, object]:
    try:
        envelope = _job_store(request).read_request(job_id)
        if envelope is None or envelope.get("operation") not in {
            "market_strategy_pool",
            TRACKING_OPERATION,
        }:
            raise HTTPException(status_code=404, detail="策略池任务不存在")
        state = _job_backend(request).get(job_id)
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(
            status_code=503,
            detail="策略池任务状态暂时无法读取，请稍后自动重试",
        ) from exc
    if state is None:
        raise HTTPException(status_code=404, detail="策略池任务不存在")
    return state


@router.get("/entries")
def list_strategy_pool_entries(
    request: Request,
    objective: str = Query(default="annual_return"),
) -> dict[str, object]:
    selected = _objective(objective)
    items = _pool_store(request).list_entries()
    tracks = PoolTrackingStore(_market_store(request)).current_versions()
    track_index = {(v["market"], v["code"], v["objective"], v["source"]): v for v in tracks}
    for item in items:
        automatic = item.get("automatic_candidates")
        overrides = item.get("manual_overrides")
        automatic_map = automatic if isinstance(automatic, Mapping) else {}
        override_map = overrides if isinstance(overrides, Mapping) else {}
        active = override_map.get(selected) or automatic_map.get(selected)
        if isinstance(active, Mapping) and isinstance(active.get("candidate_ref"), str):
            active = automatic_map.get(active["candidate_ref"])
        item["active_candidate"] = active
        item["active_source"] = "manual" if override_map.get(selected) is not None else "automatic"
        tracking = track_index.get((item["market"], item["code"], selected, item["active_source"]))
        item["tracking"] = tracking
        item["created_at"] = tracking["created_at"] if tracking else None
        item["created_return"] = tracking["summary"].get("created_return") if tracking else None
        item["tracking_status"] = tracking["status"] if tracking else "unavailable"
        item["tracking_as_of_date"] = tracking["summary"].get("as_of_date") if tracking else None

    return {"data": {"objective": selected, "items": items}}


@router.post("/jobs", status_code=status.HTTP_202_ACCEPTED)
def create_strategy_pool_job(payload: PoolJobRequest, request: Request) -> dict[str, object]:
    backend = _job_backend(request)
    if payload.mode == "single":
        try:
            ref = request.app.state.market_data_adapter.stock_ref(
                "", cast(Literal["SH", "SZ"], payload.market), cast(str, payload.code)
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if getattr(ref, "instrument_type", "stock") != "stock":
            raise HTTPException(status_code=422, detail="策略池只支持 A 股股票")
        with _market_store(request).read_session() as connection:
            completed = connection.execute(
                """
                SELECT 1 FROM daily_bars
                WHERE market = ? AND code = ? AND instrument_type = 'stock'
                  AND bar_status = 'completed'
                LIMIT 1
                """,
                [payload.market, payload.code],
            ).fetchone()
        if completed is None:
            raise HTTPException(status_code=422, detail="这只股票没有已完成的本地日线")
    elif payload.mode == "full":
        refs = request.app.state.market_data_adapter.list_stock_refs(
            "", "all", instrument_types=("stock",)
        )
        if not refs:
            raise HTTPException(status_code=422, detail="本地行情没有可研究的 A 股日线")
        with _market_store(request).read_session() as connection:
            completed_count = connection.execute(
                """
                SELECT COUNT(DISTINCT market || ':' || code)
                FROM daily_bars
                WHERE instrument_type = 'stock' AND bar_status = 'completed'
                """
            ).fetchone()
        if completed_count is None or int(completed_count[0] or 0) == 0:
            raise HTTPException(status_code=422, detail="本地行情没有已完成的 A 股日线")
    else:
        if not _pool_store(request).list_entries():
            raise HTTPException(
                status_code=422, detail="策略池还没有完成研究，暂时没有可扫描的策略"
            )
    if payload.mode == "signals":
        if payload.automatic:
            return {"data": {"job_id": "", "status": "completed", "skipped": True}}
        active_tracking = [
            j for j in backend.active_snapshots() if j.get("operation") == TRACKING_OPERATION
        ]
        if active_tracking:
            raise HTTPException(status_code=409, detail="已有策略池跟踪任务正在运行")
        target_ids = None
        if payload.market and payload.code:
            target_ids = [
                v["version_id"]
                for v in PoolTrackingStore(_market_store(request)).current_versions()
                if v["market"] == payload.market and v["code"] == payload.code
            ]
        job_id = backend.submit(
            TRACKING_OPERATION,
            {
                **payload.model_dump(exclude_none=True),
                "as_of_date": latest_completed_day(_market_store(request)),
                "version_ids": target_ids,
            },
            description="策略池双目标跟踪",
            worker_count=1,
            metadata={"mode": "signals", "phase_label": "排队等待跟踪通道"},
        )
        return {"data": {"job_id": job_id, "status": "queued"}}
    # Serialize the durable daily check and submission, including simultaneous page entries.
    with _WRITE_LOCK:
        if payload.automatic:
            current = datetime.now(ZoneInfo("Asia/Shanghai"))
            start = datetime.combine(current.date(), time.min, tzinfo=current.tzinfo)
            try:
                existing = _job_store(request).latest_pool_signal_attempt(
                    start.timestamp(), (start + timedelta(days=1)).timestamp()
                )
            except (MarketDataStoreError, OSError) as exc:
                raise HTTPException(status_code=503, detail="每日扫描记录暂时无法读取") from exc
            if existing is not None:
                return {"data": {**existing, "skipped": True}}
        active = [
            item
            for item in backend.active_snapshots()
            if item.get("operation") == "market_strategy_pool"
        ]
        active_metadata = active[0].get("metadata") if len(active) == 1 else None
        can_queue_single_after_signal_scan = (
            payload.mode == "single"
            and isinstance(active_metadata, Mapping)
            and active_metadata.get("mode") == "signals"
        )
        if active and not can_queue_single_after_signal_scan:
            raise HTTPException(status_code=409, detail="已有全市场策略池任务正在运行")
        job_payload = payload.model_dump(exclude_none=True)
        try:
            job_id = backend.submit(
                "market_strategy_pool",
                job_payload,
                description=JOB_DESCRIPTION,
                worker_count=1,
                metadata={"mode": payload.mode, "phase_label": "排队等待策略 worker"},
            )
        except (MarketDataStoreError, OSError, RuntimeError) as exc:
            raise HTTPException(status_code=503, detail="策略池任务暂时无法提交") from exc
        return {"data": {"job_id": job_id, "status": "queued"}}


@router.get("/jobs")
def list_strategy_pool_jobs(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    try:
        return {
            "data": _job_store(request).list_market_strategy_pool_history(
                limit=limit, offset=offset
            )
        }
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="策略池任务历史暂不可用") from exc


@router.get("/jobs/{job_id}")
def get_strategy_pool_job(job_id: str, request: Request) -> dict[str, object]:
    state = _pool_job_state(job_id, request)
    try:
        result = (
            _job_backend(request).result(job_id) if state.get("status") == "completed" else None
        )
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(
            status_code=503,
            detail="策略池任务结果暂时无法读取，请稍后自动重试",
        ) from exc
    return {"data": {**state, "result": result}}


@router.post("/jobs/{job_id}/cancel")
def cancel_strategy_pool_job(job_id: str, request: Request) -> dict[str, object]:
    _pool_job_state(job_id, request)
    try:
        return {"data": {"cancelled": _job_backend(request).cancel(job_id)}}
    except (MarketDataStoreError, OSError) as exc:
        raise HTTPException(status_code=503, detail="策略池取消请求暂不可用") from exc


@router.post("/entries/{market}/{code}/overrides/{objective}")
def update_strategy_pool_override(
    market: str,
    code: str,
    objective: str,
    payload: PoolCandidateRequest,
    request: Request,
) -> dict[str, object]:
    selected = _objective(objective)
    try:
        _pool_store(request).set_manual_override(
            market.upper(),
            code,
            selected,
            payload.candidate,
            benchmark=payload.benchmark,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"data": _entry_or_404(market.upper(), code, request)}


@router.delete("/entries/{market}/{code}/overrides/{objective}")
def clear_strategy_pool_override(
    market: str,
    code: str,
    objective: str,
    request: Request,
) -> dict[str, object]:
    selected = _objective(objective)
    _entry_or_404(market.upper(), code, request)
    _pool_store(request).clear_manual_override(market.upper(), code, selected)
    return {"data": _entry_or_404(market.upper(), code, request)}


__all__ = ["router"]


@router.get("/entries/{market}/{code}/detail")
def pool_detail(market: str, code: str, request: Request) -> dict[str, object]:
    item = _entry_or_404(market, code, request)
    tracks = PoolTrackingStore(_market_store(request))
    return {
        "data": {
            "market": market,
            "code": code,
            "name": item["name"],
            "objectives": {
                goal: tracks.versions(market, code, goal) for goal in ("annual_return", "sharpe")
            },
            "manual_overrides": item["manual_overrides"],
        }
    }


def _owned_version(
    market: str, code: str, version_id: str, request: Request
) -> tuple[PoolTrackingStore, dict[str, Any]]:
    tracks = PoolTrackingStore(_market_store(request))
    versions = [
        v for goal in ("annual_return", "sharpe") for v in tracks.versions(market, code, goal)
    ]
    version = next((v for v in versions if v["version_id"] == version_id), None)
    if version is None:
        raise HTTPException(status_code=404, detail="跟踪版本不存在")
    return tracks, version


@router.get("/entries/{market}/{code}/versions/{version_id}/curve")
def pool_curve(
    market: str,
    code: str,
    version_id: str,
    request: Request,
    scope: Literal["created", "full"] = "created",
) -> dict[str, object]:
    tracks, version = _owned_version(market, code, version_id, request)
    snapshot = tracks.snapshot(version_id)
    if not snapshot:
        return {"data": {"version": version, "backtest": None}}
    cp = snapshot["checkpoint"]
    summary = snapshot["summary"]
    start = summary["baseline_date"] if scope == "created" else ""
    points = [p for p in cp["equity"] if p["date"] >= start]
    baseline = (
        summary["baseline_equity"]
        if scope == "created"
        else float(version["candidate"].get("config", {}).get("initial_cash", 1000000))
    )
    trades = [t for t in cp["trades"] if t["date"] >= start]
    preserve = {0, len(points) - 1}
    event_dates = {t["date"] for t in trades}
    launch_day = (
        datetime.fromisoformat(version["created_at"])
        .astimezone(ZoneInfo("Asia/Shanghai"))
        .date()
        .isoformat()
    )
    launch_index = next((j for j, p in enumerate(points) if p["date"] >= launch_day), len(points))
    for i, point in enumerate(points):
        if point["date"] in event_dates or abs(i - launch_index) <= 1:
            preserve.add(i)
    step = max(1, len(points) // 240)
    directions: dict[str, list[str]] = {}
    for trade in trades:
        directions.setdefault(trade["date"], []).append(trade["direction"])
    sampled = [
        dict(
            p,
            return_pct=float(p["total"]) / baseline - 1,
            trade_directions=directions.get(p["date"], []),
        )
        for i, p in enumerate(points)
        if i in preserve or i % step == 0
    ]
    return {
        "data": {
            "version": version,
            "summary": summary,
            "backtest": {
                **snapshot["report"],
                "equity_curve": sampled,
                "closed_trades": sum(
                    t["direction"] == "SELL" and not t.get("rejected") for t in trades
                ),
                "start_date": points[0]["date"] if points else snapshot["report"]["start_date"],
                "trades": [],
                "performance": {
                    **snapshot["report"]["performance"],
                    "total_return": summary["created_return"]
                    if scope == "created"
                    else snapshot["report"]["performance"].get("total_return"),
                },
                "launch_date": launch_day,
                "baseline_date": summary["baseline_date"],
            },
        }
    }


@router.get("/entries/{market}/{code}/versions/{version_id}/trades")
def pool_trades(
    market: str,
    code: str,
    version_id: str,
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    scope: Literal["created", "full"] = "created",
) -> dict[str, object]:
    tracks, version = _owned_version(market, code, version_id, request)
    snapshot = tracks.snapshot(version_id)
    trades = snapshot["checkpoint"]["trades"] if snapshot else []
    if snapshot and scope == "created":
        trades = [t for t in trades if t["date"] >= snapshot["summary"]["baseline_date"]]
    return {"data": {"items": trades[offset : offset + limit], "total": len(trades)}}
