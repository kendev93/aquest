"""策略发现、策略适配度与策略库请求体。"""

from __future__ import annotations

import secrets
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from selector_app.market_data.models import InstrumentBoard, InstrumentType
from selector_app.portfolio_backtest.models import (
    PortfolioUniverse,
)
from selector_app.screening.models import ScreenCondition
from selector_app.strategy_discovery.catalog import default_indicator_catalog
from selector_app.strategy_discovery.models import DiscoveryRequest
from selector_app.strategy_fitness.models import StrategyFitnessConfig

from ..date_ranges import (
    coerce_date,
)
from ._shared import (
    StrategyDiscoveryDateMode,
    validate_instrument_scope,
)
from .portfolio import PortfolioBacktestRequest
from .screening import ScreenConditionRequest


class StrategyDiscoveryRequest(BaseModel):
    """User-facing inputs for automatic local portfolio strategy discovery."""

    model_config = ConfigDict(extra="forbid")

    vipdoc_path: str = Field(default="", max_length=1024)
    universe: PortfolioUniverse = "all"
    universe_file: str | None = Field(default=None, max_length=1024)
    instrument_types: list[InstrumentType] | None = Field(default=None, max_length=1)
    boards: list[InstrumentBoard] | None = Field(default=None, max_length=7)
    required_buy_conditions: list[ScreenConditionRequest] = Field(
        default_factory=list, max_length=3
    )
    required_sell_conditions: list[ScreenConditionRequest] = Field(
        default_factory=list, max_length=3
    )
    date_mode: StrategyDiscoveryDateMode = "recent5"
    start_date: date | None = None
    end_date: date | None = None
    max_positions: int = Field(default=5, ge=1, le=100)
    initial_cash: float = Field(default=1_000_000.0, gt=0, le=1_000_000_000_000)
    commission: float = Field(default=0.0001, ge=0, le=0.1)
    min_commission: float = Field(default=0.1, ge=0, le=100_000)
    stamp_tax: float = Field(default=0.001, ge=0, le=0.1)
    slippage: float = Field(default=0.0, ge=0, le=1_000)
    execution: Literal["next_open", "next_close"] = "next_open"
    max_trials: int = Field(default=1_000, ge=1, le=5_000)
    timeout_seconds: int = Field(default=7_200, ge=1, le=86_400)
    validation_candidates: int = Field(default=50, ge=1, le=200)
    holdout_candidates: int = Field(default=20, ge=1, le=50)
    min_trades: int = Field(default=5, ge=0, le=10_000)
    max_drawdown: float | None = Field(default=0.3, ge=0, le=1)
    max_turnover: float | None = Field(default=None, ge=0)
    max_complexity: int = Field(default=12, ge=0, le=50)
    multifidelity_enabled: bool = True
    seed: int | None = Field(default=None, ge=0, le=2**32 - 1)

    @model_validator(mode="after")
    def validate_discovery(self) -> StrategyDiscoveryRequest:
        validate_instrument_scope(self.universe, self.instrument_types, self.boards)
        if self.universe == "custom" and not self.universe_file:
            raise ValueError("自定义股票范围必须设置 universe_file")
        if self.date_mode == "custom" and (self.start_date is None or self.end_date is None):
            raise ValueError("自定义日期范围必须同时设置开始日期和结束日期")
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError("开始日期和结束日期必须同时设置")
        if self.start_date is not None and self.end_date is not None:
            if self.start_date > self.end_date:
                raise ValueError("策略实验开始日期不能晚于结束日期")
            if (self.end_date - self.start_date).days < 2:
                raise ValueError("策略实验时间范围至少需要 3 天")
        return self

    def to_discovery_request(
        self,
        *,
        default_start: date | str | None = None,
        default_end: date | str | None = None,
    ) -> DiscoveryRequest:

        catalog = default_indicator_catalog()
        # Condition operands span every numeric output, not just the rankable
        # ones. Rankability is a cross-stock sorting concern: prices and moving
        # averages are perfectly valid operands ("close 上穿 MA20") but must
        # never become ranking features. Using rankable_ids here wrongly
        # rejected price-based conditions.
        allowed_values = set(catalog.numeric_ids)

        def canonical_conditions(
            conditions: list[ScreenConditionRequest], field_name: str
        ) -> tuple[ScreenCondition, ...]:
            result: list[ScreenCondition] = []
            for condition in conditions:
                left_value = catalog.canonical_id(condition.left_value)
                right_value = (
                    catalog.canonical_id(condition.right_value)
                    if condition.right_value is not None
                    else None
                )
                referenced = [left_value, right_value] if right_value is not None else [left_value]
                invalid = [value for value in referenced if value not in allowed_values]
                if invalid:
                    raise ValueError(f"{field_name} 使用了不支持的指标: {', '.join(invalid)}")
                result.append(
                    ScreenCondition(
                        left_value=left_value,
                        operator=condition.operator,
                        right_value=right_value,
                        right_constant=condition.right_constant,
                    )
                )
            return tuple(result)

        required_buy_conditions = canonical_conditions(self.required_buy_conditions, "必选买入条件")
        required_sell_conditions = canonical_conditions(
            self.required_sell_conditions, "必选卖出条件"
        )
        resolved_start = self.start_date or coerce_date(default_start)
        resolved_end = self.end_date or coerce_date(default_end)
        if resolved_start is None or resolved_end is None:
            raise ValueError("本地 DuckDB 没有已完成日线，请先在“本地行情”页面导入日线数据")
        if resolved_start > resolved_end:
            raise ValueError("策略实验开始日期不能晚于结束日期")
        if (resolved_end - resolved_start).days < 2:
            raise ValueError("策略实验时间范围至少需要 3 天")

        total_days = (resolved_end - resolved_start).days + 1
        search_days = max(1, int(total_days * 0.6))
        validation_days = max(1, int(total_days * 0.2))
        search_end = resolved_start + timedelta(days=search_days - 1)
        validation_start = search_end + timedelta(days=1)
        validation_end = validation_start + timedelta(days=validation_days - 1)
        holdout_start = validation_end + timedelta(days=1)
        seed = self.seed if self.seed is not None else secrets.randbits(32)
        return DiscoveryRequest(
            base_config={
                "vipdoc_path": self.vipdoc_path,
                "universe": self.universe,
                "universe_file": self.universe_file,
                "instrument_types": list(self.instrument_types or ()),
                "boards": list(self.boards or ()),
                "date_mode": (
                    "custom"
                    if self.start_date is not None or self.end_date is not None
                    else self.date_mode
                ),
                "start_date": resolved_start.isoformat(),
                "end_date": resolved_end.isoformat(),
                "max_positions": self.max_positions,
                "initial_cash": self.initial_cash,
                "commission": self.commission,
                "min_commission": self.min_commission,
                "stamp_tax": self.stamp_tax,
                "slippage": self.slippage,
                "execution": self.execution,
            },
            splits={
                "search": {
                    "start_date": resolved_start.isoformat(),
                    "end_date": search_end.isoformat(),
                },
                "validation": {
                    "start_date": validation_start.isoformat(),
                    "end_date": validation_end.isoformat(),
                },
                "holdout": {
                    "start_date": holdout_start.isoformat(),
                    "end_date": resolved_end.isoformat(),
                },
            },
            indicator_ids=tuple(catalog.rankable_ids),
            signal_ids=tuple(catalog.signal_ids),
            required_buy_conditions=required_buy_conditions,
            required_sell_conditions=required_sell_conditions,
            max_trials=self.max_trials,
            timeout_seconds=self.timeout_seconds,
            validation_candidates=self.validation_candidates,
            holdout_candidates=self.holdout_candidates,
            min_trades=self.min_trades,
            max_drawdown=self.max_drawdown,
            max_turnover=self.max_turnover,
            max_complexity=self.max_complexity,
            multifidelity_enabled=self.multifidelity_enabled,
            seed=seed,
        )


class StrategyReplayRequest(BaseModel):
    """Request to replay the current top-ten leaderboard on full local history."""

    model_config = ConfigDict(extra="forbid")

    leaderboard: Literal["sharpe", "return"]


class StrategyRefinementRequest(BaseModel):
    """Request to search a bounded neighborhood around one stored candidate."""

    model_config = ConfigDict(extra="forbid")

    parent_history_id: str = Field(min_length=1, max_length=128)
    parent_candidate_hash: str = Field(min_length=1, max_length=128)
    refinement_radius: Literal["medium"] = "medium"
    max_trials: int = Field(default=1_000, ge=1, le=5_000)
    seed: int | None = Field(default=None, ge=0, le=2**32 - 1)


class StrategyFitnessRequest(PortfolioBacktestRequest):
    """Batch single-stock strategy suitability request."""

    max_positions: Literal[1] = 1
    rebalance_frequency: Literal["daily"] = "daily"
    train_ratio: float = Field(default=0.6, gt=0, lt=1)
    validation_ratio: float = Field(default=0.2, gt=0, lt=1)
    min_trades: int = Field(default=5, ge=1, le=10_000)
    max_test_drawdown: float = Field(default=0.3, ge=0, le=1)

    @model_validator(mode="after")
    def validate_fitness_split(self) -> StrategyFitnessRequest:
        if self.train_ratio + self.validation_ratio >= 1:
            raise ValueError("训练和验证比例之和必须小于 1")
        return self

    def to_fitness_config(self) -> StrategyFitnessConfig:
        return StrategyFitnessConfig(
            strategy=self.to_config(),
            train_ratio=self.train_ratio,
            validation_ratio=self.validation_ratio,
            min_trades=self.min_trades,
            max_test_drawdown=self.max_test_drawdown,
        )


class SavedStrategyCreateRequest(BaseModel):
    """One reusable portfolio strategy to persist.

    The request is validated with the same model the backtest endpoint uses, so
    a strategy that cannot be replayed is rejected at save time instead of
    failing months later when the tracking job tries to run it.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=60)
    note: str = Field(default="", max_length=500)
    request: PortfolioBacktestRequest
    summary: dict[str, object] = Field(default_factory=dict)
    source_history_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_name(self) -> SavedStrategyCreateRequest:
        if not self.name.strip():
            raise ValueError("策略名称不能为空")
        return self


class SavedStrategyUpdateRequest(BaseModel):
    """Rename or re-note a saved strategy. The configuration stays frozen."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=60)
    note: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_payload(self) -> SavedStrategyUpdateRequest:
        if self.name is None and self.note is None:
            raise ValueError("至少要修改名称或备注之一")
        if self.name is not None and not self.name.strip():
            raise ValueError("策略名称不能为空")
        return self
