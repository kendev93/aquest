from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from aquest.formulas.common import safe_divide, validate_market_data


def test_safe_divide_returns_nan_for_zero_denominator() -> None:
    result = safe_divide(np.array([2.0, 0.0, 4.0]), np.array([1.0, 0.0, 2.0]))

    assert result[0] == 2.0
    assert np.isnan(result[1])
    assert result[2] == 2.0


def test_safe_divide_preserves_negative_denominator() -> None:
    result = safe_divide(np.array([4.0]), np.array([-2.0]))

    assert result.tolist() == [-2.0]


def test_safe_divide_handles_empty_arrays() -> None:
    result = safe_divide(np.array([], dtype=float), np.array([], dtype=float))

    assert result.size == 0


def test_validate_market_data_rejects_non_finite_or_non_positive_prices() -> None:
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-01-01"]),
            "open": [10.0],
            "high": [10.5],
            "low": [9.5],
            "close": [np.inf],
            "volume": [100.0],
            "amount": [1_000.0],
        }
    )

    with pytest.raises(ValueError, match="非有限"):
        validate_market_data(frame)

    frame["close"] = 0.0
    with pytest.raises(ValueError, match="非正"):
        validate_market_data(frame)

    with pytest.raises(ValueError, match="缺少字段: date"):
        validate_market_data(frame.drop(columns=["date"]))
