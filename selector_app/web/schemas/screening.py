"""选股/公式/本地导入相关的请求体。"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from selector_app.concurrency import MAX_WORKERS
from selector_app.market_data.models import InstrumentBoard, InstrumentType
from selector_app.screening.models import ScanConfig, ScreenCondition
from selector_app.screening.rules import RuleGroup

from ._shared import (
    validate_formula_conditions,
    validate_formula_signals,
    validate_instrument_scope,
)


class ScreenConditionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    left_value: str = Field(min_length=1, max_length=128)
    operator: Literal["gt", "gte", "lt", "lte", "eq", "neq", "cross_up", "cross_down"]
    right_value: str | None = Field(default=None, max_length=128)
    right_constant: float | None = None

    @model_validator(mode="after")
    def validate_right_operand(self) -> ScreenConditionRequest:
        has_value = self.right_value is not None
        has_constant = self.right_constant is not None
        if has_value == has_constant:
            raise ValueError("条件必须且只能设置一个右值")
        if self.right_value is not None and not self.right_value.strip():
            raise ValueError("条件右侧指标不能为空")
        if self.right_constant is not None and (
            isinstance(self.right_constant, bool) or not math.isfinite(self.right_constant)
        ):
            raise ValueError("条件常数必须是有限数字")
        return self

    def to_model(self) -> ScreenCondition:
        return ScreenCondition(
            left_value=self.left_value,
            operator=self.operator,
            right_value=self.right_value,
            right_constant=self.right_constant,
        )


class RuleGroupRequest(BaseModel):
    """A reusable buy/sell rule group for simulations."""

    model_config = ConfigDict(extra="forbid")

    signals: list[str] = Field(default_factory=list, max_length=32)
    conditions: list[ScreenConditionRequest] = Field(default_factory=list, max_length=32)
    required_conditions: list[ScreenConditionRequest] = Field(default_factory=list, max_length=32)
    combine_mode: Literal["all", "any", "at_least"] = "all"
    minimum_matches: int | None = Field(default=None, ge=1, le=64)

    @model_validator(mode="after")
    def validate_group(self) -> RuleGroupRequest:
        signals = [signal.strip() for signal in self.signals]
        object.__setattr__(self, "signals", signals)
        if not signals and not self.conditions and not self.required_conditions:
            raise ValueError("交易规则至少需要一个信号或指标条件")
        if any(not signal for signal in signals):
            raise ValueError("交易规则信号不能为空")
        if len(set(signals)) != len(signals):
            raise ValueError("交易规则信号不能重复")
        condition_keys = [
            (
                condition.left_value,
                condition.operator,
                condition.right_value,
                condition.right_constant,
            )
            for condition in (*self.required_conditions, *self.conditions)
        ]
        if len(set(condition_keys)) != len(condition_keys):
            raise ValueError("交易规则指标条件不能重复")
        count = len(signals) + len(self.conditions)
        if self.combine_mode == "at_least":
            if self.minimum_matches is None or self.minimum_matches > count:
                raise ValueError("交易规则 minimum_matches 不能大于条件数量")
        elif self.minimum_matches is not None:
            raise ValueError("all/any 交易规则不应设置 minimum_matches")
        return self

    def to_model(self) -> RuleGroup:
        return RuleGroup(
            signals=tuple(self.signals),
            conditions=tuple(condition.to_model() for condition in self.conditions),
            combine_mode=self.combine_mode,
            minimum_matches=self.minimum_matches,
            required_conditions=tuple(
                condition.to_model() for condition in self.required_conditions
            ),
        )


class FormulaScreenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected_signals: list[str] = Field(default_factory=list, max_length=32)
    conditions: list[ScreenConditionRequest] = Field(default_factory=list, max_length=32)
    combine_mode: Literal["all", "any", "at_least"] = "at_least"
    minimum_matches: int | None = Field(default=None, ge=1, le=32)
    universe: Literal["all", "sh", "sz", "custom"] = "all"
    universe_file: str | None = Field(default=None, max_length=1024)
    instrument_types: list[InstrumentType] | None = Field(default=None, max_length=1)
    boards: list[InstrumentBoard] | None = Field(default=None, max_length=7)
    vipdoc_path: str = Field(default="", max_length=1024)
    workers: int = Field(default=MAX_WORKERS, ge=1, le=MAX_WORKERS)
    period: Literal["daily"] = "daily"
    formula_text: str | None = Field(default=None, max_length=20_000)
    formula_parameters: dict[str, float] = Field(default_factory=dict, max_length=32)

    @model_validator(mode="after")
    def validate_selection(self) -> FormulaScreenRequest:
        if not self.selected_signals and not self.conditions:
            raise ValueError("至少选择一个信号或设置一条指标条件")
        if self.selected_signals and self.conditions:
            raise ValueError("快捷信号和指标条件不能同时使用")
        validate_formula_signals(
            self.formula_text,
            self.selected_signals,
            self.formula_parameters,
        )
        condition_models = [condition.to_model() for condition in self.conditions]
        if condition_models:
            validate_formula_conditions(self.formula_text, condition_models)
        validate_instrument_scope(self.universe, self.instrument_types, self.boards)
        if len(set(self.selected_signals)) != len(self.selected_signals):
            raise ValueError("选股信号不能重复")
        condition_keys = [
            (
                condition.left_value,
                condition.operator,
                condition.right_value,
                condition.right_constant,
            )
            for condition in self.conditions
        ]
        if len(set(condition_keys)) != len(condition_keys):
            raise ValueError("指标条件不能重复")
        condition_count = len(self.conditions) or len(self.selected_signals)
        if self.combine_mode == "at_least":
            if self.minimum_matches is None:
                raise ValueError("至少满足 N 个模式必须设置 minimum_matches")
            if self.minimum_matches > condition_count:
                raise ValueError("minimum_matches 不能大于已选择条件数量")
        if self.universe == "custom" and not self.universe_file:
            raise ValueError("自定义股票范围必须设置 universe_file")
        return self

    def to_config(self) -> ScanConfig:
        return ScanConfig(
            selected_signals=tuple(self.selected_signals),
            combine_mode=self.combine_mode,
            minimum_matches=self.minimum_matches,
            universe=self.universe,
            universe_file=self.universe_file,
            vipdoc_path=self.vipdoc_path,
            workers=self.workers,
            period=self.period,
            instrument_types=tuple(self.instrument_types or ()),
            boards=tuple(self.boards or ()),
            formula_text=self.formula_text,
            formula_parameters=self.formula_parameters,
            conditions=tuple(condition.to_model() for condition in self.conditions),
        )


class JobCreatedResponse(BaseModel):
    job_id: str
    status: str


class CustomFormulaParseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    formula_text: str = Field(min_length=1, max_length=20_000)


class LocalImportRequest(BaseModel):
    """Import bars from a local TongDaXin ``vipdoc`` tree.

    Every parsed bar overwrites the stored row on the same ``(market, code,
    trade_date)`` key, so a freshly downloaded full history wins over whatever
    an earlier online sync wrote.
    """

    model_config = ConfigDict(extra="forbid")

    vipdoc_path: str | None = Field(default=None, max_length=1024)
    universe: Literal["all", "sh", "sz"] = "all"
    instrument_types: list[InstrumentType] | None = Field(default=None, max_length=1)
    boards: list[InstrumentBoard] | None = Field(default=None, max_length=7)

    @model_validator(mode="after")
    def validate_scope(self) -> LocalImportRequest:
        validate_instrument_scope(self.universe, self.instrument_types, self.boards)
        return self
