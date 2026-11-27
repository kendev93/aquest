"""榜单模拟盘接口。"""

from datetime import date
from typing import Literal, cast

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from aquest.strategy_pool.simulations import DEFAULT_SLOTS, PoolSimulations

from ._errors import store_guard

router = APIRouter(prefix="/strategy-pool/simulations", tags=["strategy-pool"])


class SimulationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=80)
    objective: Literal["annual_return", "sharpe"]
    ranking: Literal[
        "default",
        "win_rate",
        "payoff_ratio",
        "annual_return",
        "sharpe",
        "composite",
        "postLaunchReturn",
        "benchmark",
        "excess",
        "holdingReturn",
        "createdReturn",
    ]
    direction: Literal["asc", "desc"] = "desc"
    min_days: Literal[0, 5, 20, 60] = 0
    start_date: date
    max_positions: int = Field(default=DEFAULT_SLOTS, ge=1, le=100)
    initial_cash: float = Field(default=1_000_000, ge=1_000, le=100_000_000, allow_inf_nan=False)
    #: 再平衡阈值：单只权重偏离目标（1/持仓上限）超过多少个百分点时调仓，0 表示关闭。
    rebalance_band_pct: float = Field(default=5, ge=0, le=100, allow_inf_nan=False)


class SimulationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=80)
    paused: bool | None = None

    @model_validator(mode="after")
    def _require_change(self) -> "SimulationUpdate":
        if self.name is None and self.paused is None:
            raise ValueError("至少提供 name 或 paused")
        return self


def _service(request: Request) -> PoolSimulations:
    return cast(PoolSimulations, request.app.state.pool_simulations)


@router.get("")
def list_simulations(request: Request) -> dict[str, object]:
    with store_guard("暂时无法读取榜单模拟盘"):
        return {"data": _service(request).read()}


@router.get("/signal-days")
def list_simulation_signal_days(
    request: Request,
    objective: Literal["annual_return", "sharpe"] = "annual_return",
    ranking: str = "",
) -> dict[str, object]:
    with store_guard("暂时无法读取模拟盘信号日期"):
        return {"data": _service(request).signal_days(objective, ranking)}


@router.post("", status_code=201)
def create_simulation(payload: SimulationCreate, request: Request) -> dict[str, object]:
    with store_guard("暂时无法创建榜单模拟盘"):
        try:
            key = _service(request).create(payload.model_dump(mode="json"))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"data": {"id": key}}


@router.get("/{simulation_id}")
def simulation_detail(
    simulation_id: str,
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, object]:
    with store_guard("暂时无法读取榜单模拟盘详情"):
        rows = _service(request).read(simulation_id, offset=offset, limit=limit)
        if not rows:
            raise HTTPException(404, "模拟盘不存在")
        return {"data": rows[0]}


@router.patch("/{simulation_id}")
def update_simulation(
    simulation_id: str, payload: SimulationUpdate, request: Request
) -> dict[str, object]:
    with store_guard("暂时无法更新榜单模拟盘"):
        service = _service(request)
        try:
            if payload.name is not None:
                service.rename(simulation_id, payload.name)
            if payload.paused is not None:
                service.set_paused(simulation_id, payload.paused)
        except KeyError as exc:
            raise HTTPException(404, "模拟盘不存在") from exc
        return {"data": {"id": simulation_id, "name": payload.name, "paused": payload.paused}}


@router.delete("/{simulation_id}")
def delete_simulation(simulation_id: str, request: Request) -> dict[str, object]:
    with store_guard("暂时无法删除榜单模拟盘"):
        try:
            _service(request).delete(simulation_id)
        except KeyError as exc:
            raise HTTPException(404, "模拟盘不存在") from exc
        return {"data": {"id": simulation_id}}
