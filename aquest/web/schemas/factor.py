"""因子检验与因子归因请求体。"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from aquest.factor_attribution.models import (
    MIN_GROUP_TRADES,
    FactorAttributionConfig,
)
from aquest.factor_test.models import (
    DEFAULT_ALPHA,
    DEFAULT_BLOCK_SIZE,
    DEFAULT_HORIZON,
    DEFAULT_PERMUTATIONS,
    MIN_CROSS_SECTION,
    QUANTILE_COUNT,
    FactorTestConfig,
)
from aquest.market_data.models import InstrumentBoard, InstrumentType


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


class FactorAttributionRequest(BaseModel):
    """Explain one archived backtest: winners vs losers on the entry signal day.

    The source is an existing history id, not a strategy to re-run: re-running
    would mean a second opinion on what the strategy did, and this report exists
    precisely to avoid relitigating that.
    """

    model_config = ConfigDict(extra="forbid")

    source_history_id: str = Field(default="", max_length=64)
    value_ids: list[str] | None = Field(default=None, max_length=64)
    include_custom: bool = True
    min_group_trades: int = Field(default=MIN_GROUP_TRADES, ge=2, le=10_000)
    permutations: int = Field(default=DEFAULT_PERMUTATIONS, ge=0, le=5_000)
    block_size: int = Field(default=DEFAULT_BLOCK_SIZE, ge=1, le=500)
    alpha: float = Field(default=DEFAULT_ALPHA, gt=0, lt=1)

    def to_config(self) -> FactorAttributionConfig:
        return FactorAttributionConfig(
            source_history_id=self.source_history_id,
            value_ids=tuple(self.value_ids or ()),
            include_custom=self.include_custom,
            min_group_trades=self.min_group_trades,
            permutations=self.permutations,
            block_size_days=self.block_size,
            alpha=self.alpha,
        )
