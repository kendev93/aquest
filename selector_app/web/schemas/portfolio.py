"""组合回测请求体。"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from selector_app.market_data.adapter import parse_universe_code
from selector_app.market_data.models import InstrumentBoard, InstrumentType
from selector_app.portfolio_backtest.models import (
    PortfolioBacktestConfig,
    PortfolioUniverse,
)

from ..date_ranges import (
    DateRangeMode,
    validate_date_range_request,
)
from ._shared import (
    validate_formula_signals,
    validate_formula_values,
    validate_instrument_scope,
)
from .screening import RuleGroupRequest


class PortfolioBacktestRequest(BaseModel):
    """Dynamic ranked-slot portfolio backtest request."""

    model_config = ConfigDict(extra="forbid")

    vipdoc_path: str = Field(default="", max_length=1024)
    universe: PortfolioUniverse = "all"
    universe_file: str | None = Field(default=None, max_length=1024)
    codes: list[str] | None = Field(default=None, max_length=200)
    instrument_types: list[InstrumentType] | None = Field(default=None, max_length=1)
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
    take_profit_mode: Literal["pct", "atr"] = "pct"
    take_profit_atr_multiplier: float | None = Field(default=None, gt=0, le=100)
    take_profit_cooldown_days: int = Field(default=0, ge=0, le=10_000)
    stop_loss_mode: Literal["pct", "atr"] = "pct"
    stop_loss_atr_multiplier: float | None = Field(default=None, gt=0, le=100)
    stop_loss_cooldown_days: int = Field(default=0, ge=0, le=10_000)
    trailing_stop_pct: float | None = Field(default=None, gt=0, le=10)
    trailing_stop_mode: Literal["pct", "atr"] = "pct"
    trailing_stop_atr_multiplier: float | None = Field(default=None, gt=0, le=100)
    trailing_stop_cooldown_days: int = Field(default=0, ge=0, le=10_000)
    atr_period: int = Field(default=14, ge=1, le=1_000)
    sell_value: str | None = Field(default=None, max_length=128)
    sell_value_operator: Literal["gte", "lte"] | None = None
    sell_value_threshold: float | None = None
    compare_left_value: str | None = Field(default=None, max_length=128)
    compare_operator: Literal["gt", "gte", "lt", "lte"] | None = None
    compare_right_value: str | None = Field(default=None, max_length=128)
    sell_rule: RuleGroupRequest | None = None
    date_mode: DateRangeMode = "recent5"
    start_date: date | None = None
    end_date: date | None = None
    initial_cash: float = Field(default=1_000_000.0, gt=0, le=1_000_000_000_000)
    commission: float = Field(default=0.0001, ge=0, le=0.1)
    min_commission: float = Field(default=0.1, ge=0, le=100_000)
    stamp_tax: float = Field(default=0.001, ge=0, le=0.1)
    slippage: float = Field(default=0.0, ge=0, le=1_000)
    execution: Literal["next_open", "next_close"] = "next_open"
    fitness_rank_weight: float = Field(default=0.0, ge=0, le=1)
    fitness_min_trades: int = Field(default=5, ge=1, le=10_000)
    fitness_max_drawdown: float = Field(default=0.3, ge=0, le=1)
    position_mode: Literal["full", "fixed"] = "full"
    fixed_size: int | None = Field(default=None, ge=100, le=100_000_000)
    #: 排除 ST / *ST / 退市标的（按当前名称判定，历史回测下偏严格）。
    exclude_st: bool = False

    @model_validator(mode="after")
    def validate_portfolio_backtest(self) -> PortfolioBacktestRequest:
        validate_date_range_request(self.date_mode, self.start_date, self.end_date)
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
            *(self.buy_rule.required_conditions if self.buy_rule is not None else []),
            *(self.buy_rule.conditions if self.buy_rule is not None else []),
            *(self.sell_rule.required_conditions if self.sell_rule is not None else []),
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
        if self.universe == "codes":
            if not self.codes:
                raise ValueError("指定标的范围必须填写至少一个证券代码")
            for raw in self.codes:
                try:
                    parse_universe_code(raw)
                except ValueError as exc:
                    raise ValueError(f"证券代码格式无效: {raw}") from exc
        elif self.codes:
            raise ValueError("只有指定标的范围才需要填写证券代码")
        if self.position_mode == "fixed" and self.fixed_size is None:
            raise ValueError("固定股数模式必须设置买入股数")
        if self.position_mode == "full" and self.fixed_size is not None:
            raise ValueError("按比例建仓不应设置买入股数")
        # A 股一手 100 股，模拟器会把固定股数向下取整到整手。不在这里挡住，
        # 填 150 会被静默变成 100——用户以为自己改了仓位，实际没变。
        if self.fixed_size is not None and self.fixed_size % 100 != 0:
            raise ValueError("买入股数必须是 100 的整数倍（A 股一手 100 股）")
        if self.start_date is not None and self.end_date is not None:
            if self.start_date > self.end_date:
                raise ValueError("回测开始日期不能晚于结束日期")
        for prefix in ("take_profit", "stop_loss", "trailing_stop"):
            mode = getattr(self, f"{prefix}_mode")
            multiplier = getattr(self, f"{prefix}_atr_multiplier")
            percentage = getattr(self, f"{prefix}_pct")
            if mode == "atr" and multiplier is None:
                raise ValueError(f"{prefix}_atr_multiplier 在 ATR 模式下不能为空")
            if mode == "atr" and percentage is not None:
                raise ValueError(f"{prefix}_pct 不能在 ATR 模式下设置")
            if mode == "pct" and multiplier is not None:
                raise ValueError(f"{prefix}_atr_multiplier 不能在 pct 模式下设置")
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
        if self.atr_period != 14:
            raise ValueError("atr_period 固定为 14")
        risk_enabled = any(
            value is not None
            for value in (
                self.stop_loss_pct,
                self.stop_loss_atr_multiplier,
                self.take_profit_pct,
                self.take_profit_atr_multiplier,
                self.trailing_stop_pct,
                self.trailing_stop_atr_multiplier,
            )
        )
        if (
            self.sell_rule is None
            and self.sell_signal is None
            and not risk_enabled
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
            codes=tuple(self.codes) if self.codes is not None else None,
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
            take_profit_mode=self.take_profit_mode,
            take_profit_atr_multiplier=self.take_profit_atr_multiplier,
            take_profit_cooldown_days=self.take_profit_cooldown_days,
            stop_loss_mode=self.stop_loss_mode,
            stop_loss_atr_multiplier=self.stop_loss_atr_multiplier,
            stop_loss_cooldown_days=self.stop_loss_cooldown_days,
            trailing_stop_pct=self.trailing_stop_pct,
            trailing_stop_mode=self.trailing_stop_mode,
            trailing_stop_atr_multiplier=self.trailing_stop_atr_multiplier,
            trailing_stop_cooldown_days=self.trailing_stop_cooldown_days,
            atr_period=self.atr_period,
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
            fitness_rank_weight=self.fitness_rank_weight,
            fitness_min_trades=self.fitness_min_trades,
            fitness_max_drawdown=self.fitness_max_drawdown,
            position_mode=self.position_mode,
            fixed_size=self.fixed_size,
            exclude_st=self.exclude_st,
        )
