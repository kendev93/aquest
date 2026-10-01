"""Immutable screening domain models."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from selector_app.concurrency import MAX_WORKERS
from selector_app.market_data.models import InstrumentBoard, InstrumentType
from selector_app.market_data.scope import InstrumentScope

CombineMode = Literal["all", "any", "at_least"]
Universe = Literal["all", "sh", "sz", "custom"]
RankOrder = Literal["asc", "desc"]
ConditionOperator = Literal["gt", "gte", "lt", "lte", "eq", "neq", "cross_up", "cross_down"]
_CONDITION_OPERATORS = frozenset({"gt", "gte", "lt", "lte", "eq", "neq", "cross_up", "cross_down"})


@dataclass(frozen=True)
class ScreenCondition:
    """A numeric comparison evaluated on the latest available bar."""

    left_value: str
    operator: ConditionOperator | str
    right_value: str | None = None
    right_constant: float | None = None

    def __post_init__(self) -> None:
        if not self.left_value.strip():
            raise ValueError("条件左值不能为空")
        if self.operator not in _CONDITION_OPERATORS:
            raise ValueError(f"不支持的条件运算符: {self.operator}")
        has_right_value = self.right_value is not None
        has_right_constant = self.right_constant is not None
        if has_right_value == has_right_constant:
            raise ValueError("条件必须且只能设置一个右值")
        if self.right_value is not None and not self.right_value.strip():
            raise ValueError("条件右侧指标不能为空")
        if self.right_constant is not None and (
            isinstance(self.right_constant, bool) or not math.isfinite(self.right_constant)
        ):
            raise ValueError("条件常数必须是有限数字")
        object.__setattr__(self, "left_value", self.left_value.strip())
        if self.right_value is not None:
            object.__setattr__(self, "right_value", self.right_value.strip())


@dataclass(frozen=True)
class ScanConfig:
    selected_signals: tuple[str, ...]
    combine_mode: CombineMode | str
    minimum_matches: int | None
    universe: Universe | str
    universe_file: str | None
    vipdoc_path: str
    workers: int
    period: str
    instrument_types: tuple[InstrumentType, ...] = ()
    boards: tuple[InstrumentBoard, ...] = ()
    formula_text: str | None = None
    formula_parameters: Mapping[str, float] = field(default_factory=dict)
    conditions: tuple[ScreenCondition, ...] = ()
    ranking_value: str | None = None
    rank_order: RankOrder = "desc"

    def __post_init__(self) -> None:
        if isinstance(self.workers, bool) or not 1 <= self.workers <= MAX_WORKERS:
            raise ValueError(f"扫描并发数必须在 1 到 {MAX_WORKERS} 之间")
        if self.rank_order not in {"asc", "desc"}:
            raise ValueError("排序方向必须是 asc 或 desc")
        if self.ranking_value is not None and not self.ranking_value.strip():
            raise ValueError("排序指标不能为空")
        if self.ranking_value is not None:
            object.__setattr__(self, "ranking_value", self.ranking_value.strip())
        scope = InstrumentScope.from_values(
            universe=str(self.universe),
            instrument_types=self.instrument_types,
            boards=self.boards,
        )
        object.__setattr__(self, "instrument_types", scope.instrument_types or ())
        object.__setattr__(self, "boards", scope.boards or ())
        object.__setattr__(
            self, "formula_parameters", MappingProxyType(dict(self.formula_parameters))
        )
        object.__setattr__(self, "conditions", tuple(self.conditions))


@dataclass(frozen=True)
class ScreenMatch:
    market: str
    code: str
    signal_date: int
    last_close: float
    matched_signals: tuple[str, ...]
    match_count: int
    indicator_values: Mapping[str, float | None]
    instrument_type: str = "stock"
    board: InstrumentBoard = "main"
    name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "indicator_values", MappingProxyType(dict(self.indicator_values)))

    def to_dict(self) -> dict[str, object]:
        return {
            "market": self.market,
            "code": self.code,
            "name": self.name,
            "signal_date": self.signal_date,
            "last_close": self.last_close,
            "matched_signals": list(self.matched_signals),
            "match_count": self.match_count,
            "indicator_values": dict(self.indicator_values),
            "instrument_type": self.instrument_type,
            "board": self.board,
        }


@dataclass(frozen=True)
class ScanReport:
    total_candidates: int
    total_scanned: int
    total_signals: int
    errors: int
    skipped: int
    results: tuple[ScreenMatch, ...]
    failure_reasons: Mapping[str, int]
    skip_reasons: Mapping[str, int]
    ranking_value: str | None = None
    rank_order: RankOrder = "desc"

    def __post_init__(self) -> None:
        object.__setattr__(self, "failure_reasons", MappingProxyType(dict(self.failure_reasons)))
        object.__setattr__(self, "skip_reasons", MappingProxyType(dict(self.skip_reasons)))

    def summary_dict(self) -> dict[str, object]:
        summary: dict[str, object] = {
            "total_candidates": self.total_candidates,
            "total_scanned": self.total_scanned,
            "total_signals": self.total_signals,
            "errors": self.errors,
            "skipped": self.skipped,
            "failure_reasons": dict(self.failure_reasons),
            "skip_reasons": dict(self.skip_reasons),
        }
        if self.ranking_value is not None:
            summary.update({"ranking_value": self.ranking_value, "rank_order": self.rank_order})
        return summary
