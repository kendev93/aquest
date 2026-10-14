from __future__ import annotations

import numpy as np

from aquest.formulas.tdx_math import BARSLAST, COUNT, CROSS, EMA, HHV, LLV, MA, REF, SMA, SUM


def test_tdx_math_preserves_warmup_and_cross_semantics() -> None:
    values = np.arange(1.0, 8.0)

    assert np.isnan(REF(values, 1)[0])
    assert not np.isnan(SMA(values, 3, 1)[0])
    assert not np.isnan(EMA(values, 3)[0])
    assert np.isnan(LLV(values, 3)[1])
    assert np.isnan(HHV(values, 3)[1])
    assert np.isnan(MA(values, 3)[1])
    assert np.isnan(SUM(values, 3)[1])
    assert CROSS(np.array([1.0, 1.0, 3.0]), np.array([2.0, 2.0, 2.0])).tolist() == [
        False,
        False,
        True,
    ]
    bars_last = BARSLAST(np.array([False, False, True, False]))
    assert np.isnan(bars_last[:2]).tolist() == [True, True]
    assert bars_last[2:].tolist() == [0.0, 1.0]
    assert COUNT(np.array([True, False, True, True]), 3)[-1] == 2


def test_fractional_sma_period_is_supported() -> None:
    values = np.array([1.0, 2.0, 4.0, 8.0])

    result = SMA(values, 3.2, 1)

    assert result.shape == values.shape
    assert np.all(np.isfinite(result))


def test_ref_zero_returns_a_copy_and_empty_inputs_are_safe() -> None:
    values = np.array([1.0, 2.0])
    result = REF(values, 0)

    assert np.array_equal(result, values)
    assert result is not values
    assert BARSLAST(np.array([], dtype=bool)).size == 0
