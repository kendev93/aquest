"""Unified catalog for formula signals and numeric technical outputs."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache

from selector_app.formulas.registry import FORMULA_REGISTRY, FormulaRegistry

_CONSTANT_VALUE_IDS = frozenset({"indicator_one.z", "indicator_two.warning_line"})
_SPARSE_EVENT_VALUE_IDS = frozenset(
    {
        # Rising/falling event lines are useful condition operands, but are
        # mostly zero and should not be offered as cross-sectional rank factors.
        "main_force_absorption.entry_33",
        "main_force_absorption.washout_33",
        "breakout_signal.breakthrough",
        "breakout_signal.breakdown",
    }
)
# Raw prices are valid condition operands (close 上穿 MA20) but never ranking
# features: an absolute price is not comparable across stocks, so mining a
# cross-stock threshold on it would be meaningless.
_PRICE_VALUE_IDS = frozenset({"price.open", "price.high", "price.low", "price.close"})
# Values quoted in currency units. They remain perfectly good condition
# operands, but ranking stocks on them is just ranking them by share price:
# a 200 CNY stock always beats a 3 CNY one regardless of momentum. The damage
# gets worse once ETFs share the universe, because ETF quotes cluster around
# 0.5-5 CNY while stocks span 3-300, so a cross-section sort degenerates into
# "every ETF beats every stock". MACD belongs here too: DIF/DEA are differences
# of two EMAs, so they inherit the price scale.
#
# This is an exclusion, not a verdict: trend deviation and volatility are still
# wanted, just not in currency units. Their dimensionless counterparts live in
# the same formula (``bias20``, ``atr_pct``, ``position20``) and are rankable,
# so dropping the raw MA/MACD outputs costs no information.
_PRICE_SCALE_VALUE_IDS = frozenset(
    {
        "technical.ma5",
        "technical.ma10",
        "technical.ma20",
        "technical.ma60",
        "technical.macd",
        "technical.macd_signal",
        "technical.macd_histogram",
    }
)
_NON_RANKABLE_VALUE_IDS = (
    _CONSTANT_VALUE_IDS | _SPARSE_EVENT_VALUE_IDS | _PRICE_VALUE_IDS | _PRICE_SCALE_VALUE_IDS
)


@dataclass(frozen=True)
class IndicatorDescriptor:
    id: str
    display_name: str
    rankable: bool = True


@dataclass(frozen=True)
class SignalDescriptor:
    id: str
    display_name: str
    description: str


@dataclass(frozen=True)
class IndicatorCatalog:
    values: tuple[IndicatorDescriptor, ...]
    signals: tuple[SignalDescriptor, ...]

    @property
    def numeric_ids(self) -> tuple[str, ...]:
        canonical = tuple(item.id for item in self.values)
        aliases = tuple(
            item.id.split(".", 1)[1] for item in self.values if item.id.startswith("technical.")
        )
        return tuple(dict.fromkeys((*canonical, *aliases)))

    @property
    def value_ids(self) -> tuple[str, ...]:
        """Canonical id of every numeric output, without ``numeric_ids`` aliases.

        Use this when the ids become storage or compute slots (snapshot columns,
        cache keys) so an aliased id cannot produce a duplicate slot.
        """

        return tuple(item.id for item in self.values)

    @property
    def rankable_ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.values if item.rankable)

    @property
    def signal_ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.signals)

    def has_value(self, value_id: str) -> bool:
        return self.canonical_id(value_id) in {item.id for item in self.values}

    def has_signal(self, signal_id: str) -> bool:
        return signal_id in self.signal_ids

    def display_name(self, value_id: str) -> str:
        value_id = self.canonical_id(value_id)
        for indicator in self.values:
            if indicator.id == value_id:
                return indicator.display_name
        for signal in self.signals:
            if signal.id == value_id:
                return signal.display_name
        return value_id

    @staticmethod
    def canonical_id(value_id: str) -> str:
        """Resolve a bare ``technical`` name such as ``ma5`` to its full id.

        The names are read from the registry rather than listed here so that
        adding an output cannot silently desync this from ``numeric_ids``,
        which derives its aliases the same way: a name accepted by one and not
        the other would validate as a condition operand but fail to resolve.
        """

        technical_names = FORMULA_REGISTRY.formula("technical").value_names
        return f"technical.{value_id}" if value_id in technical_names else value_id


@lru_cache(maxsize=4)
def default_indicator_catalog(registry: FormulaRegistry | None = None) -> IndicatorCatalog:
    selected_registry = registry or FORMULA_REGISTRY
    values: list[IndicatorDescriptor] = []
    for definition in selected_registry.metadata():
        raw_values = definition.get("values")
        if not isinstance(raw_values, list):
            continue
        for raw_value in raw_values:
            if not isinstance(raw_value, Mapping):
                continue
            value_id = str(raw_value["id"])
            values.append(
                IndicatorDescriptor(
                    id=value_id,
                    display_name=str(raw_value["display_name"]),
                    rankable=value_id not in _NON_RANKABLE_VALUE_IDS,
                )
            )
    signals = [
        SignalDescriptor(signal.id, signal.display_name, signal.description)
        for formula_id in selected_registry.all_formula_ids()
        for signal in selected_registry.formula(formula_id).signals
    ]
    return IndicatorCatalog(
        values=tuple(dict.fromkeys(values)),
        signals=tuple(dict.fromkeys(signals)),
    )


def numeric_display_names(value_ids: Iterable[str]) -> dict[str, str]:
    catalog = default_indicator_catalog()
    return {value_id: catalog.display_name(value_id) for value_id in value_ids}
