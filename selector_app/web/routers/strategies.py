"""Saved strategy library: persist, load, rename and delete portfolio strategies."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import cast

from fastapi import APIRouter, HTTPException, Request, status

from selector_app.market_data.models import DataStoreStatus
from selector_app.market_data.store import DuckDbMarketDataStore, MarketDataStoreError

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
