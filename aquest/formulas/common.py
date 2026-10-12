"""Shared, side-effect-free helpers for the three formula modules."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

import numpy as np
import pandas as pd

from .tdx_math import MYTT
from .types import FormulaResult

REQUIRED_BAR_COLUMNS = ("open", "high", "low", "close", "volume", "amount")
_PRICE_COLUMNS = ("open", "high", "low", "close")

__all__ = [
    "MYTT",
    "REQUIRED_BAR_COLUMNS",
    "build_formula_result",
    "dynamic_ref",
    "empty_formula_result",
    "numeric_column",
    "safe_divide",
    "validate_formula_frame",
    "validate_market_data",
]


def safe_divide(numerator: object, denominator: object) -> np.ndarray:
    """Divide element-wise and return NaN where a denominator is zero.

    A negative denominator remains negative.  This matters for indicator one,
    whose ``MIN(HIGH-VAR1, 0)`` denominator is intentionally not an absolute
    value.  Returning NaN for zero avoids inf values poisoning a whole scan.
    """

    top = np.asarray(numerator, dtype=float)
    bottom = np.asarray(denominator, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        result = np.divide(top, bottom)
    return np.asarray(np.where(bottom == 0, np.nan, result), dtype=float)


def numeric_column(frame: pd.DataFrame, name: str) -> np.ndarray:
    if name not in frame.columns:
        raise ValueError(f"行情数据缺少字段: {name}")
    return cast(np.ndarray, frame[name].to_numpy(dtype=float, copy=True))


def validate_formula_frame(frame: pd.DataFrame) -> None:
    missing = [name for name in REQUIRED_BAR_COLUMNS if name not in frame.columns]
    if missing:
        raise ValueError(f"行情数据缺少字段: {', '.join(missing)}")


def validate_market_data(frame: pd.DataFrame) -> None:
    """Reject malformed OHLCV values before they reach trading simulation."""

    if "date" not in frame.columns:
        raise ValueError("行情数据缺少字段: date")
    validate_formula_frame(frame)
    try:
        numeric = frame[list(REQUIRED_BAR_COLUMNS)].to_numpy(dtype=float, copy=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("行情数据包含非数字字段") from exc
    if not np.isfinite(numeric).all():
        raise ValueError("行情数据包含非有限数值")
    if (numeric[:, [REQUIRED_BAR_COLUMNS.index(name) for name in _PRICE_COLUMNS]] <= 0).any():
        raise ValueError("行情数据包含非正价格")


def dynamic_ref(values: np.ndarray, offsets: np.ndarray) -> np.ndarray:
    """Implement TDX ``REF(S, dynamic_period)`` without looking ahead."""

    source = np.asarray(values, dtype=float)
    periods = np.asarray(offsets, dtype=float)
    result = np.full(len(source), np.nan, dtype=float)
    for index, period in enumerate(periods):
        if np.isfinite(period):
            source_index = index - int(period)
            if 0 <= source_index <= index:
                result[index] = source[source_index]
    return result


def empty_formula_result(
    *,
    formula_id: str,
    display_name: str,
    minimum_bars: int,
    recommended_bars: int,
    signal_names: tuple[str, ...],
    data_length: int = 0,
) -> FormulaResult:
    return FormulaResult.build(
        formula_id=formula_id,
        display_name=display_name,
        minimum_bars=minimum_bars,
        recommended_bars=recommended_bars,
        data_length=data_length,
        values={},
        signals={name: np.array([], dtype=bool) for name in signal_names},
    )


def build_formula_result(
    *,
    formula_id: str,
    display_name: str,
    minimum_bars: int,
    recommended_bars: int,
    frame: pd.DataFrame,
    values: Mapping[str, object],
    signals: Mapping[str, object],
) -> FormulaResult:
    validate_formula_frame(frame)
    return FormulaResult.build(
        formula_id=formula_id,
        display_name=display_name,
        minimum_bars=minimum_bars,
        recommended_bars=recommended_bars,
        data_length=len(frame),
        values=values,
        signals=signals,
    )
