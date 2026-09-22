"""条件研究请求体。"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from selector_app.condition_research.mining import DEFAULT_DUPLICATE_CORRELATION
from selector_app.condition_research.models import ConditionResearchConfig
from selector_app.formulas.custom import parse_formula
from selector_app.market_data.models import InstrumentBoard, InstrumentType

from ..date_ranges import (
    DateRangeMode,
    validate_date_range_request,
)
from ._shared import (
    _rankable_research_values,
    validate_formula_conditions,
    validate_formula_signals,
)
from .screening import RuleGroupRequest


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
