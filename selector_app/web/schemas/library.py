"""筛选预设、候选库与历史对比请求体。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ScreenPresetCreateRequest(BaseModel):
    """One named screening configuration.

    ``payload`` 只收「可复现的输入」。服务端会做白名单裁剪并把改了什么写进 notes，
    所以这里的类型刻意放得宽——真正的约束在 screen_presets.payload 里。
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=60)
    note: str = Field(default="", max_length=200)
    payload: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_name(self) -> ScreenPresetCreateRequest:
        if not self.name.strip():
            raise ValueError("方案名称不能为空")
        return self


class ScreenPresetUpdateRequest(BaseModel):
    """Rename, re-note, or overwrite a preset's payload."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=60)
    note: str | None = Field(default=None, max_length=200)
    payload: dict[str, object] | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> ScreenPresetUpdateRequest:
        if self.name is None and self.note is None and self.payload is None:
            raise ValueError("至少要修改名称、备注或内容之一")
        if self.name is not None and not self.name.strip():
            raise ValueError("方案名称不能为空")
        return self


class CandidateImportRequest(BaseModel):
    """Copy mined candidates out of one study into the library."""

    model_config = ConfigDict(extra="forbid")

    history_id: str = Field(min_length=1, max_length=128)
    candidate_ids: list[str] = Field(min_length=1, max_length=20)
    note: str = Field(default="", max_length=300)

    @model_validator(mode="after")
    def validate_ids(self) -> CandidateImportRequest:
        if not self.history_id.strip():
            raise ValueError("研究记录 ID 不能为空")
        cleaned = [item.strip() for item in self.candidate_ids if item.strip()]
        if not cleaned:
            raise ValueError("至少要选择一条候选条件")
        self.candidate_ids = cleaned
        return self


class CandidateStatusRequest(BaseModel):
    """Adopt, reject, reopen or withdraw one candidate."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["pending", "adopted", "rejected", "published"]
    note: str | None = Field(default=None, max_length=300)


class CandidatePublishRequest(BaseModel):
    """Publish one adopted candidate as a strategy - offline, always."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=60)
    note: str = Field(default="", max_length=300)
    # 允许覆盖的只有"买什么、买几只、用多少钱、什么时候离场"这几项。其余一律
    # 沿用来源研究的配置，否则发布就变成了第二个配置界面，而它的配置没人核对过。
    # 止损/止盈必须开放：研究可以拿「持有到期末」当卖出基准，策略不行——不设离场
    # 的回测只买不卖，净值说明不了任何事。
    ranking_value: str | None = Field(default=None, max_length=128)
    rank_order: Literal["asc", "desc"] | None = None
    max_positions: int | None = Field(default=None, ge=1, le=100)
    initial_cash: float | None = Field(default=None, gt=0, le=1_000_000_000_000)
    stop_loss_pct: float | None = Field(default=None, gt=0, le=1)
    take_profit_pct: float | None = Field(default=None, gt=0, le=10)

    @model_validator(mode="after")
    def validate_name(self) -> CandidatePublishRequest:
        if not self.name.strip():
            raise ValueError("策略名称不能为空")
        return self


class StockGroupMemberRequest(BaseModel):
    """One instrument inside a group."""

    model_config = ConfigDict(extra="forbid")

    market: str = Field(min_length=1, max_length=8)
    code: str = Field(min_length=1, max_length=16)
    name: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_identity(self) -> StockGroupMemberRequest:
        if not self.market.strip():
            raise ValueError("标的市场不能为空")
        if not self.code.strip():
            raise ValueError("标的代码不能为空")
        object.__setattr__(self, "market", self.market.strip().upper())
        object.__setattr__(self, "code", self.code.strip())
        return self


class StockGroupCreateRequest(BaseModel):
    """Save a basket of instruments.

    ``items`` 上限 200 与组合回测的 codes 上限一致：组最终是要拿去观察表现的，
    超过这个数后面那步会被拒，不如在存的时候就拦住。
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=60)
    note: str = Field(default="", max_length=300)
    items: list[StockGroupMemberRequest] = Field(min_length=1, max_length=200)
    source: Literal["manual", "screen"] = "manual"
    source_history_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_name(self) -> StockGroupCreateRequest:
        if not self.name.strip():
            raise ValueError("分组名称不能为空")
        return self


class StockGroupUpdateRequest(BaseModel):
    """Rename, re-note or replace the members of one group."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=60)
    note: str | None = Field(default=None, max_length=300)
    items: list[StockGroupMemberRequest] | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_payload(self) -> StockGroupUpdateRequest:
        if self.name is None and self.note is None and self.items is None:
            raise ValueError("至少要修改名称、备注或成员之一")
        if self.name is not None and not self.name.strip():
            raise ValueError("分组名称不能为空")
        return self


class StockGroupRemoveMembersRequest(BaseModel):
    """Drop individual members from a group."""

    model_config = ConfigDict(extra="forbid")

    members: list[StockGroupMemberRequest] = Field(min_length=1, max_length=200)


class HistoryCompareRequest(BaseModel):
    """Two to five archived runs to lay out side by side.

    上限不是随手定的：五条以上曲线叠在一张图上，颜色都分不出来，读图的人只会
    记住最上面那条。归一化默认除以本金——本金不同的两次实验，收益率口径才是
    可比的（取不到本金时服务端会退回按首值归一，并在结果里写明）。
    """

    model_config = ConfigDict(extra="forbid")

    history_ids: list[str] = Field(min_length=2, max_length=5)
    normalize: Literal["capital", "first"] = "capital"
    x_axis: Literal["date", "index"] = "date"

    @model_validator(mode="after")
    def validate_ids(self) -> HistoryCompareRequest:
        cleaned = [item.strip() for item in self.history_ids if item.strip()]
        if len(cleaned) < 2:
            raise ValueError("至少需要选择 2 条历史记录")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("同一条历史记录不能重复选择")
        self.history_ids = cleaned
        return self
