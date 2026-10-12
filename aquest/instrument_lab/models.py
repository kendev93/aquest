"""Shared shapes for the single-instrument workbench.

这个模块里没有「评分」，只有读数。标的检验台存在的意义是让用户自己核对一只票
在指标、规则、条件三件事上的真实表现，而不是替它打一个分——所以每个数据类都
带着自己的口径说明（``note``），拿不到数就留 ``None``，绝不用 0 顶上。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

# 前瞻收益的 horizon：当日收盘 → N 日后收盘，与因子检验同一口径。
DEFAULT_HORIZON = 5
MIN_HORIZON = 1
MAX_HORIZON = 60
# 命中样本太少时，均值差本身就没意义，标「样本不足」但仍然把数给出来。
MIN_HIT_SAMPLES = 20
# 置换检验需要足够多的交易日，否则块置换会把同一批日期搬来搬去「确认」自己。
MIN_SAMPLE_DAYS = 60
DEFAULT_PERMUTATIONS = 200
MIN_PERMUTATIONS = 20
MAX_PERMUTATIONS = 2000
DEFAULT_BLOCK_SIZE = 20
DEFAULT_SEED = 20240918
DEFAULT_ALPHA = 0.05
# 时间轴点数上限：全历史可能有上万根 K 线，超出后等距抽样但首尾必留。
MAX_TIMELINE_POINTS = 1500
# 命中日期只回传最近的一批，够画 K 线标记即可。
MAX_HIT_DATES = 200
MAX_VALUE_IDS = 12
MAX_LIBRARY_CONDITIONS = 60
MAX_CHECK_CONDITIONS = 20

LOW_SAMPLE_NOTE = f"命中样本少于 {MIN_HIT_SAMPLES} 个，均值差不足以说明问题"
SHORT_SERIES_NOTE = f"可用交易日少于 {MIN_SAMPLE_DAYS} 天，未做置换检验"
NO_HIT_NOTE = "该标的上从未命中这个条件"


@dataclass(frozen=True)
class ValueTimeline:
    """One indicator's full-history readings for one instrument."""

    value_id: str
    label: str
    dates: tuple[str, ...]
    values: tuple[float | None, ...]
    last_value: float | None
    # 自身历史分位（0~100）：这只票跟它自己的历史比，不是跟全市场比。
    percentile: float | None
    note: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "value_id": self.value_id,
            "label": self.label,
            "dates": list(self.dates),
            "values": list(self.values),
            "last_value": self.last_value,
            "percentile": self.percentile,
            "note": self.note,
        }


@dataclass(frozen=True)
class InstrumentValues:
    """The timeline block's payload."""

    market: str
    code: str
    name: str | None
    start_date: str | None
    end_date: str | None
    bars: int
    series: tuple[ValueTimeline, ...]
    thinned: bool = False
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "market": self.market,
            "code": self.code,
            "name": self.name,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "bars": self.bars,
            "series": [item.to_dict() for item in self.series],
            "thinned": self.thinned,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class LibraryCondition:
    """A mined condition that survived its permutation test.

    「通过了置换检验」说的是**在挖掘时的全市场样本上**成立，放到某只票上是否还
    成立是另一回事——检验台要回答的正是这个。
    """

    key: str
    label: str
    side: str | None
    condition: Mapping[str, object]
    lift: float | None
    pvalue: float | None
    support_count: int | None
    instrument_count: int | None
    history_id: str | None = None
    created_at: str | None = None
    scanned_stocks: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "side": self.side,
            "condition": dict(self.condition),
            "lift": self.lift,
            "pvalue": self.pvalue,
            "support_count": self.support_count,
            "instrument_count": self.instrument_count,
            "history_id": self.history_id,
            "created_at": self.created_at,
            "scanned_stocks": self.scanned_stocks,
        }


@dataclass(frozen=True)
class ConditionCheck:
    """One condition's evidence on one instrument."""

    key: str
    label: str
    hit_count: int
    sample_days: int
    # 命中组 / 全样本的前瞻收益均值，差值是这两个数的差（百分点），不是比值。
    hit_mean_return: float | None
    base_mean_return: float | None
    difference: float | None
    p_value: float | None
    adjusted_p_value: float | None
    significant: bool | None
    hit_dates: tuple[str, ...] = ()
    note: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "hit_count": self.hit_count,
            "sample_days": self.sample_days,
            "hit_mean_return": self.hit_mean_return,
            "base_mean_return": self.base_mean_return,
            "difference": self.difference,
            "p_value": self.p_value,
            "adjusted_p_value": self.adjusted_p_value,
            "significant": self.significant,
            "hit_dates": list(self.hit_dates),
            "note": self.note,
        }


@dataclass(frozen=True)
class ConditionCheckReport:
    market: str
    code: str
    name: str | None
    horizon: int
    start_date: str | None
    end_date: str | None
    sample_days: int
    permutations: int
    block_size: int
    items: tuple[ConditionCheck, ...]
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "market": self.market,
            "code": self.code,
            "name": self.name,
            "horizon": self.horizon,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "sample_days": self.sample_days,
            "permutations": self.permutations,
            "block_size": self.block_size,
            "items": [item.to_dict() for item in self.items],
            "notes": list(self.notes),
        }


def thin_indices(count: int, limit: int = MAX_TIMELINE_POINTS) -> tuple[int, ...]:
    """Equally spaced indices that always keep the first and last point."""

    if count <= limit or limit <= 1:
        return tuple(range(count))
    step = (count - 1) / (limit - 1)
    indices = sorted({int(round(position * step)) for position in range(limit)})
    if indices[0] != 0:
        indices[0] = 0
    if indices[-1] != count - 1:
        indices[-1] = count - 1
    return tuple(indices)


def thin_values(
    values: Sequence[float | None], limit: int = MAX_TIMELINE_POINTS
) -> list[float | None]:
    indices = thin_indices(len(values), limit)
    return [values[index] for index in indices]
