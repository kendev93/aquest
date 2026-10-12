"""Validated configuration for cross-stock buy/sell condition research."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from types import MappingProxyType
from typing import Literal

from aquest.market_data.models import InstrumentBoard, InstrumentType
from aquest.screening.models import Universe
from aquest.screening.rules import RuleGroup

from .mining import DEFAULT_DUPLICATE_CORRELATION
from .time_split import split_calendar_range

SearchSides = Literal["buy", "sell", "both"]
ExecutionMode = Literal["next_open", "next_close"]
PositionMode = Literal["full", "fixed"]
# 排序方向：desc = 指标值越大越优先，asc = 越小越优先。
RankOrder = Literal["asc", "desc"]

RESEARCH_INSTRUMENT_TYPES: tuple[InstrumentType, ...] = ("stock",)


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
    # 同义条件合并门槛：两条候选的命中向量相关性达到这个值就算同一个发现，
    # 只保留排序最好的那条，其余挂在它的 duplicates 里（不静默丢弃）。
    # 默认 0.6：同一指标的相邻分位阈值相关性约 0.61~0.67，会合并；
    # 相隔一个分位的降到 0.41，保留为独立候选（它们代表不同的激进度）。
    # 调到 1.0 ≈ 关闭（仍会合并命中集合几乎完全一致的条件）。
    duplicate_correlation: float = DEFAULT_DUPLICATE_CORRELATION
    holdout_candidates: int = 1
    # 滚动复验窗口数：把留出段再切成几段，逐个回放同一批候选。1 = 不拆（现状）。
    # 它回答的是"这条挖出来的条件换个时间段还成立吗"，不是真 walk-forward
    # ——挖掘仍然只在训练段做一次，各窗共用同一批候选，所以窗口之间可比。
    walk_forward_windows: int = 1
    # 置换检验：挖掘会评估上千个阈值，即使全是噪声，"最好的那一个"lift 也会很好看。
    # 这里把标签随机打乱重新评分若干次，看纯运气能把 lift 推到多高，用它当门槛。
    # 0 = 不做检验（结果里没有显著性标记）。
    # 200 而不是 20：标定过纯噪声数据集，20 次时名义 5% 的门槛实际误报 11.7%，
    # 200 次回到 5.0%。次数少了 p 值的分辨率也不够（20 次最细只能到 1/21）。
    significance_permutations: int = 200
    # 门槛取噪声最大 lift 分布的这个分位数，越大越严格。
    significance_quantile: float = 0.95
    # 排序规则：研究本身不构建组合、不设槽位，但它挖出的条件最终要落到组合回测里，
    # 而组合是「先按条件筛出候选池，再按排序抢有限槽位」。带上排序规则，研究才能
    # 顺带回答「该按什么排序」以及「这个条件在 N 个槽位下还有没有意义」。
    # 不填 = 不指定，研究照常跑，只是不给排序建议。
    ranking_value: str | None = None
    rank_order: RankOrder = "desc"
    # 用于估算「前 N 名」的槽位数，默认对齐组合回测的默认持仓上限。
    ranking_top_n: int = 5

    def __post_init__(self) -> None:
        if self.universe not in {"all", "sh", "sz"}:
            raise ValueError("股票范围必须是 all、sh 或 sz")
        if any(item not in RESEARCH_INSTRUMENT_TYPES for item in self.instrument_types):
            raise ValueError("买卖条件研究只支持股票，ETF、指数与债券不参与候选池")
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
        if not 0.5 <= self.duplicate_correlation <= 1.0:
            raise ValueError("同义条件合并门槛必须在 0.5 和 1 之间")
        if not 1 <= self.holdout_candidates <= 3:
            raise ValueError("留出期复验候选数必须在 1 到 3 之间")
        if not 1 <= self.walk_forward_windows <= 5:
            raise ValueError("滚动复验窗口数必须在 1 到 5 之间")
        if not 0 <= self.significance_permutations <= 500:
            raise ValueError("置换检验次数必须在 0 到 500 之间")
        if not 0.0 < self.significance_quantile <= 1.0:
            raise ValueError("显著性分位数必须在 0 和 1 之间")
        if self.ranking_value is not None and not self.ranking_value.strip():
            raise ValueError("排序指标不能是空字符串，不指定请直接留空")
        if self.rank_order not in {"asc", "desc"}:
            raise ValueError("排序方向必须是 asc 或 desc")
        if not 1 <= self.ranking_top_n <= 50:
            raise ValueError("排序槽位数必须在 1 到 50 之间")
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
        object.__setattr__(self, "boards", boards)
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
            "duplicate_correlation": self.duplicate_correlation,
            "holdout_candidates": self.holdout_candidates,
            "walk_forward_windows": self.walk_forward_windows,
            "significance_permutations": self.significance_permutations,
            "significance_quantile": self.significance_quantile,
            # 排序规则要能随历史记录一起还原，否则回看时无法知道当年是按什么排序推荐的。
            "ranking_value": self.ranking_value,
            "rank_order": self.rank_order,
            "ranking_top_n": self.ranking_top_n,
        }


def _has_rule(rule: RuleGroup) -> bool:
    return bool(rule.signals or rule.conditions or rule.required_conditions)
