"""Reusable rule groups shared by screening and trading simulations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .models import ScreenCondition

RuleCombineMode = Literal["all", "any", "at_least"]


@dataclass(frozen=True)
class RuleGroup:
    """A user-composable group of signal and numeric conditions."""

    signals: tuple[str, ...] = ()
    conditions: tuple[ScreenCondition, ...] = ()
    combine_mode: RuleCombineMode | str = "all"
    minimum_matches: int | None = None

    def __post_init__(self) -> None:
        signals = tuple(signal.strip() for signal in self.signals if signal.strip())
        conditions = tuple(self.conditions)
        count = len(signals) + len(conditions)
        if count == 0:
            raise ValueError("交易规则至少需要一个信号或指标条件")
        if self.combine_mode not in {"all", "any", "at_least"}:
            raise ValueError("不支持的交易规则组合方式")
        if self.combine_mode == "at_least":
            if self.minimum_matches is None or not 1 <= self.minimum_matches <= count:
                raise ValueError("交易规则 minimum_matches 必须在条件数量范围内")
        elif self.minimum_matches is not None:
            raise ValueError("all/any 交易规则不应设置 minimum_matches")
        object.__setattr__(self, "signals", signals)
        object.__setattr__(self, "conditions", conditions)
