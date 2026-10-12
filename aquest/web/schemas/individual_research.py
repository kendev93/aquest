"""Request validation for single-security research."""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aquest.individual_research.models import IndividualResearchConfig
from aquest.individual_research.search_policy import CANDIDATE_LIMIT
from aquest.market_data.day_format import classify_instrument
from aquest.strategy_discovery.catalog import default_indicator_catalog

from ..date_ranges import DateRangeMode, validate_date_range_request

_CODE_PATTERN = re.compile(r"^\d{6}$")


class IndividualResearchRequest(BaseModel):
    """One A-share instrument, causal swing labels, and bounded risk search."""

    model_config = ConfigDict(extra="forbid")

    market: Literal["SH", "SZ"]
    code: str = Field(min_length=6, max_length=6)
    date_mode: DateRangeMode = "full"
    start_date: date | None = None
    end_date: date | None = None
    reversal_multiple: float = Field(default=2.0, gt=0, le=20)
    atr_window: int = Field(default=14, ge=1, le=250)
    min_bars: int = Field(default=5, ge=0, le=1_000)
    horizon: int = Field(default=10, ge=1, le=250)
    value_ids: list[str] | None = Field(default=None, max_length=256)
    signal_ids: list[str] | None = Field(default=None, max_length=256)
    search_policy: Literal["legacy", "adaptive-v1"] = "adaptive-v1"
    candidate_limit: int = Field(default=CANDIDATE_LIMIT, ge=1, le=CANDIDATE_LIMIT)
    risk_trials: Literal[1_000, 2_000, 3_000] = 1_000
    #: 最长持仓**交易日**数，作为统一上限传给每个候选（None = 不限制）。
    max_hold_days: int | None = Field(default=20, ge=1, le=10_000)
    initial_cash: float = Field(default=1_000_000.0, gt=0, le=1_000_000_000_000)
    commission: float = Field(default=0.0001, ge=0, le=0.1)
    min_commission: float = Field(default=0.1, ge=0, le=100_000)
    stamp_tax: float = Field(default=0.001, ge=0, le=0.1)
    slippage: float = Field(default=0.0, ge=0, le=1_000)
    execution: Literal["next_open", "next_close"] = "next_open"
    position_mode: Literal["full", "fixed"] = "full"
    fixed_size: int | None = Field(default=None, ge=100, le=100_000_000)

    @model_validator(mode="after")
    def validate_request(self) -> IndividualResearchRequest:
        validate_date_range_request(self.date_mode, self.start_date, self.end_date)
        if not _CODE_PATTERN.fullmatch(self.code):
            raise ValueError("证券代码必须是 6 位数字")
        if classify_instrument(self.market, self.code) != "stock":
            raise ValueError("个股研究只支持 A 股股票")
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError("研究开始日期和结束日期必须同时填写")
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("研究开始日期不能晚于结束日期")
        catalog = default_indicator_catalog()
        if self.value_ids is not None:
            if not self.value_ids:
                raise ValueError("至少选择一个可搜索指标")
            unknown = [value_id for value_id in self.value_ids if value_id not in catalog.value_ids]
            if unknown:
                raise ValueError(f"指标不可用于个股条件搜索: {', '.join(unknown)}")
            if len(set(self.value_ids)) != len(self.value_ids):
                raise ValueError("指标不能重复选择")
        if self.signal_ids is not None:
            unknown_signals = [
                signal_id for signal_id in self.signal_ids if signal_id not in catalog.signal_ids
            ]
            if unknown_signals:
                raise ValueError(f"信号不可用于个股研究: {', '.join(unknown_signals)}")
            if len(set(self.signal_ids)) != len(self.signal_ids):
                raise ValueError("信号不能重复选择")
        if self.position_mode == "fixed":
            if self.fixed_size is None or self.fixed_size % 100 != 0:
                raise ValueError("固定股数必须是 100 的整数倍")
        elif self.fixed_size is not None:
            raise ValueError("全仓模式不应设置固定股数")
        return self

    def to_config(
        self, *, start_date: date, end_date: date, name: str = ""
    ) -> IndividualResearchConfig:
        catalog = default_indicator_catalog()
        return IndividualResearchConfig(
            market=self.market,
            code=self.code,
            name=name,
            start_date=start_date,
            end_date=end_date,
            value_ids=tuple(self.value_ids or catalog.value_ids),
            signal_ids=tuple(
                self.signal_ids if self.signal_ids is not None else catalog.signal_ids
            ),
            reversal_multiple=self.reversal_multiple,
            atr_window=self.atr_window,
            min_bars=self.min_bars,
            horizon=self.horizon,
            search_policy=self.search_policy,
            candidate_limit=self.candidate_limit,
            risk_trials=self.risk_trials,
            max_hold_days=self.max_hold_days,
            initial_cash=self.initial_cash,
            commission=self.commission,
            min_commission=self.min_commission,
            stamp_tax=self.stamp_tax,
            slippage=self.slippage,
            execution=self.execution,
            position_mode=self.position_mode,
            fixed_size=self.fixed_size,
        )


__all__ = ["IndividualResearchRequest"]
