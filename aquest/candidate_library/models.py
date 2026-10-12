"""Shared vocabulary for the mined-condition candidate library.

The library exists because a mined condition has no identity of its own: the
ids a study hands out (``buy-1``) are runtime sequence numbers, and the
evidence lives in a history table that keeps only the newest 20 records. A
stored candidate therefore carries its own id, its own evidence snapshot and
the user's own verdict.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

#: 来源类型。目前只有条件研究；策略发现的候选是整套配置（带 candidate_hash），
#: 粒度差一个量级，混进来两边都别扭，所以字段先留着但不接。
SOURCE_CONDITION_RESEARCH: Final = "condition_research"
SOURCE_LABELS: Final[dict[str, str]] = {
    SOURCE_CONDITION_RESEARCH: "买卖条件研究",
}

#: 候选状态。adopted 是人的表态（我认可这条发现），published 是人的二次确认
#: （把它变成策略）。显著 ≠ adopted：前者是统计结论，后者是人的决定。
STATUS_PENDING: Final = "pending"
STATUS_ADOPTED: Final = "adopted"
STATUS_REJECTED: Final = "rejected"
STATUS_PUBLISHED: Final = "published"

STATUS_LABELS: Final[dict[str, str]] = {
    STATUS_PENDING: "待定",
    STATUS_ADOPTED: "已采纳",
    STATUS_REJECTED: "已否决",
    STATUS_PUBLISHED: "已发布",
}

#: 允许的状态流转。没有 pending → published 这条路：不表态就发布等于把「通过
#: 检验」当成「我认可」，而这两件事从来不是一回事。
ALLOWED_TRANSITIONS: Final[dict[str, tuple[str, ...]]] = {
    STATUS_PENDING: (STATUS_ADOPTED, STATUS_REJECTED),
    STATUS_ADOPTED: (STATUS_PUBLISHED, STATUS_REJECTED, STATUS_PENDING),
    STATUS_REJECTED: (STATUS_PENDING,),
    # 撤回：published → adopted。删除由它生成的那条策略是调用方的事（删不掉要
    # 拒绝，不能留下孤儿策略）。
    STATUS_PUBLISHED: (STATUS_ADOPTED,),
}

SIDE_BUY: Final = "buy"
SIDE_SELL: Final = "sell"
SIDE_LABELS: Final[dict[str, str]] = {SIDE_BUY: "买入", SIDE_SELL: "卖出"}

MAX_LIBRARY_CANDIDATES: Final = 500
MAX_IMPORT_PER_REQUEST: Final = 20
NOTE_MAX_LENGTH: Final = 300
NAME_MAX_LENGTH: Final = 60


@dataclass(frozen=True)
class PublishPlan:
    """What publishing one candidate would actually write into the strategy library."""

    #: 拼好的组合回测请求（日期已固化成绝对日期，候选条件已按方向合并进规则）。
    request: dict[str, object]
    #: 不拦人但必须说明的事（用了什么默认值、条件被合并在哪一侧、来源研究当初
    #: 是持续满仓/持有到期末所以基准被替换了）。
    notes: tuple[str, ...]
    #: 拦人的事：猜不出来，必须由用户补，否则发布会在校验阶段失败。
    blockers: tuple[str, ...]
    #: 来源研究的配置本身（不含候选条件），给前端摊开用。
    base_request: dict[str, object]

    @property
    def blocked(self) -> bool:
        return bool(self.blockers)


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def side_label(side: str) -> str:
    return SIDE_LABELS.get(side, side)


def can_transition(current: str, target: str) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, ())


def transition_reason(current: str, target: str) -> str:
    """Why a status change is refused - written for the person who asked for it."""

    from_label = status_label(current)
    to_label = status_label(target)
    if current == target:
        return f"这条候选已经是「{from_label}」了。"
    if current == STATUS_PENDING and target == STATUS_PUBLISHED:
        return "发布前必须先采纳：通过检验是统计结论，采纳是你的表态，两者不能互相冒充。"
    if current == STATUS_PUBLISHED and target == STATUS_PUBLISHED:
        return "这条候选已经发布过了。要换一套配置，请先撤回发布再重新发布。"
    if current == STATUS_REJECTED and target in (STATUS_ADOPTED, STATUS_PUBLISHED):
        return f"这条候选已被否决，先恢复成「{STATUS_LABELS[STATUS_PENDING]}」再决定。"
    return f"「{from_label}」不能直接变成「{to_label}」。"


def as_mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}
