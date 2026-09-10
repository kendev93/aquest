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

PortfolioUniverse = Literal["all", "sh", "sz", "custom"]
RankOrder = Literal["asc", "desc"]
RebalanceFrequency = Literal["daily", "weekly", "monthly"]
ExecutionMode = Literal["next_open", "next_close"]
SellValueOperator = Literal["gte", "lte"]
CompareOperator = Literal["gt", "gte", "lt", "lte"]
PortfolioJsonValue = object


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
    commission: float = 0.0003
    min_commission: float = 5.0
    stamp_tax: float = 0.001
    slippage: float = 0.0
    fitness_filter_enabled: bool = False
    fitness_min_score: float = 75.0
    fitness_min_trades: int = 5
    fitness_max_drawdown: float = 0.3

    def __post_init__(self) -> None:
        if self.universe not in {"all", "sh", "sz", "custom"}:
            raise ValueError("不支持的组合回测范围")
        scope = InstrumentScope.from_values(
            universe=self.universe,
            instrument_types=self.instrument_types,
            boards=self.boards,
        )
        object.__setattr__(self, "instrument_types", scope.instrument_types)
        object.__setattr__(self, "boards", scope.boards)
        explicit_buy_rule = self.buy_rule is not None
        explicit_sell_rule = self.sell_rule is not None
        if not explicit_buy_rule and not self.selected_signals:
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
        if not self.ranking_value.strip():
            raise ValueError("排序指标不能为空")
        if not 1 <= self.max_positions <= 100:
            raise ValueError("持仓槽位数量必须在 1 到 100 之间")
        if self.rebalance_frequency not in {"daily", "weekly", "monthly"}:
            raise ValueError("不支持的候选刷新频率")
        if self.execution not in {"next_open", "next_close"}:
            raise ValueError("不支持的成交方式")
        if not isinstance(self.fitness_filter_enabled, bool):
            raise ValueError("适配性过滤开关必须是布尔值")
        if not math.isfinite(self.fitness_min_score) or not 0 <= self.fitness_min_score <= 100:
            raise ValueError("适配性分数阈值必须在 0 到 100 之间")
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
        }.items():
            if optional_value is not None and (
                not math.isfinite(optional_value) or optional_value < 0
            ):
                raise ValueError(f"{name} 必须是大于等于 0 的有限数字")
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
        if not explicit_buy_rule:
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
        if self.sell_rule is None and self.stop_loss_pct is None and self.take_profit_pct is None:
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
    ranking_value: str
    rank_order: str
    fitness_filter_enabled: bool
    fitness_min_score: float
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

    def __post_init__(self) -> None:
        object.__setattr__(self, "performance", MappingProxyType(dict(self.performance)))
        object.__setattr__(self, "failure_reasons", MappingProxyType(dict(self.failure_reasons)))

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
            "ranking_value": self.ranking_value,
            "rank_order": self.rank_order,
            "fitness_filter_enabled": self.fitness_filter_enabled,
            "fitness_min_score": self.fitness_min_score,
            "fitness_min_trades": self.fitness_min_trades,
            "fitness_max_drawdown": self.fitness_max_drawdown,
            "performance": dict(self.performance),
            "equity_curve": [dict(row) for row in self.equity_curve],
            "trades": [dict(row) for row in self.trades],
            "states": [dict(row) for row in self.states],
            "ranking_events": [dict(row) for row in self.ranking_events],
            "failure_reasons": dict(self.failure_reasons),
            "diagnostic": self.diagnostic,
            "data_fingerprint": self.data_fingerprint,
        }
