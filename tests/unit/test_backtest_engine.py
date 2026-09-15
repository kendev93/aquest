from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from selector_app.backtest.engine import SignalBacktestConfig, run_signal_backtest
from selector_app.backtest.performance import PerformanceAnalyzer


def _frame() -> pd.DataFrame:
    close = np.array([10.0, 11.0, 12.0, 13.0, 14.0, 13.0])
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=len(close), freq="D"),
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": 1000.0,
            "amount": close * 1000,
        }
    )


def test_signal_engine_executes_on_next_bar_and_tracks_full_position() -> None:
    result = run_signal_backtest(
        _frame(),
        np.array([False, True, False, False, False, False]),
        np.array([False, False, False, False, True, False]),
        SignalBacktestConfig(initial_cash=10_000, commission=0, min_commission=0, stamp_tax=0),
    )

    assert result.trades["direction"].tolist() == ["BUY", "SELL"]
    assert result.trades["datetime"].dt.strftime("%Y-%m-%d").tolist() == [
        "2024-01-03",
        "2024-01-06",
    ]
    assert result.performance["end_value"] == 10_800.0
    assert result.equity_curve.iloc[0]["date"] == "2024-01-01"


def test_signal_engine_supports_fixed_size_and_date_window() -> None:
    result = run_signal_backtest(
        _frame(),
        np.array([False, True, False, False, False, False]),
        np.array([False, False, False, False, True, False]),
        SignalBacktestConfig(
            initial_cash=10_000,
            commission=0,
            min_commission=0,
            stamp_tax=0,
            position_mode="fixed",
            fixed_size=200,
            lot_size=100,
            start_date=date(2024, 1, 2),
            end_date=date(2024, 1, 5),
        ),
    )

    assert result.trades.iloc[0]["size"] == 200
    assert len(result.equity_curve) == 4


@pytest.mark.parametrize(
    "total",
    [
        [10_000.0],
        [10_000.0, 10_000.0, 10_000.0],
        [10_000.0, 11_000.0, 12_000.0],
        [12_000.0, 11_000.0, 10_000.0],
        [10_000.0, 12_000.0, 9_000.0, 11_000.0, 8_000.0, 13_000.0],
        [13_000.0, 12_000.0, 12_500.0, 9_000.0],
        [10_000.0, 10_500.0, 10_200.0, 10_800.0, 10_400.0],
        [],
    ],
)
def test_drawdown_helpers_match_the_reference_implementation(total: list[float]) -> None:
    """回归：回撤序列与最长回撤期必须与旧的 DataFrame/逐格回扫写法一致。"""

    values = np.asarray(total, dtype=float)

    drawdown, drawdown_pct = PerformanceAnalyzer._drawdown_arrays(values)

    peak = np.maximum.accumulate(values)
    expected_drawdown = peak - values
    expected_pct = np.divide(
        expected_drawdown, peak, out=np.zeros_like(expected_drawdown), where=peak != 0
    )
    assert np.array_equal(drawdown, expected_drawdown)
    assert np.array_equal(drawdown_pct, expected_pct)

    if not len(expected_drawdown) or float(np.max(expected_drawdown)) == 0:
        expected_duration = 0
    else:
        max_index = int(np.argmax(expected_drawdown))
        peak_index = max_index
        for index in range(max_index - 1, -1, -1):
            if values[index] > values[max_index]:
                peak_index = index
                break
        expected_duration = max_index - peak_index

    assert PerformanceAnalyzer._max_drawdown_duration(values, drawdown) == expected_duration
