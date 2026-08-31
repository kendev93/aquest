"""Stable metadata and dispatch for formula/signal identifiers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pandas as pd

from .indicator_one import (
    DISPLAY_NAME as ONE_DISPLAY_NAME,
)
from .indicator_one import (
    MINIMUM_BARS as ONE_MINIMUM_BARS,
)
from .indicator_one import (
    RECOMMENDED_BARS as ONE_RECOMMENDED_BARS,
)
from .indicator_one import (
    calculate_indicator_one,
)
from .indicator_three import (
    DISPLAY_NAME as THREE_DISPLAY_NAME,
)
from .indicator_three import (
    MINIMUM_BARS as THREE_MINIMUM_BARS,
)
from .indicator_three import (
    RECOMMENDED_BARS as THREE_RECOMMENDED_BARS,
)
from .indicator_three import (
    calculate_indicator_three,
)
from .indicator_two import (
    DISPLAY_NAME as TWO_DISPLAY_NAME,
)
from .indicator_two import (
    MINIMUM_BARS as TWO_MINIMUM_BARS,
)
from .indicator_two import (
    RECOMMENDED_BARS as TWO_RECOMMENDED_BARS,
)
from .indicator_two import (
    calculate_indicator_two,
)
from .types import FormulaResult

FormulaCalculator = Callable[[pd.DataFrame], FormulaResult]


@dataclass(frozen=True)
class SignalDefinition:
    id: str
    display_name: str
    description: str
    formula_id: str


@dataclass(frozen=True)
class FormulaDefinition:
    id: str
    display_name: str
    minimum_bars: int
    recommended_bars: int
    calculate: FormulaCalculator
    signals: tuple[SignalDefinition, ...]
    value_names: tuple[str, ...]


class FormulaRegistry:
    def __init__(self, definitions: tuple[FormulaDefinition, ...]) -> None:
        self._definitions = definitions
        self._by_id = {definition.id: definition for definition in definitions}
        self._signals = {
            signal.id: signal for definition in definitions for signal in definition.signals
        }

    def formula(self, formula_id: str) -> FormulaDefinition:
        return self._by_id[formula_id]

    def signal(self, signal_id: str) -> SignalDefinition:
        return self._signals[signal_id]

    def has_signal(self, signal_id: str) -> bool:
        return signal_id in self._signals

    def has_value(self, value_id: str) -> bool:
        formula_id, separator, value_name = value_id.partition(".")
        return bool(
            separator
            and formula_id in self._by_id
            and value_name in self._by_id[formula_id].value_names
        )

    def formulas_for_signals(self, signal_ids: tuple[str, ...]) -> tuple[FormulaDefinition, ...]:
        formula_ids = {self.signal(signal_id).formula_id for signal_id in signal_ids}
        return tuple(definition for definition in self._definitions if definition.id in formula_ids)

    def formulas_for_values(self, value_ids: tuple[str, ...]) -> tuple[FormulaDefinition, ...]:
        formula_ids = {value_id.split(".", 1)[0] for value_id in value_ids}
        return tuple(definition for definition in self._definitions if definition.id in formula_ids)

    def metadata(self) -> list[dict[str, object]]:
        return [
            {
                "id": definition.id,
                "display_name": definition.display_name,
                "minimum_bars": definition.minimum_bars,
                "recommended_bars": definition.recommended_bars,
                "signals": [
                    {
                        "id": signal.id,
                        "display_name": signal.display_name,
                        "description": signal.description,
                    }
                    for signal in definition.signals
                ],
                "values": [
                    {
                        "id": f"{definition.id}.{value_name}",
                        "display_name": value_name,
                        "description": f"{value_name} 的数值输出",
                    }
                    for value_name in definition.value_names
                ],
            }
            for definition in self._definitions
        ]

    def all_signal_ids(self) -> tuple[str, ...]:
        return tuple(self._signals)


FORMULA_REGISTRY = FormulaRegistry(
    (
        FormulaDefinition(
            id="indicator_one",
            display_name=ONE_DISPLAY_NAME,
            minimum_bars=ONE_MINIMUM_BARS,
            recommended_bars=ONE_RECOMMENDED_BARS,
            calculate=calculate_indicator_one,
            signals=(
                SignalDefinition(
                    "indicator_one.main_force_entry", "主力进场", "VAR5 上升", "indicator_one"
                ),
                SignalDefinition("indicator_one.wash", "洗盘", "VAR5 下降", "indicator_one"),
                SignalDefinition(
                    "indicator_one.main_force_raise", "主力拉高", "VAR51 下降", "indicator_one"
                ),
                SignalDefinition(
                    "indicator_one.distribution", "出货", "VAR51 上升", "indicator_one"
                ),
            ),
            value_names=(
                "var1",
                "var2_numerator",
                "var2_denominator",
                "var2",
                "var3",
                "var4",
                "var5",
                "main_force_entry",
                "wash",
                "var21_denominator_input",
                "var21_numerator",
                "var21_denominator",
                "var21",
                "var31",
                "var41",
                "var51",
                "main_force_raise",
                "distribution",
            ),
        ),
        FormulaDefinition(
            id="indicator_two",
            display_name=TWO_DISPLAY_NAME,
            minimum_bars=TWO_MINIMUM_BARS,
            recommended_bars=TWO_RECOMMENDED_BARS,
            calculate=calculate_indicator_two,
            signals=(
                SignalDefinition(
                    "indicator_two.start", "始 · 金叉", "短期线上穿中期线", "indicator_two"
                ),
                SignalDefinition("indicator_two.end", "终", "短期线大于 90", "indicator_two"),
                SignalDefinition(
                    "indicator_two.saturation_hot",
                    "高饱和",
                    "饱和度 ≥ 97 且 CQ > 90",
                    "indicator_two",
                ),
                SignalDefinition(
                    "indicator_two.new_high_breakout", "新高突破", "5 日内首次突破", "indicator_two"
                ),
                SignalDefinition(
                    "indicator_two.short_above_mid_long",
                    "短高于中长",
                    "成本线持续强势",
                    "indicator_two",
                ),
                SignalDefinition(
                    "indicator_two.short_below_mid_long",
                    "短低于中长",
                    "成本线持续弱势",
                    "indicator_two",
                ),
            ),
            value_names=(
                "short_cost",
                "a",
                "x",
                "mid_cost",
                "var1",
                "var2",
                "var3",
                "n1",
                "n4",
                "cq",
                "mid_term",
                "sat",
                "saturation",
                "w1",
                "w2",
                "w3",
                "w4",
                "holding_base",
                "holding",
                "support",
                "short_line",
                "mid_line",
                "start",
                "end",
            ),
        ),
        FormulaDefinition(
            id="indicator_three",
            display_name=THREE_DISPLAY_NAME,
            minimum_bars=THREE_MINIMUM_BARS,
            recommended_bars=THREE_RECOMMENDED_BARS,
            calculate=calculate_indicator_three,
            signals=(
                SignalDefinition(
                    "indicator_three.prepare_rally", "准备拉升", "VAR1 上穿 8", "indicator_three"
                ),
                SignalDefinition(
                    "indicator_three.suppress_main", "压住庄家", "VAR1 ≤ 8", "indicator_three"
                ),
                SignalDefinition(
                    "indicator_three.accumulation_zone", "建仓区", "VARO7 < 10", "indicator_three"
                ),
                SignalDefinition(
                    "indicator_three.begin_zone", "始", "VAR1 < 10", "indicator_three"
                ),
                SignalDefinition("indicator_three.end_zone", "终", "VAR1 > 90", "indicator_three"),
            ),
            value_names=(
                "n",
                "stochastic_base",
                "sma_inner",
                "var1",
                "var2",
                "varo5",
                "varo6",
                "varo7",
                "prepare_rally",
                "suppress_main",
                "accumulation_zone",
            ),
        ),
    )
)
