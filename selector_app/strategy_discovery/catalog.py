"""Unified catalog for formula signals and numeric technical outputs."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from selector_app.formulas.registry import FORMULA_REGISTRY, FormulaRegistry

_CONSTANT_VALUE_IDS = frozenset({"indicator_one.z", "indicator_two.warning_line"})


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
        return (
            f"technical.{value_id}"
            if value_id
            in {
                "ma5",
                "ma10",
                "ma20",
                "ma60",
                "rsi14",
                "macd",
                "macd_signal",
                "macd_histogram",
            }
            else value_id
        )


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
                    rankable=value_id not in _CONSTANT_VALUE_IDS,
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
