"""Immutable configuration and JSON-ready reports for portfolio rotation."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from selector_app.market_data.models import InstrumentBoard, InstrumentType
from selector_app.market_data.scope import InstrumentScope
from selector_app.screening.models import ScreenCondition
from selector_app.screening.rules import RuleGroup

PortfolioUniverse = Literal["all", "sh", "sz", "custom", "codes"]
SINGLE_SECURITY_RANKING_VALUE = "__single_security__"
RankOrder = Literal["asc", "desc"]
RebalanceFrequency = Literal["daily", "weekly", "monthly"]
ExecutionMode = Literal["next_open", "next_close"]
SellValueOperator = Literal["gte", "lte"]
CompareOperator = Literal["gt", "gte", "lt", "lte"]
PositionMode = Literal["full", "fixed"]
PortfolioJsonValue = object
PORTFOLIO_BACKTEST_PRICE_BASIS: Literal["raw"] = "raw"
PORTFOLIO_BACKTEST_LIMITATIONS = (
    "行情使用未复权原始日线；除权除息未校准，现金分红未计入收益。",
    "仅模拟单一做多组合和固定持仓槽位，不模拟实盘下单、融资融券或多策略资金隔离。",
    "成交按日线价格及配置的费用、滑点模拟；不模拟涨跌停撮合、停牌成交限制或成交量冲击。",
    "这是历史模拟，不代表未来收益，也不是投资建议。",
)


@dataclass(frozen=True)
class PortfolioBacktestConfig:
    """Configuration for a daily, slot-based long-only portfolio backtest."""

    vipdoc_path: str | Path
    universe: PortfolioUniverse
    selected_signals: tuple[str, ...]
    combine_mode: str
    minimum_matches: int | None
    ranking_value: str
    rank_order: RankOrder = "desc"
    max_positions: int = 5
    rebalance_frequency: RebalanceFrequency = "daily"
    execution: ExecutionMode = "next_open"
    universe_file: str | Path | None = None
    instrument_types: tuple[InstrumentType, ...] | None = None
    boards: tuple[InstrumentBoard, ...] | None = None
    formula_text: str | None = None
    formula_parameters: Mapping[str, float] = field(default_factory=dict)
    sell_signal: str | None = None
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    take_profit_mode: Literal["pct", "atr"] = "pct"
    take_profit_atr_multiplier: float | None = None
    take_profit_cooldown_days: int = 0
    stop_loss_mode: Literal["pct", "atr"] = "pct"
    stop_loss_atr_multiplier: float | None = None
    stop_loss_cooldown_days: int = 0
    trailing_stop_pct: float | None = None
    trailing_stop_mode: Literal["pct", "atr"] = "pct"
    trailing_stop_atr_multiplier: float | None = None
    trailing_stop_cooldown_days: int = 0
    #: 最长持有**交易日**数：满 N 个交易日后按 next_open 卖出，与规则卖出同一出口。
    #: None 表示不限制。止盈截断的是涨幅，这个截断的是时间 —— 它对盈利和亏损的
    #: 持仓一视同仁，所以能同时把套牢票赶出槽位，这是单向止损做不到的。
    #: 单位是交易日（与 cooldown_days 同口径）；报告里的 avg_holding_days 是自然日
    #: 口径，两者数值不可直接比较。
    max_hold_days: int | None = None
    atr_period: int = 14
    sell_value: str | None = None
    sell_value_operator: SellValueOperator | None = None
    sell_value_threshold: float | None = None
    compare_left_value: str | None = None
    compare_operator: CompareOperator | None = None
    compare_right_value: str | None = None
    buy_rule: RuleGroup | None = None
    sell_rule: RuleGroup | None = None
    start_date: date | None = None
    end_date: date | None = None
    initial_cash: float = 1_000_000.0
    commission: float = 0.0001
    min_commission: float = 0.1
    stamp_tax: float = 0.001
    slippage: float = 0.0
    fitness_rank_weight: float = 0.0
    fitness_min_trades: int = 5
    fitness_max_drawdown: float = 0.3
    codes: tuple[str, ...] | None = None
    single_security_mode: bool = False
    symbol_strategies: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    symbol_priority_scores: Mapping[str, float] = field(default_factory=dict)
    position_mode: PositionMode = "full"
    fixed_size: int | None = None
    #: 打开后把 ST / *ST / 退市标的挡在候选池外。判定只能按**当前名称**做
    #: （行情库没有历史名称快照），对回测来说偏严格：会把"当时还没戴帽、
    #: 后来才变 ST"的股票一起排除。默认关闭，保持历史结果可比。
    exclude_st: bool = False

    def __post_init__(self) -> None:
        if self.universe not in {"all", "sh", "sz", "custom", "codes"}:
            raise ValueError("不支持的组合回测范围")
        if self.universe == "codes" and not self.codes:
            raise ValueError("指定标的范围必须提供至少一个代码")
        if self.universe != "codes" and self.codes:
            raise ValueError("只有指定标的范围才需要填写代码列表")
        if self.position_mode not in {"full", "fixed"}:
            raise ValueError("不支持的建仓方式")
        if self.position_mode == "fixed" and (self.fixed_size is None or self.fixed_size <= 0):
            raise ValueError("固定股数模式必须设置大于 0 的买入股数")
        if not isinstance(self.exclude_st, bool):
            raise ValueError("排除 ST/退市标的开关必须是布尔值")
        if self.max_hold_days is not None and (
            isinstance(self.max_hold_days, bool)
            or not isinstance(self.max_hold_days, int)
            or self.max_hold_days < 1
        ):
            raise ValueError("最长持有天数必须是大于 0 的整数")
        scope = InstrumentScope.from_values(
            universe=self.universe,
            instrument_types=self.instrument_types,
            boards=self.boards,
        )
        object.__setattr__(self, "instrument_types", scope.instrument_types)
        object.__setattr__(self, "boards", scope.boards)
        object.__setattr__(
            self,
            "symbol_strategies",
            MappingProxyType(
                {
                    str(key): MappingProxyType(dict(value))
                    for key, value in self.symbol_strategies.items()
                }
            ),
        )
        object.__setattr__(
            self, "symbol_priority_scores", MappingProxyType(dict(self.symbol_priority_scores))
        )
        explicit_buy_rule = self.buy_rule is not None
        explicit_sell_rule = self.sell_rule is not None
        if not explicit_buy_rule and not self.selected_signals and not self.symbol_strategies:
            raise ValueError("至少需要一个选股条件")
        if len(set(self.selected_signals)) != len(self.selected_signals):
            raise ValueError("选股条件不能重复")
        if self.combine_mode not in {"all", "any", "at_least"}:
            raise ValueError("不支持的条件组合方式")
        if not explicit_buy_rule and self.combine_mode == "at_least":
            if self.minimum_matches is None or not 1 <= self.minimum_matches <= len(
                self.selected_signals
            ):
                raise ValueError("minimum_matches 必须在选股条件数量范围内")
        elif not explicit_buy_rule and self.minimum_matches is not None:
            raise ValueError("all/any 模式不应设置 minimum_matches")
        if self.single_security_mode:
            if self.universe != "codes" or self.codes is None or len(self.codes) != 1:
                raise ValueError("单股模式只能指定一只股票")
            if self.ranking_value.strip() not in {"", SINGLE_SECURITY_RANKING_VALUE}:
                raise ValueError("单股模式不使用排序指标")
            object.__setattr__(self, "ranking_value", SINGLE_SECURITY_RANKING_VALUE)
            object.__setattr__(self, "max_positions", 1)
        elif not self.ranking_value.strip():
            raise ValueError("排序指标不能为空")
        if not 1 <= self.max_positions <= 100:
            raise ValueError("持仓槽位数量必须在 1 到 100 之间")
        if self.rebalance_frequency not in {"daily", "weekly", "monthly"}:
            raise ValueError("不支持的候选刷新频率")
        if self.execution not in {"next_open", "next_close"}:
            raise ValueError("不支持的成交方式")
        if not math.isfinite(self.fitness_rank_weight) or not 0 <= self.fitness_rank_weight <= 1:
            raise ValueError("适配性排序权重必须在 0 到 1 之间")
        if isinstance(self.fitness_min_trades, bool) or not 1 <= self.fitness_min_trades <= 10_000:
            raise ValueError("适配性最少成交笔数必须在 1 到 10000 之间")
        if not math.isfinite(self.fitness_max_drawdown) or not 0 <= self.fitness_max_drawdown <= 1:
            raise ValueError("适配性最大回撤阈值必须在 0 到 1 之间")
        if self.sell_value_operator not in {None, "gte", "lte"}:
            raise ValueError("不支持的指标阈值比较方式")
        if self.compare_operator not in {None, "gt", "gte", "lt", "lte"}:
            raise ValueError("不支持的指标比较方式")
        if self.start_date is not None and self.end_date is not None:
            if self.start_date > self.end_date:
                raise ValueError("回测开始日期不能晚于结束日期")
        _validate_non_negative("initial_cash", self.initial_cash, strictly_positive=True)
        for name, value in {
            "commission": self.commission,
            "min_commission": self.min_commission,
            "stamp_tax": self.stamp_tax,
            "slippage": self.slippage,
        }.items():
            _validate_non_negative(name, value)
        for name, optional_value in {
            "stop_loss_pct": self.stop_loss_pct,
            "take_profit_pct": self.take_profit_pct,
            "trailing_stop_pct": self.trailing_stop_pct,
        }.items():
            if optional_value is not None and (
                not math.isfinite(optional_value) or optional_value <= 0
            ):
                raise ValueError(f"{name} 必须是大于 0 的有限数字")
        for prefix in ("take_profit", "stop_loss", "trailing_stop"):
            mode = getattr(self, f"{prefix}_mode")
            if mode not in {"pct", "atr"}:
                raise ValueError(f"{prefix}_mode 必须是 pct 或 atr")
            multiplier = getattr(self, f"{prefix}_atr_multiplier")
            if multiplier is not None and (not math.isfinite(multiplier) or multiplier <= 0):
                raise ValueError(f"{prefix}_atr_multiplier 必须是大于 0 的有限数字")
            cooldown = getattr(self, f"{prefix}_cooldown_days")
            if isinstance(cooldown, bool) or not 0 <= cooldown <= 10_000:
                raise ValueError(f"{prefix}_cooldown_days 必须在 0 到 10000 之间")
            percentage = getattr(self, f"{prefix}_pct")
            if mode == "atr" and multiplier is None:
                raise ValueError(f"{prefix}_atr_multiplier 在 ATR 模式下不能为空")
            if mode == "atr" and percentage is not None:
                raise ValueError(f"{prefix}_pct 不能在 ATR 模式下设置")
            if mode == "pct" and multiplier is not None:
                raise ValueError(f"{prefix}_atr_multiplier 不能在 pct 模式下设置")
        if self.atr_period != 14:
            raise ValueError("atr_period 固定为 14")
        if self.sell_value_threshold is not None and not math.isfinite(self.sell_value_threshold):
            raise ValueError("sell_value_threshold 必须是有限数字")
        if self.sell_value is None:
            if self.sell_value_operator is not None or self.sell_value_threshold is not None:
                raise ValueError("指标阈值卖出规则缺少指标")
        elif self.sell_value_operator is None or self.sell_value_threshold is None:
            raise ValueError("指标阈值卖出规则需要指标、比较方式和阈值")
        comparison = (
            self.compare_left_value,
            self.compare_operator,
            self.compare_right_value,
        )
        if any(value is not None for value in comparison) and not all(
            value is not None for value in comparison
        ):
            raise ValueError("指标比较卖出规则需要左右指标和比较方式")
        if not explicit_buy_rule and not self.symbol_strategies:
            object.__setattr__(
                self,
                "buy_rule",
                RuleGroup(
                    signals=tuple(self.selected_signals),
                    combine_mode=self.combine_mode,
                    minimum_matches=self.minimum_matches,
                ),
            )
        if not explicit_sell_rule:
            sell_conditions: list[ScreenCondition] = []
            if self.sell_value is not None:
                sell_conditions.append(
                    ScreenCondition(
                        left_value=self.sell_value,
                        operator=self.sell_value_operator or "gte",
                        right_constant=self.sell_value_threshold,
                    )
                )
            if self.compare_left_value is not None:
                sell_conditions.append(
                    ScreenCondition(
                        left_value=self.compare_left_value,
                        operator=self.compare_operator or "gt",
                        right_value=self.compare_right_value,
                    )
                )
            sell_signals = (self.sell_signal,) if self.sell_signal is not None else ()
            if sell_signals or sell_conditions:
                object.__setattr__(
                    self,
                    "sell_rule",
                    RuleGroup(
                        signals=sell_signals,
                        conditions=tuple(sell_conditions),
                        combine_mode="any",
                    ),
                )
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
        has_symbol_exits = any(
            isinstance(strategy.get("sell_rule"), RuleGroup)
            or any(
                strategy.get(key) is not None
                for key in (
                    "stop_loss_pct",
                    "stop_loss_atr_multiplier",
                    "take_profit_pct",
                    "take_profit_atr_multiplier",
                    "trailing_stop_pct",
                    "trailing_stop_atr_multiplier",
                )
            )
            for strategy in self.symbol_strategies.values()
        )
        # ``max_hold_days`` 同样是出口：满 N 个交易日必卖，所以只设它也合法。
        if (
            self.sell_rule is None
            and not risk_enabled
            and not has_symbol_exits
            and self.max_hold_days is None
        ):
            raise ValueError("至少需要设置一个卖出规则")
        object.__setattr__(self, "ranking_value", self.ranking_value.strip())
        object.__setattr__(
            self, "formula_text", self.formula_text.strip() if self.formula_text else None
        )
        object.__setattr__(
            self, "formula_parameters", MappingProxyType(dict(self.formula_parameters))
        )


def _validate_non_negative(name: str, value: float, *, strictly_positive: bool = False) -> None:
    if not math.isfinite(value) or (value <= 0 if strictly_positive else value < 0):
        operator = "大于 0" if strictly_positive else "大于等于 0"
        raise ValueError(f"{name} 必须是{operator}的有限数字")


@dataclass(frozen=True)
class PortfolioBacktestReport:
    """JSON-ready dynamic portfolio result."""

    universe: str
    total_candidates: int
    processed: int
    skipped: int
    errors: int
    bars: int
    start_date: str
    end_date: str
    max_positions: int
    ranking_value: str | None
    rank_order: str | None
    fitness_rank_weight: float
    fitness_min_trades: int
    fitness_max_drawdown: float
    performance: Mapping[str, float | None]
    equity_curve: tuple[Mapping[str, PortfolioJsonValue], ...]
    trades: tuple[Mapping[str, PortfolioJsonValue], ...]
    states: tuple[Mapping[str, PortfolioJsonValue], ...]
    ranking_events: tuple[Mapping[str, PortfolioJsonValue], ...]
    failure_reasons: Mapping[str, int]
    diagnostic: str | None = None
    data_fingerprint: str = ""
    risk_controls: Mapping[str, object] = field(default_factory=dict)
    #: 指定标的中行情库里根本找不到的代码。以前只在"一个都没找到"时报错，
    #: 少几个就被悄悄丢掉，回测看上去跑通了，实际样本少了一块。
    missing_codes: tuple[str, ...] = ()
    #: 指定标的中被板块/类型过滤掉的代码。
    filtered_codes: tuple[str, ...] = ()
    #: 打开了 exclude_st 时被当作 ST / *ST / 退市剔除掉的标的数。全市场回测里
    #: 逐只列名字会让报告膨胀，所以只给数量——用户点名的标的（codes 模式）
    #: 会同时出现在 filtered_codes 里，方便看出"它为什么没被买"。
    excluded_st_codes: tuple[str, ...] = ()
    excluded_st_count: int = 0
    #: 基准对照（标的池等权买入持有）。算不出来时是 None——比如窗口只有一天，
    #: 或者池里没有一只标的在窗口首日就有价格。没有基准时报告照常出，只是
    #: "跑赢了没有"这件事没人回答。
    benchmark: Mapping[str, object] | None = None
    #: 下一交易日待执行计划（最后一棒收盘时排定的 pending 单 + 当前持仓）。
    #: 数量一律按最后一根 K 线的收盘价**估算**——明日开盘价今天不存在。没有
    #: 追踪需求时是空字典，报告其余部分照常。
    next_plan: Mapping[str, PortfolioJsonValue] = field(default_factory=dict)
    #: 最近行情日的候选指标扫描，与模拟执行产生的 next_plan 严格分离。
    today_candidates: Mapping[str, PortfolioJsonValue] = field(default_factory=dict)
    #: 最近行情日账户快照，持仓含卖出指标读数。
    current_state: Mapping[str, PortfolioJsonValue] | None = None
    price_basis: Literal["raw"] = PORTFOLIO_BACKTEST_PRICE_BASIS
    limitations: tuple[str, ...] = PORTFOLIO_BACKTEST_LIMITATIONS
    single_security_mode: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "performance", MappingProxyType(dict(self.performance)))
        object.__setattr__(self, "failure_reasons", MappingProxyType(dict(self.failure_reasons)))
        object.__setattr__(self, "risk_controls", MappingProxyType(dict(self.risk_controls)))
        object.__setattr__(self, "limitations", tuple(self.limitations))

    def to_dict(self) -> dict[str, object]:
        return {
            "universe": self.universe,
            "total_candidates": self.total_candidates,
            "processed": self.processed,
            "skipped": self.skipped,
            "errors": self.errors,
            "bars": self.bars,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "max_positions": self.max_positions,
            "ranking_value": None if self.single_security_mode else self.ranking_value,
            "rank_order": None if self.single_security_mode else self.rank_order,
            **({"single_security_mode": True} if self.single_security_mode else {}),
            "fitness_rank_weight": self.fitness_rank_weight,
            "fitness_min_trades": self.fitness_min_trades,
            "fitness_max_drawdown": self.fitness_max_drawdown,
            "performance": dict(self.performance),
            "equity_curve": [dict(row) for row in self.equity_curve],
            "trades": [dict(row) for row in self.trades],
            "states": [dict(row) for row in self.states],
            "ranking_events": []
            if self.single_security_mode
            else [dict(row) for row in self.ranking_events],
            "failure_reasons": dict(self.failure_reasons),
            "diagnostic": self.diagnostic,
            "data_fingerprint": self.data_fingerprint,
            "risk_controls": dict(self.risk_controls),
            "missing_codes": list(self.missing_codes),
            "filtered_codes": list(self.filtered_codes),
            "excluded_st_codes": list(self.excluded_st_codes),
            "excluded_st_count": self.excluded_st_count,
            "benchmark": dict(self.benchmark) if self.benchmark is not None else None,
            "next_plan": dict(self.next_plan),
            "today_candidates": (
                {
                    "as_of": self.today_candidates.get("as_of"),
                    "total": 0,
                    "rows": [],
                }
                if self.single_security_mode
                else dict(self.today_candidates)
            ),
            "current_state": dict(self.current_state) if self.current_state is not None else None,
            "price_basis": self.price_basis,
            "limitations": list(self.limitations),
        }
