from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from selector_app.formulas.indicator_one import calculate_indicator_one
from selector_app.formulas.indicator_three import calculate_indicator_three
from selector_app.formulas.indicator_two import calculate_indicator_two
from selector_app.formulas.tdx_math import BARSLAST, COUNT, CROSS, EMA, HHV, LLV, REF, SMA


def make_bars(length: int = 180, *, descending: bool = False) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=length, freq="D")
    if descending:
        close = np.linspace(300.0, 20.0, length)
    else:
        close = 20.0 + np.sin(np.arange(length) / 7.0) * 2.0 + np.arange(length) * 0.04
    low = close - 1.0
    high = close + 1.0
    return pd.DataFrame(
        {
            "date": dates,
            "open": close - 0.2,
            "high": high,
            "low": low,
            "close": close,
            "volume": np.linspace(100_000.0, 200_000.0, length),
            "amount": close * np.linspace(100_000.0, 200_000.0, length),
        }
    )


def test_mytt_core_functions_have_expected_warmup_and_cross_semantics() -> None:
    values = np.arange(1.0, 8.0)

    assert np.isnan(REF(values, 1)[0])
    assert not np.isnan(SMA(values, 3, 1)[0])
    assert not np.isnan(EMA(values, 3)[0])
    assert np.isnan(LLV(values, 3)[1])
    assert np.isnan(HHV(values, 3)[1])
    assert CROSS(np.array([1.0, 1.0, 3.0]), np.array([2.0, 2.0, 2.0])).tolist() == [
        False,
        False,
        True,
    ]
    assert BARSLAST(np.array([False, False, True, False])).tolist() == [1, 2, 0, 1]
    assert COUNT(np.array([True, False, True, True]), 3)[-1] == 2


@pytest.mark.parametrize(
    ("calculator", "minimum_bars", "expected_signals"),
    [
        (
            calculate_indicator_one,
            33,
            {"main_force_entry", "wash", "main_force_raise", "distribution"},
        ),
        (
            calculate_indicator_two,
            34,
            {
                "start",
                "end",
                "saturation_hot",
                "new_high_breakout",
                "short_above_mid_long",
                "short_below_mid_long",
            },
        ),
        (
            calculate_indicator_three,
            34,
            {"prepare_rally", "suppress_main", "accumulation_zone", "begin_zone", "end_zone"},
        ),
    ],
)
def test_each_formula_returns_structured_intermediates_and_signals(
    calculator, minimum_bars: int, expected_signals: set[str]
) -> None:
    result = calculator(make_bars())

    assert result.minimum_bars == minimum_bars
    assert result.sufficient_data is True
    assert expected_signals.issubset(result.signals)
    assert expected_signals == set(result.last_signal_state)
    assert len(result.values) >= 5
    assert result.last_indicator_values
    assert all(len(array) == 180 for array in result.values.values())


def test_formula_results_mark_empty_and_insufficient_data_as_skipped() -> None:
    empty = make_bars(0)
    short = make_bars(10)

    for calculator in (calculate_indicator_one, calculate_indicator_two, calculate_indicator_three):
        empty_result = calculator(empty)
        short_result = calculator(short)
        assert empty_result.sufficient_data is False
        assert short_result.sufficient_data is False
        assert all(value is False for value in empty_result.last_signal_state.values())
        assert all(value is False for value in short_result.last_signal_state.values())


def test_constant_price_data_does_not_produce_infinite_values() -> None:
    bars = make_bars()
    for column in ("open", "high", "low", "close"):
        bars[column] = 10.0

    for calculator in (calculate_indicator_one, calculate_indicator_two, calculate_indicator_three):
        result = calculator(bars)
        assert all(not np.isinf(array).any() for array in result.values.values())


def test_indicator_one_keeps_the_negative_min_denominator() -> None:
    result = calculate_indicator_one(make_bars(descending=True))
    denominator = result.values["var21_denominator"]
    var21 = result.values["var21"]

    negative_indexes = np.flatnonzero(denominator < 0)
    assert negative_indexes.size > 0
    finite_negative_indexes = [i for i in negative_indexes if np.isfinite(var21[i])]
    assert finite_negative_indexes
    assert var21[finite_negative_indexes[-1]] < 0


def test_indicator_two_keeps_the_explicit_ref_twenty_gap() -> None:
    bars = make_bars()
    result = calculate_indicator_two(bars)
    a = result.values["a"]
    expected = np.zeros(len(a))
    for index in range(len(a)):
        if index < 20:
            expected[index] = np.nan
            continue
        expected[index] = sum((20 - offset) * a[index - offset] for offset in range(19))
        expected[index] += a[index - 20]
        expected[index] /= 210

    assert np.allclose(result.values["x"][20:], expected[20:], equal_nan=True)
    with_ref_19 = result.values["x"] + 19 * REF(a, 19)
    assert not np.allclose(result.values["x"][30:], with_ref_19[30:], equal_nan=True)


def test_indicator_three_uses_fractional_sma_period_3_2() -> None:
    bars = make_bars()
    result = calculate_indicator_three(bars)
    base = result.values["stochastic_base"]
    expected_inner = SMA(base, 5, 1)
    expected = 4 * expected_inner - 3 * SMA(expected_inner, 3.2, 1)

    assert np.allclose(result.values["var1"], expected, equal_nan=True)


def test_screenshot_named_outputs_are_present() -> None:
    one = calculate_indicator_one(make_bars())
    two = calculate_indicator_two(make_bars())

    assert np.all(one.values["z"] == 0)
    assert all(name in two.values for name in ("short_line", "mid_line", "bottom_line"))
    assert np.all(two.values["warning_line"] == 85)


def test_formulas_do_not_use_future_rows() -> None:
    original = make_bars()
    changed = original.copy(deep=True)
    changed.loc[len(changed) - 1, ["open", "high", "low", "close", "amount"]] = [
        999.0,
        1000.0,
        998.0,
        999.5,
        999_500_000.0,
    ]

    for calculator in (calculate_indicator_one, calculate_indicator_two, calculate_indicator_three):
        before = calculator(original)
        after = calculator(changed)
        for name in before.values:
            assert np.allclose(before.values[name][:-1], after.values[name][:-1], equal_nan=True)
