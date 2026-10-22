"""Validated immutable settings for one-symbol research runs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Literal

from aquest.market_data.models import MarketCode
from aquest.strategy_discovery.candidates import DEFAULT_MAX_HOLD_DAYS

from .search_policy import CANDIDATE_LIMIT


@dataclass(frozen=True)
class IndividualResearchConfig:
    market: MarketCode
    code: str
    name: str
    start_date: date
    end_date: date
    value_ids: tuple[str, ...]
    signal_ids: tuple[str, ...]
    reversal_multiple: float = 2.0
    atr_window: int = 14
    min_bars: int = 5
    horizon: int = 10
    search_policy: Literal["legacy", "adaptive-v1"] = "adaptive-v1"
    candidate_limit: int = CANDIDATE_LIMIT
    risk_trials: Literal[1000, 2000, 3000] = 1000
    #: 最长持有**交易日**数，传给每个候选回测的 ``PortfolioBacktestConfig``。
    #: 之前这里从不设置——引擎支持时间止损但池内策略永远拿不到，42 个持仓里
    #: 13 个超过 100 天就是这么来的。语义与默认值见
    #: ``strategy_discovery.candidates.DEFAULT_MAX_HOLD_DAYS``。
    max_hold_days: int | None = DEFAULT_MAX_HOLD_DAYS
    initial_cash: float = 1_000_000.0
    commission: float = 0.0001
    min_commission: float = 0.1
    stamp_tax: float = 0.001
    slippage: float = 0.0
    execution: Literal["next_open", "next_close"] = "next_open"
    position_mode: Literal["full", "fixed"] = "full"
    fixed_size: int | None = None
    split_windows: Mapping[str, tuple[date, date]] | None = None
    common_split_metadata: Mapping[str, object] | None = None
    #: 研究窗口开头只作指标预热的 K 线根数；0 表示不启用（手动研究默认）。
    #: 只在标的自身数据起点就落在研究起点之后时生效（2016 年后上市的池内标的）。
    warmup_bars: int = 0
    #: 预热之后至少保留的研究 K 线根数；不足时判为历史样本不足。0 表示不限制。
    min_research_bars: int = 0

    @property
    def symbol(self) -> str:
        return f"{self.market}{self.code}"
