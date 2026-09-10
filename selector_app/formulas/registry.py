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
from .technical import (
    DISPLAY_NAME as TECHNICAL_DISPLAY_NAME,
)
from .technical import (
    MINIMUM_BARS as TECHNICAL_MINIMUM_BARS,
)
from .technical import (
    RECOMMENDED_BARS as TECHNICAL_RECOMMENDED_BARS,
)
from .technical import (
    VALUE_NAMES as TECHNICAL_VALUE_NAMES,
)
from .technical import (
    calculate_technical_indicators,
)
from .types import FormulaResult

FormulaCalculator = Callable[[pd.DataFrame], FormulaResult]

_VALUE_DISPLAY_NAMES: dict[str, dict[str, str]] = {
    "indicator_one": {
        "var1": "前一日均价",
        "var2_numerator": "低位偏离分子",
        "var2_denominator": "低位下行分母",
        "var2": "低位偏离比",
        "var3": "低位平滑值",
        "var4": "33日最低价",
        "var5": "主力进场强度",
        "main_force_entry": "主力进场",
        "wash": "洗盘",
        "var21_denominator_input": "高位偏离输入",
        "var21_numerator": "高位偏离分子",
        "var21_denominator": "高位下行分母",
        "var21": "高位偏离比",
        "var31": "高位平滑值",
        "var41": "33日最高价",
        "var51": "主力拉高强度",
        "main_force_raise": "主力拉高",
        "distribution": "出货",
        "z": "Z",
    },
    "indicator_two": {
        "short_cost": "短期成本线",
        "a": "加权价格",
        "x": "加权成本输入",
        "mid_cost": "中期成本线",
        "var1": "常数基线",
        "var2": "10日最低价",
        "var3": "25日最高价",
        "n1": "短期周期",
        "n4": "长期周期",
        "cq": "趋势强度",
        "mid_term": "中期趋势",
        "sat": "饱和度原值",
        "saturation": "饱和度",
        "w1": "20日新高标记",
        "w2": "距新高天数",
        "w3": "新高参考价",
        "w4": "新高突破标记",
        "holding_base": "持仓位置",
        "holding": "持仓强度",
        "support": "支撑强度",
        "bottom_line": "底部线",
        "short_line": "短期线",
        "mid_line": "中期线",
        "start": "始信号值",
        "end": "终信号值",
        "warning_line": "警戒线",
    },
    "indicator_three": {
        "n": "随机周期",
        "stochastic_base": "随机强度",
        "sma_inner": "随机平滑值",
        "var1": "VAR1",
        "var2": "VAR2",
        "varo5": "27日最低价",
        "varo6": "34日最高价",
        "varo7": "建仓强度",
        "prepare_rally": "准备拉升",
        "suppress_main": "压住庄家",
        "accumulation_zone": "建仓区值",
    },
    "technical": {
        "ma5": "MA5",
        "ma10": "MA10",
        "ma20": "MA20",
        "ma60": "MA60",
        "rsi14": "RSI14",
        "macd": "MACD",
        "macd_signal": "SIGNAL",
        "macd_histogram": "MACD柱",
    },
}


def value_display_name(formula_id: str, value_name: str) -> str:
    """Return a user-facing name while retaining raw names as a fallback."""

    return _VALUE_DISPLAY_NAMES.get(formula_id, {}).get(value_name, value_name.upper())


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
    # Bump this whenever the formula's numerical semantics change. Cached
    # values are derived data and must never survive a semantic formula change.
    cache_version: str = "1"
    screen_value_names: tuple[str, ...] = ()

    @property
    def selectable_value_names(self) -> tuple[str, ...]:
        return self.screen_value_names or self.value_names


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
                        "display_name": value_display_name(definition.id, value_name),
                        "description": (
                            f"{value_display_name(definition.id, value_name)}"
                            f"（{value_name.upper()}）的数值输出"
                        ),
                    }
                    for value_name in definition.selectable_value_names
                ],
            }
            for definition in self._definitions
        ]

    def all_signal_ids(self) -> tuple[str, ...]:
        return tuple(self._signals)

    def all_formula_ids(self) -> tuple[str, ...]:
        return tuple(definition.id for definition in self._definitions)


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
                "z",
            ),
            screen_value_names=(
                "wash",
                "main_force_raise",
                "distribution",
                "main_force_entry",
                "z",
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
                "bottom_line",
                "short_line",
                "mid_line",
                "warning_line",
                "start",
                "end",
            ),
            screen_value_names=(
                "short_line",
                "mid_line",
                "bottom_line",
                "warning_line",
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
            screen_value_names=(
                "var1",
                "var2",
                "prepare_rally",
                "suppress_main",
            ),
        ),
        FormulaDefinition(
            id="technical",
            display_name=TECHNICAL_DISPLAY_NAME,
            minimum_bars=TECHNICAL_MINIMUM_BARS,
            recommended_bars=TECHNICAL_RECOMMENDED_BARS,
            calculate=calculate_technical_indicators,
            signals=(),
            value_names=TECHNICAL_VALUE_NAMES,
            screen_value_names=TECHNICAL_VALUE_NAMES,
        ),
    )
)
