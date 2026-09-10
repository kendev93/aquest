"""Pydantic request/response schemas for formula-screen APIs."""

from __future__ import annotations

import math
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from selector_app.backtest.models import BacktestConfig
from selector_app.concurrency import MAX_WORKERS
from selector_app.formulas.custom import FormulaParseError, custom_output_id, parse_formula
from selector_app.formulas.registry import FORMULA_REGISTRY
from selector_app.market_data.models import InstrumentBoard, InstrumentType
from selector_app.market_data.scope import InstrumentScope
from selector_app.portfolio_backtest.models import (
    PortfolioBacktestConfig,
    PortfolioUniverse,
)
from selector_app.screening.models import ScanConfig, ScreenCondition
from selector_app.screening.rules import RuleGroup
from selector_app.strategy_fitness.models import StrategyFitnessConfig


def validate_formula_signals(
    formula_text: str | None,
    signal_ids: list[str],
    formula_parameters: dict[str, float],
) -> None:
    """Validate signal ids and parameter overrides for preset/custom formulas."""

    if formula_text:
        try:
            parsed = parse_formula(formula_text)
        except FormulaParseError as exc:
            raise ValueError(str(exc)) from exc
        available = {signal.id for signal in parsed.signals}
        invalid = [signal for signal in signal_ids if signal not in available]
        known_parameters = {parameter.name for parameter in parsed.parameters}
        unknown_parameters = sorted(set(formula_parameters) - known_parameters)
        if unknown_parameters:
            raise ValueError(f"未知公式参数: {', '.join(unknown_parameters)}")
        parameters_by_name = {parameter.name: parameter for parameter in parsed.parameters}
        for name, value in formula_parameters.items():
            definition = parameters_by_name[name]
            if not math.isfinite(value) or not definition.minimum <= value <= definition.maximum:
                raise ValueError(
                    f"参数 {name} 必须在 {definition.minimum:g} 到 {definition.maximum:g} 之间"
                )
            if definition.step == 1 and not value.is_integer():
                raise ValueError(f"参数 {name} 必须是整数")
    else:
        invalid = [signal for signal in signal_ids if not FORMULA_REGISTRY.has_signal(signal)]
        if formula_parameters:
            raise ValueError("预置公式不接受自定义参数")
    if invalid:
        raise ValueError(f"未知选股信号: {', '.join(invalid)}")


def validate_formula_values(formula_text: str | None, value_ids: list[str]) -> None:
    """Validate numeric output identifiers used for ranking or exits."""

    if formula_text:
        try:
            parsed = parse_formula(formula_text)
        except FormulaParseError as exc:
            raise ValueError(str(exc)) from exc
        available = {custom_output_id(name) for name in parsed.value_names}
        invalid = [value_id for value_id in value_ids if value_id not in available]
    else:
        invalid = [value_id for value_id in value_ids if not FORMULA_REGISTRY.has_value(value_id)]
    if invalid:
        raise ValueError(f"未知指标输出: {', '.join(invalid)}")


def validate_formula_conditions(
    formula_text: str | None, conditions: list[ScreenCondition]
) -> None:
    """Validate every numeric output referenced by a screening condition."""

    if formula_text:
        try:
            parsed = parse_formula(formula_text)
        except FormulaParseError as exc:
            raise ValueError(str(exc)) from exc
        available = {custom_output_id(name) for name in parsed.value_names}
        invalid = [
            value_id
            for condition in conditions
            for value_id in (condition.left_value, condition.right_value)
            if value_id is not None and value_id not in available
        ]
    else:
        invalid = [
            value_id
            for condition in conditions
            for value_id in (condition.left_value, condition.right_value)
            if value_id is not None and not FORMULA_REGISTRY.has_value(value_id)
        ]
    if invalid:
        raise ValueError(f"未知指标输出: {', '.join(dict.fromkeys(invalid))}")


def validate_instrument_scope(
    universe: str,
    instrument_types: list[InstrumentType] | None,
    boards: list[InstrumentBoard] | None,
) -> None:
    InstrumentScope.from_values(
        universe=universe,
        instrument_types=instrument_types,
        boards=boards,
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
    combine_mode: Literal["all", "any", "at_least"] = "all"
    minimum_matches: int | None = Field(default=None, ge=1, le=64)

    @model_validator(mode="after")
    def validate_group(self) -> RuleGroupRequest:
        signals = [signal.strip() for signal in self.signals]
        object.__setattr__(self, "signals", signals)
        if not signals and not self.conditions:
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
            for condition in self.conditions
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
        )


class FormulaScreenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected_signals: list[str] = Field(default_factory=list, max_length=32)
    conditions: list[ScreenConditionRequest] = Field(default_factory=list, max_length=32)
    combine_mode: Literal["all", "any", "at_least"] = "at_least"
    minimum_matches: int | None = Field(default=None, ge=1, le=32)
    universe: Literal["all", "sh", "sz", "custom"] = "all"
    universe_file: str | None = Field(default=None, max_length=1024)
    instrument_types: list[InstrumentType] | None = Field(default=None, max_length=4)
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


class MarketSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vipdoc_path: str | None = Field(default=None, max_length=1024)
    universe: Literal["all", "sh", "sz"] = "all"
    bars: int = Field(default=800, ge=1, le=800)
    instrument_types: list[InstrumentType] | None = Field(default=None, max_length=4)
    boards: list[InstrumentBoard] | None = Field(default=None, max_length=7)

    @model_validator(mode="after")
    def validate_scope(self) -> MarketSyncRequest:
        validate_instrument_scope(self.universe, self.instrument_types, self.boards)
        return self


class LocalMarketImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vipdoc_path: str = Field(min_length=1, max_length=1024)
    universe: Literal["all", "sh", "sz"] = "all"
    instrument_types: list[InstrumentType] | None = Field(default=None, max_length=4)
    boards: list[InstrumentBoard] | None = Field(default=None, max_length=7)

    @model_validator(mode="after")
    def validate_scope(self) -> LocalMarketImportRequest:
        validate_instrument_scope(self.universe, self.instrument_types, self.boards)
        return self


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
    start_date: date | None = None
    end_date: date | None = None
    initial_cash: float = Field(default=100_000.0, gt=0, le=1_000_000_000_000)
    commission: float = Field(default=0.0003, ge=0, le=0.1)
    min_commission: float = Field(default=5.0, ge=0, le=100_000)
    stamp_tax: float = Field(default=0.001, ge=0, le=0.1)
    slippage: float = Field(default=0.0, ge=0, le=1_000)
    execution: Literal["next_open", "next_close"] = "next_open"
    position_mode: Literal["full", "fixed"] = "full"
    fixed_size: int | None = Field(default=None, ge=100, le=100_000_000)

    @model_validator(mode="after")
    def validate_backtest(self) -> BacktestRequest:
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
            *(self.buy_rule.conditions if self.buy_rule is not None else []),
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


class PortfolioBacktestRequest(BaseModel):
    """Dynamic ranked-slot portfolio backtest request."""

    model_config = ConfigDict(extra="forbid")

    vipdoc_path: str = Field(default="", max_length=1024)
    universe: PortfolioUniverse = "all"
    universe_file: str | None = Field(default=None, max_length=1024)
    instrument_types: list[InstrumentType] | None = Field(default=None, max_length=4)
    boards: list[InstrumentBoard] | None = Field(default=None, max_length=7)
    selected_signals: list[str] = Field(default_factory=list, max_length=32)
    combine_mode: Literal["all", "any", "at_least"] = "any"
    minimum_matches: int | None = Field(default=None, ge=1, le=32)
    buy_rule: RuleGroupRequest | None = None
    ranking_value: str = Field(min_length=1, max_length=128)
    rank_order: Literal["asc", "desc"] = "desc"
    max_positions: int = Field(default=5, ge=1, le=100)
    rebalance_frequency: Literal["daily", "weekly", "monthly"] = "daily"
    formula_text: str | None = Field(default=None, max_length=20_000)
    formula_parameters: dict[str, float] = Field(default_factory=dict, max_length=32)
    sell_signal: str | None = Field(default=None, max_length=128)
    stop_loss_pct: float | None = Field(default=None, gt=0, le=1)
    take_profit_pct: float | None = Field(default=None, gt=0, le=10)
    sell_value: str | None = Field(default=None, max_length=128)
    sell_value_operator: Literal["gte", "lte"] | None = None
    sell_value_threshold: float | None = None
    compare_left_value: str | None = Field(default=None, max_length=128)
    compare_operator: Literal["gt", "gte", "lt", "lte"] | None = None
    compare_right_value: str | None = Field(default=None, max_length=128)
    sell_rule: RuleGroupRequest | None = None
    start_date: date | None = None
    end_date: date | None = None
    initial_cash: float = Field(default=1_000_000.0, gt=0, le=1_000_000_000_000)
    commission: float = Field(default=0.0003, ge=0, le=0.1)
    min_commission: float = Field(default=5.0, ge=0, le=100_000)
    stamp_tax: float = Field(default=0.001, ge=0, le=0.1)
    slippage: float = Field(default=0.0, ge=0, le=1_000)
    execution: Literal["next_open", "next_close"] = "next_open"
    fitness_filter_enabled: bool = False
    fitness_min_score: float = Field(default=75.0, ge=0, le=100)
    fitness_min_trades: int = Field(default=5, ge=1, le=10_000)
    fitness_max_drawdown: float = Field(default=0.3, ge=0, le=1)

    @model_validator(mode="after")
    def validate_portfolio_backtest(self) -> PortfolioBacktestRequest:
        signal_ids = list(self.selected_signals)
        if self.sell_signal is not None:
            signal_ids.append(self.sell_signal)
        if self.buy_rule is not None:
            signal_ids.extend(self.buy_rule.signals)
        if self.sell_rule is not None:
            signal_ids.extend(self.sell_rule.signals)
        validate_formula_signals(self.formula_text, signal_ids, self.formula_parameters)
        validate_instrument_scope(self.universe, self.instrument_types, self.boards)
        conditions = [
            *(self.buy_rule.conditions if self.buy_rule is not None else []),
            *(self.sell_rule.conditions if self.sell_rule is not None else []),
        ]
        validate_formula_values(
            self.formula_text,
            [
                self.ranking_value,
                *([self.sell_value] if self.sell_value is not None else []),
                *([self.compare_left_value] if self.compare_left_value is not None else []),
                *([self.compare_right_value] if self.compare_right_value is not None else []),
                *[
                    value_id
                    for condition in conditions
                    for value_id in (condition.left_value, condition.right_value)
                    if value_id is not None
                ],
            ],
        )
        if len(set(self.selected_signals)) != len(self.selected_signals):
            raise ValueError("选股条件不能重复")
        if self.buy_rule is None and not self.selected_signals:
            raise ValueError("至少设置一个买入信号或指标条件")
        if self.buy_rule is None and self.combine_mode == "at_least":
            if self.minimum_matches is None or self.minimum_matches > len(self.selected_signals):
                raise ValueError("minimum_matches 不能大于已选择条件数量")
        elif self.buy_rule is None and self.minimum_matches is not None:
            raise ValueError("all/any 模式不应设置 minimum_matches")
        if self.universe == "custom" and not self.universe_file:
            raise ValueError("自定义股票范围必须设置 universe_file")
        if self.start_date is not None and self.end_date is not None:
            if self.start_date > self.end_date:
                raise ValueError("回测开始日期不能晚于结束日期")
        if self.sell_value is None:
            if self.sell_value_operator is not None or self.sell_value_threshold is not None:
                raise ValueError("指标阈值卖出规则缺少指标")
        elif self.sell_value_operator is None or self.sell_value_threshold is None:
            raise ValueError("指标阈值卖出规则需要指标、比较方式和阈值")
        comparison = (self.compare_left_value, self.compare_operator, self.compare_right_value)
        if any(value is not None for value in comparison) and not all(
            value is not None for value in comparison
        ):
            raise ValueError("指标比较卖出规则需要左右指标和比较方式")
        if (
            self.sell_rule is None
            and self.sell_signal is None
            and self.stop_loss_pct is None
            and self.take_profit_pct is None
            and self.sell_value is None
            and self.compare_left_value is None
        ):
            raise ValueError("至少需要设置一个卖出规则")
        return self

    def to_config(self) -> PortfolioBacktestConfig:
        return PortfolioBacktestConfig(
            vipdoc_path=self.vipdoc_path,
            universe=self.universe,
            universe_file=self.universe_file,
            instrument_types=tuple(self.instrument_types or ()),
            boards=tuple(self.boards or ()),
            selected_signals=tuple(self.selected_signals),
            combine_mode=self.combine_mode,
            minimum_matches=self.minimum_matches,
            buy_rule=self.buy_rule.to_model() if self.buy_rule is not None else None,
            ranking_value=self.ranking_value,
            rank_order=self.rank_order,
            max_positions=self.max_positions,
            rebalance_frequency=self.rebalance_frequency,
            formula_text=self.formula_text,
            formula_parameters=self.formula_parameters,
            sell_signal=self.sell_signal,
            stop_loss_pct=self.stop_loss_pct,
            take_profit_pct=self.take_profit_pct,
            sell_value=self.sell_value,
            sell_value_operator=self.sell_value_operator,
            sell_value_threshold=self.sell_value_threshold,
            compare_left_value=self.compare_left_value,
            compare_operator=self.compare_operator,
            compare_right_value=self.compare_right_value,
            sell_rule=self.sell_rule.to_model() if self.sell_rule is not None else None,
            start_date=self.start_date,
            end_date=self.end_date,
            initial_cash=self.initial_cash,
            commission=self.commission,
            min_commission=self.min_commission,
            stamp_tax=self.stamp_tax,
            slippage=self.slippage,
            execution=self.execution,
            fitness_filter_enabled=self.fitness_filter_enabled,
            fitness_min_score=self.fitness_min_score,
            fitness_min_trades=self.fitness_min_trades,
            fitness_max_drawdown=self.fitness_max_drawdown,
        )


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
