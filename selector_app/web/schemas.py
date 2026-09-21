"""Pydantic request/response schemas for formula-screen APIs."""

from __future__ import annotations

import math
import secrets
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from selector_app.backtest.models import BacktestConfig
from selector_app.concurrency import MAX_WORKERS
from selector_app.condition_research.mining import DEFAULT_DUPLICATE_CORRELATION
from selector_app.condition_research.models import ConditionResearchConfig
from selector_app.factor_test.models import (
    DEFAULT_ALPHA,
    DEFAULT_BLOCK_SIZE,
    DEFAULT_HORIZON,
    DEFAULT_PERMUTATIONS,
    MIN_CROSS_SECTION,
    QUANTILE_COUNT,
    FactorTestConfig,
)
from selector_app.formulas.custom import FormulaParseError, custom_output_id, parse_formula
from selector_app.formulas.registry import FORMULA_REGISTRY
from selector_app.market_data.adapter import parse_universe_code
from selector_app.market_data.models import InstrumentBoard, InstrumentType
from selector_app.market_data.scope import InstrumentScope
from selector_app.portfolio_backtest.models import (
    PortfolioBacktestConfig,
    PortfolioUniverse,
)
from selector_app.screening.models import ScanConfig, ScreenCondition
from selector_app.screening.rules import RuleGroup
from selector_app.strategy_discovery.catalog import default_indicator_catalog
from selector_app.strategy_discovery.models import DiscoveryRequest
from selector_app.strategy_fitness.models import StrategyFitnessConfig
from selector_app.swing_research.labels import ALL_LABELS as ALL_SWING_LABELS
from selector_app.swing_research.models import SwingIndicatorConfig, SwingOverviewConfig

from .date_ranges import (
    DateRangeMode,
    coerce_date,
    resolve_date_range,
    validate_date_range_request,
)


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


StrategyDiscoveryDateMode = DateRangeMode
DEFAULT_STRATEGY_DISCOVERY_LOOKBACK_YEARS = 5


def resolve_strategy_discovery_defaults(
    data_start: date | str | None,
    data_end: date | str | None,
    mode: StrategyDiscoveryDateMode = "recent5",
) -> tuple[date | None, date | None]:
    """Resolve a preset range against the latest completed local bar."""
    return resolve_date_range(
        mode=mode,
        requested_start=None,
        requested_end=None,
        available_start=data_start,
        available_end=data_end,
    )


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


class BarDeleteRequest(BaseModel):
    """Undo a bad import by dropping stored bars.

    ``source`` is required on purpose: local rows outrank online rows forever,
    so dropping them is the only way back from a wrong import — but a request
    with no filter at all would wipe the whole table, and that must never be
    one missing field away.
    """

    model_config = ConfigDict(extra="forbid")

    source: Literal["local", "online"]
    universe: Literal["all", "sh", "sz"] = "all"
    codes: list[str] | None = Field(default=None, max_length=5_000)
    start_date: date | None = None
    end_date: date | None = None
    confirm: bool = False

    @model_validator(mode="after")
    def validate_range(self) -> BarDeleteRequest:
        if not self.confirm:
            raise ValueError("删除日线数据不可撤销，请传 confirm=true 明确确认")
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("开始日期不能晚于结束日期")
        return self

    @property
    def markets(self) -> tuple[str, ...]:
        if self.universe == "all":
            return ("SH", "SZ")
        return (self.universe.upper(),)


class MarketSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vipdoc_path: str | None = Field(default=None, max_length=1024)
    universe: Literal["all", "sh", "sz"] = "all"
    # The legacy TDX provider was removed; the field stays for API compatibility
    # and only accepts the default so old callers sending it do not 422.
    provider: Literal["eastmoney"] = "eastmoney"
    bars: int = Field(default=800, ge=1, le=800)
    instrument_types: list[InstrumentType] | None = Field(default=None, max_length=1)
    boards: list[InstrumentBoard] | None = Field(default=None, max_length=7)
    full_history: bool = False
    # 指标预计算按自然年分批执行，这里决定往前算几年（None = 用服务端默认值）。
    precompute_years: int | None = Field(default=None, ge=1, le=30)
    # 只补算缓存缺失或已过期的标的，而不是整个 universe 重算一遍。
    precompute_only_changed: bool = False

    @model_validator(mode="after")
    def validate_scope(self) -> MarketSyncRequest:
        validate_instrument_scope(self.universe, self.instrument_types, self.boards)
        if not isinstance(self.full_history, bool):
            raise ValueError("是否全量同步历史行情必须是布尔值")
        if not isinstance(self.precompute_only_changed, bool):
            raise ValueError("是否只补算变化标的必须是布尔值")
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


def _rankable_research_values(formula_text: str | None) -> frozenset[str]:
    """Numeric outputs that may be used as a cross-stock ranking key.

    Mirrors ``ConditionResearchService._feature_ids``: a custom formula decides
    its own outputs (minus raw price/volume levels), otherwise the shared
    catalog decides which built-in values are comparable across stocks.
    """

    if formula_text:
        parsed = parse_formula(formula_text)
        return frozenset(
            custom_output_id(name)
            for name in parsed.value_names
            if name not in parsed.price_scale_names
        )
    return frozenset(default_indicator_catalog().rankable_ids)


class ConditionResearchRequest(BaseModel):
    """Cross-stock independent backtests with rule discovery."""

    model_config = ConfigDict(extra="forbid")

    universe: Literal["all", "sh", "sz"] = "all"
    vipdoc_path: str = Field(default="", max_length=1024)
    instrument_types: list[InstrumentType] | None = Field(default=None, max_length=1)
    boards: list[InstrumentBoard] = Field(default_factory=list, max_length=4)
    # 留空表示「不设基准买入条件，持续满仓」，由研究直接搜索买入时机。
    buy_rule: RuleGroupRequest | None = None
    # 留空表示「不设基准卖出条件，持有到期末」，由研究直接搜索卖出时机。
    sell_rule: RuleGroupRequest | None = None
    formula_text: str | None = Field(default=None, max_length=20_000)
    formula_parameters: dict[str, float] = Field(default_factory=dict, max_length=32)
    date_mode: DateRangeMode = "recent5"
    start_date: date | None = None
    end_date: date | None = None
    search_sides: Literal["buy", "sell", "both"] = "both"
    initial_cash: float = Field(default=100_000.0, gt=0, le=1_000_000_000_000)
    commission: float = Field(default=0.0001, ge=0, le=0.1)
    min_commission: float = Field(default=0.1, ge=0, le=100_000)
    stamp_tax: float = Field(default=0.001, ge=0, le=0.1)
    slippage: float = Field(default=0.0, ge=0, le=1_000)
    execution: Literal["next_open", "next_close"] = "next_open"
    position_mode: Literal["full", "fixed"] = "full"
    fixed_size: int | None = Field(default=None, ge=100, le=100_000_000)
    min_trades: int = Field(default=20, ge=1, le=100_000)
    min_instruments: int = Field(default=5, ge=1, le=10_000)
    candidate_limit: int = Field(default=5, ge=1, le=10)
    # 同义条件合并门槛：命中向量相关性达到它就只留一条，其余列在被保留那条下面。
    # 1.0 ≈ 关闭。
    duplicate_correlation: float = Field(default=DEFAULT_DUPLICATE_CORRELATION, ge=0.5, le=1.0)
    holdout_candidates: int = Field(default=1, ge=1, le=3)
    # 滚动复验：把留出段再切成几段，逐个回放同一批候选。1 = 不拆（现状）。
    walk_forward_windows: int = Field(default=1, ge=1, le=5)
    # 置换检验：0 = 不检验，越大门槛越稳但越慢（单次约等价于挖掘耗时的 1/13，
    # 但零分布池是所有可用掩码而不仅是入围候选，实测按 1/6 估更稳妥）。
    # 默认 200：20 次时名义 5% 的门槛实测误报 11.7%。
    significance_permutations: int = Field(default=200, ge=0, le=500)
    significance_quantile: float = Field(default=0.95, gt=0, le=1)
    # 排序规则：研究不建组合，但结论要落到组合回测的「候选池 + 有限槽位」模型上，
    # 带上它才能顺带给「该按什么排序」和「这个条件在 N 槽位下还有没有意义」的答案。
    ranking_value: str | None = Field(default=None, max_length=128)
    rank_order: Literal["asc", "desc"] = "desc"
    ranking_top_n: int = Field(default=5, ge=1, le=50)

    @model_validator(mode="after")
    def validate_research(self) -> ConditionResearchRequest:
        validate_date_range_request(self.date_mode, self.start_date, self.end_date)
        if self.instrument_types is not None:
            if any(item != "stock" for item in self.instrument_types):
                raise ValueError("买卖条件研究只支持股票，ETF、指数与债券不参与候选池")
        if any(board not in {"main", "star", "chinext"} for board in self.boards):
            raise ValueError("买卖条件研究的范围只支持股票板块")
        if self.start_date is not None and self.end_date is not None:
            if self.start_date > self.end_date:
                raise ValueError("研究开始日期不能晚于结束日期")
        if self.buy_rule is None and self.sell_rule is None:
            raise ValueError("买入和卖出条件不能同时留空，至少要保留一侧作为研究基准")
        signal_ids = [
            *(self.buy_rule.signals if self.buy_rule is not None else []),
            *(self.sell_rule.signals if self.sell_rule is not None else []),
        ]
        validate_formula_signals(
            self.formula_text,
            signal_ids,
            self.formula_parameters,
        )
        if self.formula_text and not parse_formula(self.formula_text).value_names:
            raise ValueError("自定义公式至少需要包含一个数值输出，才能自动挖掘条件")
        if self.ranking_value is not None and self.ranking_value not in _rankable_research_values(
            self.formula_text
        ):
            raise ValueError("排序指标必须是可跨股票比较的数值输出（例如涨跌幅、RSI，不能是价格）")
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
        if self.position_mode == "fixed":
            if self.fixed_size is None or self.fixed_size % 100 != 0:
                raise ValueError("固定股数必须是 100 的整数倍")
        elif self.fixed_size is not None:
            raise ValueError("全仓模式不应设置固定股数")
        return self

    def to_config(
        self,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> ConditionResearchConfig:
        resolved_start = self.start_date or start_date
        resolved_end = self.end_date or end_date
        if resolved_start is None or resolved_end is None:
            raise ValueError("本地行情没有可用日期范围")
        return ConditionResearchConfig(
            vipdoc_path=self.vipdoc_path,
            universe=self.universe,
            buy_rule=self.buy_rule.to_model() if self.buy_rule is not None else None,
            sell_rule=self.sell_rule.to_model() if self.sell_rule is not None else None,
            start_date=resolved_start,
            end_date=resolved_end,
            formula_text=self.formula_text,
            formula_parameters=self.formula_parameters,
            instrument_types=tuple(self.instrument_types or ("stock",)),
            boards=tuple(self.boards),
            search_sides=self.search_sides,
            initial_cash=self.initial_cash,
            commission=self.commission,
            min_commission=self.min_commission,
            stamp_tax=self.stamp_tax,
            slippage=self.slippage,
            execution=self.execution,
            position_mode=self.position_mode,
            fixed_size=self.fixed_size,
            min_trades=self.min_trades,
            min_instruments=self.min_instruments,
            candidate_limit=self.candidate_limit,
            duplicate_correlation=self.duplicate_correlation,
            holdout_candidates=self.holdout_candidates,
            walk_forward_windows=self.walk_forward_windows,
            significance_permutations=self.significance_permutations,
            significance_quantile=self.significance_quantile,
            ranking_value=self.ranking_value,
            rank_order=self.rank_order,
            ranking_top_n=self.ranking_top_n,
        )


class SwingOverviewRequest(BaseModel):
    """Swing segmentation overview - the calibration step before a study.

    Every field that decides what counts as a leg is exposed, because the
    first run exists to answer "what should these be?" rather than to produce
    a finding. ``reversal_multiples`` takes several thresholds at once so the
    trade-off between how many legs appear and how far they travel is visible
    in a single pass.
    """

    model_config = ConfigDict(extra="forbid")

    vipdoc_path: str = Field(default="", max_length=1024)
    universe: Literal["all", "sh", "sz"] = "all"
    instrument_types: list[InstrumentType] | None = Field(default=None, max_length=1)
    boards: list[InstrumentBoard] | None = Field(default=None, max_length=7)
    start_date: date | None = None
    end_date: date | None = None
    reversal_multiples: list[float] = Field(
        default_factory=lambda: [2.0, 3.0, 4.0],
        max_length=8,
    )
    min_bars: int = Field(default=5, ge=0, le=1_000)
    atr_window: int = Field(default=14, ge=1, le=1_000)
    include_pending: bool = False
    limit_stocks: int | None = Field(default=None, ge=1, le=100_000)

    def to_config(self) -> SwingOverviewConfig:
        return SwingOverviewConfig(
            universe=self.universe,
            instrument_types=tuple(self.instrument_types or ("stock",)),
            boards=tuple(self.boards or ()),
            vipdoc_path=self.vipdoc_path,
            start_date=self.start_date,
            end_date=self.end_date,
            reversal_multiples=tuple(self.reversal_multiples),
            min_bars=self.min_bars,
            atr_window=self.atr_window,
            include_pending=self.include_pending,
            limit_stocks=self.limit_stocks,
        )


SwingLabel = Literal[
    "up_leg_start",
    "down_leg_start",
    "up_onset_ahead",
    "down_onset_ahead",
]


class SwingIndicatorRequest(BaseModel):
    """Score technical indicators against swing-derived labels.

    This is the study proper, so unlike the overview it fixes a *single*
    threshold: sweeping several here would multiply the very multiplicity the
    significance test is trying to control.

    ``labels`` picks the question. ``*_leg_start`` attributes big moves to the
    state of the bar they began from; ``*_onset_ahead`` asks whether a move
    starts running soon, which is the tradable version.
    """

    model_config = ConfigDict(extra="forbid")

    vipdoc_path: str = Field(default="", max_length=1024)
    universe: Literal["all", "sh", "sz"] = "all"
    instrument_types: list[InstrumentType] | None = Field(default=None, max_length=1)
    boards: list[InstrumentBoard] | None = Field(default=None, max_length=7)
    start_date: date | None = None
    end_date: date | None = None
    reversal_multiple: float = Field(default=3.0, gt=0, le=20)
    min_bars: int = Field(default=5, ge=0, le=1_000)
    atr_window: int = Field(default=14, ge=1, le=1_000)
    horizon: int = Field(default=10, ge=1, le=250)
    labels: list[SwingLabel] | None = Field(default=None, max_length=4)
    value_ids: list[str] | None = Field(default=None, max_length=64)
    #: Study the continuous outputs of 主力清洗 / 庄家顶底 / 准备拉升 alongside
    #: the technical family.
    include_custom: bool = True
    min_cross_section: int = Field(default=20, ge=2, le=10_000)
    permutations: int = Field(default=200, ge=0, le=5_000)
    block_size: int = Field(default=20, ge=1, le=500)
    alpha: float = Field(default=0.05, gt=0, lt=1)
    limit_stocks: int | None = Field(default=None, ge=1, le=100_000)
    #: 存档用，不参与计算。研究分两步跑，第一步（切分校准）的表单与结果
    #: 只存在于浏览器里；不带过来，历史记录就只能还原第二步，加载后第一步
    #: 的表格会是空的。后端原样存进 request_json，不做校验。
    overview_request: dict[str, object] | None = None
    overview_result: dict[str, object] | None = None

    def to_config(self) -> SwingIndicatorConfig:
        return SwingIndicatorConfig(
            universe=self.universe,
            instrument_types=tuple(self.instrument_types or ("stock",)),
            boards=tuple(self.boards or ()),
            vipdoc_path=self.vipdoc_path,
            start_date=self.start_date,
            end_date=self.end_date,
            reversal_multiple=self.reversal_multiple,
            min_bars=self.min_bars,
            atr_window=self.atr_window,
            horizon=self.horizon,
            labels=tuple(self.labels) if self.labels else ALL_SWING_LABELS,
            value_ids=tuple(self.value_ids or ()),
            include_custom=self.include_custom,
            min_cross_section=self.min_cross_section,
            permutations=self.permutations,
            block_size=self.block_size,
            alpha=self.alpha,
            limit_stocks=self.limit_stocks,
        )


class FactorTestRequest(BaseModel):
    """Cross-sectional factor test against forward returns.

    ``horizon`` is the whole label: how many trading days ahead the return is
    measured over. It is exposed rather than fixed because a value can be a
    month-scale factor and a week-scale none at all, and there is no way to
    tell from the indicator's name which one it is.
    """

    model_config = ConfigDict(extra="forbid")

    vipdoc_path: str = Field(default="", max_length=1024)
    universe: Literal["all", "sh", "sz"] = "all"
    instrument_types: list[InstrumentType] | None = Field(default=None, max_length=1)
    boards: list[InstrumentBoard] | None = Field(default=None, max_length=7)
    start_date: date | None = None
    end_date: date | None = None
    horizon: int = Field(default=DEFAULT_HORIZON, ge=1, le=250)
    value_ids: list[str] | None = Field(default=None, max_length=64)
    include_custom: bool = True
    min_cross_section: int = Field(default=MIN_CROSS_SECTION, ge=2, le=10_000)
    permutations: int = Field(default=DEFAULT_PERMUTATIONS, ge=0, le=5_000)
    block_size: int = Field(default=DEFAULT_BLOCK_SIZE, ge=1, le=500)
    alpha: float = Field(default=DEFAULT_ALPHA, gt=0, lt=1)
    quantile_count: int = Field(default=QUANTILE_COUNT, ge=2, le=10)
    limit_stocks: int | None = Field(default=None, ge=1, le=100_000)

    def to_config(self) -> FactorTestConfig:
        return FactorTestConfig(
            universe=self.universe,
            instrument_types=tuple(self.instrument_types or ("stock",)),
            boards=tuple(self.boards or ()),
            vipdoc_path=self.vipdoc_path,
            start_date=self.start_date,
            end_date=self.end_date,
            horizon=self.horizon,
            value_ids=tuple(self.value_ids or ()),
            include_custom=self.include_custom,
            min_cross_section=self.min_cross_section,
            permutations=self.permutations,
            block_size=self.block_size,
            alpha=self.alpha,
            quantile_count=self.quantile_count,
            limit_stocks=self.limit_stocks,
        )


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
        )


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
        from selector_app.strategy_discovery.catalog import default_indicator_catalog

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


class HistoryCompareRequest(BaseModel):
    """Two to five archived runs to lay out side by side.

    上限不是随手定的：五条以上曲线叠在一张图上，颜色都分不出来，读图的人只会
    记住最上面那条。归一化默认除以本金——本金不同的两次实验，收益率口径才是
    可比的（取不到本金时服务端会退回按首值归一，并在结果里写明）。
    """

    model_config = ConfigDict(extra="forbid")

    history_ids: list[str] = Field(min_length=2, max_length=5)
    normalize: Literal["capital", "first"] = "capital"
    x_axis: Literal["date", "index"] = "date"

    @model_validator(mode="after")
    def validate_ids(self) -> HistoryCompareRequest:
        cleaned = [item.strip() for item in self.history_ids if item.strip()]
        if len(cleaned) < 2:
            raise ValueError("至少需要选择 2 条历史记录")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("同一条历史记录不能重复选择")
        self.history_ids = cleaned
        return self
