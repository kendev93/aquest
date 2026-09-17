"""Validated configuration for cross-stock buy/sell condition research."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from types import MappingProxyType
from typing import Literal

from selector_app.market_data.models import InstrumentBoard, InstrumentType
from selector_app.screening.models import Universe
from selector_app.screening.rules import RuleGroup

from .time_split import split_calendar_range

SearchSides = Literal["buy", "sell", "both"]
ExecutionMode = Literal["next_open", "next_close"]
PositionMode = Literal["full", "fixed"]

RESEARCH_INSTRUMENT_TYPES: tuple[InstrumentType, ...] = ("stock", "fund")


def _condition_dict(condition: object) -> dict[str, object]:
    return {
        "left_value": getattr(condition, "left_value"),
        "operator": getattr(condition, "operator"),
        "right_value": getattr(condition, "right_value"),
        "right_constant": getattr(condition, "right_constant"),
    }


def rule_to_dict(rule: RuleGroup) -> dict[str, object]:
    return {
        "signals": list(rule.signals),
        "conditions": [_condition_dict(item) for item in rule.conditions],
        "required_conditions": [_condition_dict(item) for item in rule.required_conditions],
        "combine_mode": rule.combine_mode,
        "minimum_matches": rule.minimum_matches,
    }


@dataclass(frozen=True)
class ConditionResearchConfig:
    vipdoc_path: str
    universe: Universe
    # 留空 = 不设基准买入条件，以「持续满仓」为基准（卖出后下一个交易日立即再买入）。
    # 此时研究挖出的买入候选就是完整买入规则（基础为空，候选 AND 上去等于候选本身）。
    # 与 sell_rule 一样刻意不给默认值，理由见下。
    buy_rule: RuleGroup | None
    # 留空 = 不设基准卖出规则，持仓一直拿到期末按收盘价平仓。此时研究挖出的卖出候选
    # 就是完整卖出规则（基础为空，候选 OR 上去等于候选本身）。
    # 刻意不给默认值：dataclass 里它后面还有无默认的 start_date/end_date，而且
    # 「不设买入/卖出」应当是一次显式的表达，不是漏填。
    sell_rule: RuleGroup | None
    start_date: date
    end_date: date
    formula_text: str | None = None
    formula_parameters: Mapping[str, float] = field(default_factory=dict)
    instrument_types: tuple[InstrumentType, ...] = ("stock",)
    boards: tuple[InstrumentBoard, ...] = ()
    search_sides: SearchSides = "both"
    initial_cash: float = 100_000.0
    commission: float = 0.0001
    min_commission: float = 0.1
    stamp_tax: float = 0.001
    slippage: float = 0.0
    execution: ExecutionMode = "next_open"
    position_mode: PositionMode = "full"
    fixed_size: int | None = None
    train_ratio: float = 0.6
    validation_ratio: float = 0.2
    min_trades: int = 20
    min_instruments: int = 5
    candidate_limit: int = 5
    holdout_candidates: int = 1

    def __post_init__(self) -> None:
        if self.universe not in {"all", "sh", "sz"}:
            raise ValueError("股票范围必须是 all、sh 或 sz")
        if any(item not in RESEARCH_INSTRUMENT_TYPES for item in self.instrument_types):
            raise ValueError("买卖条件研究只支持股票或 ETF")
        if len(set(self.instrument_types)) > 1:
            raise ValueError("买卖条件研究一次只能选择一类证券，避免跨类型样本混池影响阈值可比性")
        if self.start_date > self.end_date:
            raise ValueError("研究开始日期不能晚于结束日期")
        if self.buy_rule is None and self.sell_rule is None:
            raise ValueError("买入和卖出条件不能同时留空，至少要保留一侧作为研究基准")
        if self.buy_rule is not None and not _has_rule(self.buy_rule):
            raise ValueError("基础买入条件为空时请直接留空，表示持续满仓由研究寻找买点")
        if self.sell_rule is not None and not _has_rule(self.sell_rule):
            raise ValueError("基础卖出条件为空时请直接留空，表示持有到期末由研究寻找卖点")
        if self.search_sides not in {"buy", "sell", "both"}:
            raise ValueError("条件搜索范围无效")
        if not 0.0 < self.train_ratio < 1.0:
            raise ValueError("训练比例必须在 0 和 1 之间")
        if not 0.0 < self.validation_ratio < 1.0:
            raise ValueError("验证比例必须在 0 和 1 之间")
        if self.train_ratio + self.validation_ratio >= 1.0:
            raise ValueError("训练和验证比例之和必须小于 1")
        if not 1 <= self.min_trades <= 100_000:
            raise ValueError("最少成交数必须在 1 到 100000 之间")
        if not 1 <= self.min_instruments <= 10_000:
            raise ValueError("最少覆盖股票数必须在 1 到 10000 之间")
        if not 1 <= self.candidate_limit <= 10:
            raise ValueError("候选条件上限必须在 1 到 10 之间")
        if not 1 <= self.holdout_candidates <= 3:
            raise ValueError("留出期复验候选数必须在 1 到 3 之间")
        if not math.isfinite(self.initial_cash) or self.initial_cash <= 0:
            raise ValueError("每只股票的初始资金必须大于 0")
        for name, value in {
            "commission": self.commission,
            "min_commission": self.min_commission,
            "stamp_tax": self.stamp_tax,
            "slippage": self.slippage,
        }.items():
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} 必须是大于等于 0 的有限数字")
        if self.execution not in {"next_open", "next_close"}:
            raise ValueError("不支持的成交方式")
        if self.position_mode not in {"full", "fixed"}:
            raise ValueError("不支持的仓位方式")
        if self.position_mode == "fixed":
            if self.fixed_size is None or self.fixed_size < 100 or self.fixed_size % 100 != 0:
                raise ValueError("固定股数必须是 100 的整数倍")
        elif self.fixed_size is not None:
            raise ValueError("全仓模式不应设置固定股数")
        formula = self.formula_text.strip() if self.formula_text else None
        object.__setattr__(self, "formula_text", formula or None)
        parameters = {str(key): float(value) for key, value in self.formula_parameters.items()}
        if any(not math.isfinite(value) for value in parameters.values()):
            raise ValueError("公式参数必须是有限数字")
        object.__setattr__(
            self,
            "formula_parameters",
            MappingProxyType(parameters),
        )
        object.__setattr__(
            self,
            "instrument_types",
            tuple(dict.fromkeys(self.instrument_types)) or ("stock",),
        )
        boards = tuple(dict.fromkeys(self.boards))
        object.__setattr__(self, "boards", () if "fund" in self.instrument_types else boards)
        split_calendar_range(
            self.start_date,
            self.end_date,
            train_ratio=self.train_ratio,
            validation_ratio=self.validation_ratio,
        )

    def __getstate__(self) -> dict[str, object]:
        """Pickle support: ``MappingProxyType`` cannot cross a spawn boundary."""

        state = dict(self.__dict__)
        state["formula_parameters"] = dict(self.formula_parameters)
        return state

    def __setstate__(self, state: Mapping[str, object]) -> None:
        for key, value in state.items():
            object.__setattr__(self, key, value)
        object.__setattr__(
            self,
            "formula_parameters",
            MappingProxyType(dict(self.formula_parameters)),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "vipdoc_path": self.vipdoc_path,
            "universe": self.universe,
            # None 是两种「让研究自己找」的模式，历史记录要能还原它。
            "buy_rule": rule_to_dict(self.buy_rule) if self.buy_rule is not None else None,
            "sell_rule": rule_to_dict(self.sell_rule) if self.sell_rule is not None else None,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "formula_text": self.formula_text,
            "formula_parameters": dict(self.formula_parameters),
            "instrument_types": list(self.instrument_types),
            "boards": list(self.boards),
            "search_sides": self.search_sides,
            "initial_cash": self.initial_cash,
            "commission": self.commission,
            "min_commission": self.min_commission,
            "stamp_tax": self.stamp_tax,
            "slippage": self.slippage,
            "execution": self.execution,
            "position_mode": self.position_mode,
            "fixed_size": self.fixed_size,
            "train_ratio": self.train_ratio,
            "validation_ratio": self.validation_ratio,
            "min_trades": self.min_trades,
            "min_instruments": self.min_instruments,
            "candidate_limit": self.candidate_limit,
            "holdout_candidates": self.holdout_candidates,
        }


def _has_rule(rule: RuleGroup) -> bool:
    return bool(rule.signals or rule.conditions or rule.required_conditions)
