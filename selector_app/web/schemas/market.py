"""行情数据与同步相关的请求体。"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from selector_app.market_data.models import InstrumentBoard, InstrumentType

from ._shared import validate_instrument_scope


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
