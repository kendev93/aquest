"""API for the full-market per-security strategy pool."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, cast

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from selector_app.market_data.store import DuckDbMarketDataStore, MarketDataStoreError
from selector_app.strategy_discovery.duckdb_job_store import DuckDbStrategyJobStore
from selector_app.strategy_discovery.job_backend import StrategyJobBackend
from selector_app.strategy_pool.store import DuckDbStrategyPoolStore, Objective

router = APIRouter(prefix="/strategy-pool", tags=["strategy-pool"])
JOB_DESCRIPTION = "全市场策略池研究"


class PoolJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["full", "single", "signals", "portfolio"] = "full"
    market: Literal["SH", "SZ"] | None = None
    code: str | None = Field(default=None, pattern=r"^\d{6}$")
    objective: Literal["annual_return", "sharpe"] | None = None
    risk_trials: Literal[1_000, 2_000, 3_000] = 3_000
    reversal_multiple: float = Field(default=3.0, gt=0, le=20)
    atr_window: int = Field(default=14, ge=1, le=250)
    min_bars: int = Field(default=5, ge=1, le=1_000)
    horizon: int = Field(default=10, ge=1, le=250)

    @model_validator(mode="after")
    def validate_mode(self) -> PoolJobRequest:
        if self.mode == "single" and (self.market is None or self.code is None):
            raise ValueError("单股重跑必须提供市场和 6 位证券代码")
        if self.mode == "full" and (self.market is not None or self.code is not None):
            raise ValueError("全市场任务不应指定单只证券")
        if self.mode in {"signals", "portfolio"} and self.objective is None:
            raise ValueError("信号扫描或组合回测必须选择年化收益或夏普目标")
        if self.mode not in {"signals", "portfolio"} and self.objective is not None:
            raise ValueError("年化/夏普目标只用于信号扫描或组合回测")
        if self.mode in {"signals", "portfolio"} and (
            self.market is not None or self.code is not None
        ):
            raise ValueError("全市场操作不应指定单只证券")
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
        if envelope is None or envelope.get("operation") != "market_strategy_pool":
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
