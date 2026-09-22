"""单股回测请求体。"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from selector_app.backtest.models import BacktestConfig

from ..date_ranges import (
    DateRangeMode,
    validate_date_range_request,
)
from ._shared import (
    validate_formula_conditions,
    validate_formula_signals,
)
from .screening import RuleGroupRequest


class BacktestRequest(BaseModel):
    """Single-stock daily backtest request."""

    model_config = ConfigDict(extra="forbid")

    market: Literal["SH", "SZ"] = "SH"
    code: str = Field(pattern=r"^\d{6}$")
    vipdoc_path: str = Field(default="", max_length=1024)
    buy_signal: str | None = Field(default=None, min_length=1, max_length=128)
    sell_signal: str | None = Field(default=None, min_length=1, max_length=128)
    buy_rule: RuleGroupRequest | None = None
    sell_rule: RuleGroupRequest | None = None
    formula_text: str | None = Field(default=None, max_length=20_000)
    formula_parameters: dict[str, float] = Field(default_factory=dict, max_length=32)
    date_mode: DateRangeMode = "recent5"
    start_date: date | None = None
    end_date: date | None = None
    initial_cash: float = Field(default=100_000.0, gt=0, le=1_000_000_000_000)
    commission: float = Field(default=0.0001, ge=0, le=0.1)
    min_commission: float = Field(default=0.1, ge=0, le=100_000)
    stamp_tax: float = Field(default=0.001, ge=0, le=0.1)
    slippage: float = Field(default=0.0, ge=0, le=1_000)
    execution: Literal["next_open", "next_close"] = "next_open"
    position_mode: Literal["full", "fixed"] = "full"
    fixed_size: int | None = Field(default=None, ge=100, le=100_000_000)

    @model_validator(mode="after")
    def validate_backtest(self) -> BacktestRequest:
        validate_date_range_request(self.date_mode, self.start_date, self.end_date)
        if self.buy_signal is None and self.buy_rule is None:
            raise ValueError("至少设置一个买入信号或指标条件")
        if self.sell_signal is None and self.sell_rule is None:
            raise ValueError("至少设置一个卖出信号或指标条件")
        if self.buy_rule is None and self.sell_rule is None and self.buy_signal == self.sell_signal:
            raise ValueError("买入信号和卖出信号不能相同")
        signal_ids = [
            *([self.buy_signal] if self.buy_signal is not None else []),
            *([self.sell_signal] if self.sell_signal is not None else []),
            *(self.buy_rule.signals if self.buy_rule is not None else []),
            *(self.sell_rule.signals if self.sell_rule is not None else []),
        ]
        validate_formula_signals(
            self.formula_text,
            signal_ids,
            self.formula_parameters,
        )
        conditions = [
            *(self.buy_rule.required_conditions if self.buy_rule is not None else []),
            *(self.buy_rule.conditions if self.buy_rule is not None else []),
            *(self.sell_rule.required_conditions if self.sell_rule is not None else []),
            *(self.sell_rule.conditions if self.sell_rule is not None else []),
        ]
        if conditions:
            validate_formula_conditions(
                self.formula_text,
                [condition.to_model() for condition in conditions],
            )
        if self.start_date is not None and self.end_date is not None:
            if self.start_date > self.end_date:
                raise ValueError("回测开始日期不能晚于结束日期")
        if self.position_mode == "fixed":
            if self.fixed_size is None or self.fixed_size % 100 != 0:
                raise ValueError("固定股数必须是 100 的整数倍")
        elif self.fixed_size is not None:
            raise ValueError("全仓模式不应设置固定股数")
        return self

    def to_config(self) -> BacktestConfig:
        return BacktestConfig(
            market=self.market,
            code=self.code,
            vipdoc_path=self.vipdoc_path,
            buy_signal=self.buy_signal,
            sell_signal=self.sell_signal,
            buy_rule=self.buy_rule.to_model() if self.buy_rule is not None else None,
            sell_rule=self.sell_rule.to_model() if self.sell_rule is not None else None,
            formula_text=self.formula_text,
            formula_parameters=self.formula_parameters,
            start_date=self.start_date,
            end_date=self.end_date,
            initial_cash=self.initial_cash,
            commission=self.commission,
            min_commission=self.min_commission,
            stamp_tax=self.stamp_tax,
            slippage=self.slippage,
            execution=self.execution,
            position_mode=self.position_mode,
            fixed_size=self.fixed_size,
        )
