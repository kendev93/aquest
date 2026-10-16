"""行情数据与同步相关的请求体。"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aquest.market_data.health import REPAIR_MAX_CODES, CheckKey
from aquest.market_data.models import InstrumentBoard, InstrumentType

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
    # 兼容保留、不再生效：区间由库里每只标的自己的覆盖决定（空库 2015-01-01 起全量，
    # 已有则补到今天并回看若干天做逐值对照）。老调用还在发这个值，所以留着并继续校验。
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


class MarketRefreshRequest(MarketSyncRequest):
    """Re-download every bar of the selected scope from scratch.

    常规同步只补最新一段，全量补齐只补已有区间**之外**的缺口，两者都覆盖不到
    中间已经写歪的那段。这个入口把范围内标的的日线先删干净再整段重下——和
    ``HealthRepairRequest`` 同级别的破坏性，所以同样要求显式 ``confirm``。
    """

    model_config = ConfigDict(extra="forbid")

    confirm: bool = False

    @model_validator(mode="after")
    def validate_confirmation(self) -> MarketRefreshRequest:
        if not self.confirm:
            raise ValueError(
                "全量重刷会先删除所选范围内的全部日线再重新下载，请传 confirm=true 明确确认"
            )
        return self

    def as_sync_request(self) -> MarketSyncRequest:
        """Rebuild the plain sync payload: 重刷永远是全历史。"""

        return MarketSyncRequest(**{**self.model_dump(exclude={"confirm"}), "full_history": True})


class HealthRepairRequest(BaseModel):
    """Re-download every bar for the instruments flagged by the health check.

    破坏性比删除更含蓄但同样不可逆：**先删掉这些标的的全部历史，再全量重下**。
    在线源已经没有的标的（退市）删了就补不回来，所以和 ``BarDeleteRequest``
    一样要求显式 ``confirm``，并且一次最多修 ``REPAIR_MAX_CODES`` 只。
    """

    model_config = ConfigDict(extra="forbid")

    #: 留空 = 修所有可修复的检查项（见 health.CHECK_REPAIRABLE）。
    checks: list[CheckKey] | None = Field(default=None, max_length=6)
    #: 直接指定标的（六位代码）时跳过检查项扫描。
    codes: list[str] | None = Field(default=None, max_length=REPAIR_MAX_CODES)
    limit: int = Field(default=REPAIR_MAX_CODES, ge=1, le=REPAIR_MAX_CODES)
    #: 名单按 ``market, code`` 排序后从第几位开始取。命中面一旦超过上限，修复结果里的
    #: 缺口标记不会消失（停牌缺口重下也修不掉），所以只能靠 offset 往后推进下一批，
    #: 不能指望"再点一次"自动换标的。给了 ``codes`` 时忽略。
    offset: int = Field(default=0, ge=0)
    confirm: bool = False

    @model_validator(mode="after")
    def validate_scope(self) -> HealthRepairRequest:
        if not self.confirm:
            raise ValueError(
                "修复会先删除这些标的的全部历史行情再重新下载，请传 confirm=true 明确确认"
            )
        if self.codes is not None and any(not str(code).strip().isdigit() for code in self.codes):
            raise ValueError("标的代码只能是数字")
        return self
