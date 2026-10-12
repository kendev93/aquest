"""Immutable result types for formula calculations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

import numpy as np


def readonly_array(values: object, *, dtype: str | type | np.dtype | None = None) -> np.ndarray:
    array = np.array(values, dtype=dtype, copy=True)
    array.setflags(write=False)
    return array


@dataclass(frozen=True)
class FormulaResult:
    """All intermediate arrays, named outputs, and latest signal state."""

    formula_id: str
    display_name: str
    minimum_bars: int
    recommended_bars: int
    data_length: int
    sufficient_data: bool
    values: Mapping[str, np.ndarray]
    signals: Mapping[str, np.ndarray]
    last_signal_state: Mapping[str, bool]
    last_indicator_values: Mapping[str, float | None]

    @classmethod
    def build(
        cls,
        *,
        formula_id: str,
        display_name: str,
        minimum_bars: int,
        recommended_bars: int,
        data_length: int,
        values: Mapping[str, object],
        signals: Mapping[str, object],
    ) -> FormulaResult:
        readonly_values = {
            name: readonly_array(value, dtype=float) for name, value in values.items()
        }
        readonly_signals = {
            name: readonly_array(value, dtype=bool) for name, value in signals.items()
        }
        sufficient = data_length >= minimum_bars
        last_signal_state = {
            name: bool(array[-1]) if sufficient and len(array) else False
            for name, array in readonly_signals.items()
        }
        last_values: dict[str, float | None] = {}
        for name, array in readonly_values.items():
            if not len(array) or not np.isfinite(array[-1]):
                last_values[name] = None
            else:
                last_values[name] = float(array[-1])
        return cls(
            formula_id=formula_id,
            display_name=display_name,
            minimum_bars=minimum_bars,
            recommended_bars=recommended_bars,
            data_length=data_length,
            sufficient_data=sufficient,
            values=MappingProxyType(readonly_values),
            signals=MappingProxyType(readonly_signals),
            last_signal_state=MappingProxyType(last_signal_state),
            last_indicator_values=MappingProxyType(last_values),
        )
