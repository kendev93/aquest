"""Configuration and report shapes for trade attribution.

Attribution counts **trades**, not trading days, so the sample-size guards from
the factor test (which are stated in days and cross-sections) do not carry over.
They are restated here in the unit this study actually samples.
"""

from __future__ import annotations

from dataclasses import dataclass

# The permutation parameters live with the statistical primitives; importing
# them beats keeping a fourth copy of the same four numbers in sync by hand.
from aquest.factor_test.models import DEFAULT_ALPHA
from aquest.swing_research.scoring import (
    DEFAULT_BLOCK_SIZE,
    DEFAULT_PERMUTATIONS,
    DEFAULT_SEED,
)

#: Below this many paired round trips nothing can be said at all: with a
#: handful of trades the split between wins and losses is one unlucky week.
MIN_TOTAL_TRADES = 10
#: A group smaller than this still gets its difference reported, but it never
#: gets a verdict - twenty trades is where the mean stops being anecdote.
MIN_GROUP_TRADES = 20
#: Fewer distinct entry signal days than this and the block permutation has
#: nothing to shuffle: the null would be built from one or two blocks.
MIN_SIGNAL_DAYS = 10

LOW_SAMPLE_NOTE = "胜组或败组不足 20 笔，差值只作描述，不判断显著性"
SHORT_SERIES_NOTE = "入场信号日不足 10 天，样本太集中，无法估计噪声门槛"
CONSTANT_NOTE = "该指标在样本内取值恒定，区分不出胜负"
NO_SPLIT_NOTE = "样本里没有败单（或没有胜单），无从比较"

__all__ = [
    "CONSTANT_NOTE",
    "DEFAULT_ALPHA",
    "DEFAULT_BLOCK_SIZE",
    "DEFAULT_PERMUTATIONS",
    "DEFAULT_SEED",
    "LOW_SAMPLE_NOTE",
    "MIN_GROUP_TRADES",
    "MIN_SIGNAL_DAYS",
    "MIN_TOTAL_TRADES",
    "AttributionFactor",
    "FactorAttributionConfig",
]


@dataclass(frozen=True)
class AttributionFactor:
    """One indicator, compared between the winning and the losing trades."""

    value_id: str
    display_name: str
    #: Mean over winning trades, ``None`` when no winning trade has a value.
    win_mean: float | None
    loss_mean: float | None
    #: 胜组均值 − 败组均值。**差值不比值**：指标值可以取负，两负数相除会读反。
    difference: float | None
    #: Difference divided by the pooled standard deviation. Only used to rank
    #: indicators of different units against each other.
    standardized: float | None
    win_count: int
    loss_count: int
    #: Trades dropped because the signal day had no usable value for it.
    missing: int
    p_value: float | None = None
    adjusted_p_value: float | None = None
    #: ``None`` means "not judged" (too few trades, or no noise floor), which is
    #: deliberately not the same thing as ``False``.
    significant: bool | None = None
    note: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "value_id": self.value_id,
            "display_name": self.display_name,
            "win_mean": self.win_mean,
            "loss_mean": self.loss_mean,
            "difference": self.difference,
            "standardized": self.standardized,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "missing": self.missing,
            "p_value": self.p_value,
            "adjusted_p_value": self.adjusted_p_value,
            "significant": self.significant,
            "note": self.note,
        }


@dataclass(frozen=True)
class FactorAttributionConfig:
    """One request to explain a finished backtest's trades."""

    source_history_id: str
    value_ids: tuple[str, ...] = ()
    include_custom: bool = True
    min_group_trades: int = MIN_GROUP_TRADES
    permutations: int = DEFAULT_PERMUTATIONS
    #: Block length in **trading days**; converted to a trade count at run time
    #: because this study permutes trades, not a calendar.
    block_size_days: int = DEFAULT_BLOCK_SIZE
    alpha: float = DEFAULT_ALPHA
    batch_size: int = 100

    def normalized(self) -> FactorAttributionConfig:
        if not self.source_history_id.strip():
            raise ValueError("必须先选一条回测记录")
        if self.min_group_trades < 2:
            raise ValueError("每组最少成交笔数至少为 2")
        if self.permutations < 0:
            raise ValueError("噪声门槛检验次数不能为负数")
        if self.block_size_days < 1:
            raise ValueError("置换块长度至少为 1 个交易日")
        if not 0 < self.alpha < 1:
            raise ValueError("显著性水平必须介于 0 与 1 之间")
        if self.batch_size < 1:
            raise ValueError("分批读取的股票数必须大于 0")
        return FactorAttributionConfig(
            source_history_id=self.source_history_id.strip(),
            value_ids=tuple(dict.fromkeys(self.value_ids)),
            include_custom=bool(self.include_custom),
            min_group_trades=int(self.min_group_trades),
            permutations=int(self.permutations),
            block_size_days=int(self.block_size_days),
            alpha=float(self.alpha),
            batch_size=int(self.batch_size),
        )
