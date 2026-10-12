"""标的检验台请求体。"""

from __future__ import annotations

import math
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aquest.instrument_lab.models import (
    DEFAULT_ALPHA as LAB_DEFAULT_ALPHA,
)
from aquest.instrument_lab.models import (
    DEFAULT_BLOCK_SIZE as LAB_DEFAULT_BLOCK_SIZE,
)
from aquest.instrument_lab.models import (
    DEFAULT_HORIZON as LAB_DEFAULT_HORIZON,
)
from aquest.instrument_lab.models import (
    DEFAULT_PERMUTATIONS as LAB_DEFAULT_PERMUTATIONS,
)


class InstrumentLabInstrument(BaseModel):
    """Which stock the workbench is looking at."""

    market: Literal["SH", "SZ"] = "SH"
    code: str = Field(pattern=r"^\d{6}$")
    vipdoc_path: str = Field(default="", max_length=1024)
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def validate_window(self) -> InstrumentLabInstrument:
        if self.start_date is not None and self.end_date is not None:
            if self.start_date > self.end_date:
                raise ValueError("开始日期不能晚于结束日期")
        return self


class InstrumentLabValuesRequest(InstrumentLabInstrument):
    """Indicator timeline request: readings only, never a score."""

    model_config = ConfigDict(extra="forbid")

    value_ids: list[str] = Field(min_length=1, max_length=12)


class InstrumentLabCondition(BaseModel):
    """One stored condition, posted back for a single-instrument check."""

    model_config = ConfigDict(extra="forbid")

    left_value: str = Field(min_length=1, max_length=128)
    operator: Literal["gt", "gte", "lt", "lte", "eq", "neq", "cross_up", "cross_down"]
    right_value: str | None = Field(default=None, max_length=128)
    right_constant: float | None = None
    label: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def validate_side(self) -> InstrumentLabCondition:
        has_value = self.right_value is not None and bool(self.right_value.strip())
        constant = self.right_constant
        if has_value == (constant is not None):
            raise ValueError("条件必须且只能设置一个右值")
        if constant is not None and not math.isfinite(constant):
            raise ValueError("条件常数必须是有限数字")
        return self


class InstrumentLabCheckRequest(InstrumentLabInstrument):
    """Re-test mined conditions on one instrument.

    默认参数与因子检验一致（5 日前瞻、200 次块置换），这样两边的 p 值是同一种
    东西，不会因为「换了个页面算」而不可比。
    """

    model_config = ConfigDict(extra="forbid")

    # 边界由服务层用同一份常量校验（那里才是唯一真相），这里只挡住明显无效的输入。
    conditions: list[InstrumentLabCondition] = Field(min_length=1, max_length=20)
    horizon: int = Field(default=LAB_DEFAULT_HORIZON, ge=1)
    permutations: int = Field(default=LAB_DEFAULT_PERMUTATIONS, ge=1)
    block_size: int = Field(default=LAB_DEFAULT_BLOCK_SIZE, ge=1)
    alpha: float = Field(default=LAB_DEFAULT_ALPHA, gt=0, lt=1)

    @model_validator(mode="after")
    def validate_conditions(self) -> InstrumentLabCheckRequest:
        keys = [
            (
                item.left_value.strip(),
                item.operator,
                (item.right_value or "").strip(),
                item.right_constant,
            )
            for item in self.conditions
        ]
        if len(set(keys)) != len(keys):
            raise ValueError("同一条条件不能重复选择")
        return self
