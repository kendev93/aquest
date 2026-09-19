"""Which custom-formula outputs the swing study is allowed to score.

The three TDX formulas (主力清洗 / 庄家顶底 / 准备拉升) are trading formulas,
not measurement libraries: most of their named outputs are either the formula's
own plumbing or an on/off signal. Scoring them as if they were market factors
is how a study ends up reporting nonsense, so the study takes an explicit
allow-list instead of "everything the formula happens to expose".

Measured on 160 stocks x 722 trading days (2023-09 ~ 2026-09), one
cross-section per day. Three failure modes showed up, and each has a number:

- **Constants.** ``indicator_one.z`` is literally ``zeros``; ``indicator_three.n``
  is 5 and ``var2`` is 8 for every stock on every day. Their median daily mode
  share is 1.00 - the whole market ties, so a rank is meaningless.
- **Pulses.** ``prepare_rally`` is ``IF(CROSS(VAR1,8),80,0)``: 98% of the market
  sits on the same value on any given day, and ``suppress_main`` (91%) and
  ``accumulation_zone`` (93%) are the same story. A rank over 2% ones and 98%
  zeros is a coin flip dressed up as a correlation.
- **Price in disguise.** ``indicator_two.short_cost`` is ``EMA(close,17)`` and
  ``indicator_three.varo5`` is ``LLV(low,27)``: their cross-sectional rank
  correlates 0.99 with the rank of the closing price. Scoring those does not
  find a factor, it re-discovers that some stocks cost more than others.

What survives is the *continuous, dimensionless* part of each formula - the
stochastic position, the smoothed pressure, the saturation - which is exactly
what the technical family already trades in.
"""

from __future__ import annotations

from selector_app.formulas.registry import FORMULA_REGISTRY
from selector_app.strategy_discovery.catalog import IndicatorCatalog

#: Continuous, dimensionless outputs of the three custom formulas, each measured
#: to be rankable: daily mode share <= 0.7, unique-value ratio >= 2%, and
#: |rank correlation with the closing price| <= 0.8.
CUSTOM_VALUE_IDS: tuple[str, ...] = (
    # 主力清洗
    "indicator_one.main_force_entry",
    "indicator_one.wash",
    "indicator_one.distribution",
    "indicator_one.var3",
    # 庄家顶底
    "indicator_two.short_line",
    "indicator_two.mid_line",
    "indicator_two.bottom_line",
    "indicator_two.saturation",
    "indicator_two.holding",
    # 准备拉升
    "indicator_three.var1",
    "indicator_three.varo7",
    "indicator_three.stochastic_base",
)

#: Cross-section-friendly outputs keep the formula's own display name; the rest
#: are internal levers and are shown as "公式名 · 输出名" so their origin is
#: obvious in a table full of technical indicators.
CUSTOM_DISPLAY_NAMES: dict[str, str] = {
    "indicator_one.main_force_entry": "主力清洗 · 主力进场",
    "indicator_one.wash": "主力清洗 · 洗盘",
    "indicator_one.distribution": "主力清洗 · 出货",
    "indicator_one.var3": "主力清洗 · 低位平滑值",
    "indicator_two.short_line": "庄家顶底 · 短期线",
    "indicator_two.mid_line": "庄家顶底 · 中期线",
    "indicator_two.bottom_line": "庄家顶底 · 底部线",
    "indicator_two.saturation": "庄家顶底 · 饱和度",
    "indicator_two.holding": "庄家顶底 · 持仓",
    "indicator_three.var1": "准备拉升 · VAR1",
    "indicator_three.varo7": "准备拉升 · 建仓强度",
    "indicator_three.stochastic_base": "准备拉升 · 随机强度",
}


def custom_value_ids() -> tuple[str, ...]:
    """Allow-listed custom outputs that the formulas actually expose."""

    return tuple(value_id for value_id in CUSTOM_VALUE_IDS if FORMULA_REGISTRY.has_value(value_id))


def display_name_for(value_id: str, catalog: IndicatorCatalog) -> str:
    """Name to show in the study table.

    Custom outputs win over the catalog: the catalog names them for the formula
    screen, where the formula is already known from context, and a study table
    mixes all four formulas in one column.
    """

    custom = CUSTOM_DISPLAY_NAMES.get(value_id)
    if custom:
        return custom
    return catalog.display_name(value_id)


def formula_groups(value_ids: tuple[str, ...]) -> dict[str, list[tuple[int, str]]]:
    """Split ``formula.output`` ids into per-formula panels of column indexes.

    One formula is computed once per stock no matter how many of its outputs the
    study asked for, and the results land in the columns that value belongs to.
    """

    groups: dict[str, list[tuple[int, str]]] = {}
    for index, value_id in enumerate(value_ids):
        formula_id, _, name = value_id.partition(".")
        groups.setdefault(formula_id, []).append((index, name))
    return groups


__all__ = [
    "CUSTOM_DISPLAY_NAMES",
    "CUSTOM_VALUE_IDS",
    "custom_value_ids",
    "display_name_for",
    "formula_groups",
]
