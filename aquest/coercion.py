"""Shared coercions for decoded JSON and report fields."""

from __future__ import annotations

from collections.abc import Mapping
from math import isfinite


def as_mapping(value: object) -> Mapping[str, object]:
    """Return the value when it is a mapping, otherwise an empty mapping."""

    return value if isinstance(value, Mapping) else {}


def as_finite_float(value: object) -> float | None:
    """Return a finite float for numeric input; reject bools, text and NaN/inf."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if isfinite(number) else None
