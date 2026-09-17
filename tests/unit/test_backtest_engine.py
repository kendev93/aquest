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
        # 峰值取谷底之前的历史最高点，而不是回扫遇到的第一个高于谷底的点。
        peak_index = int(np.argmax(values[: max_index + 1]))
        expected_duration = max_index - peak_index

    assert PerformanceAnalyzer._max_drawdown_duration(values, drawdown) == expected_duration


@pytest.mark.parametrize(
    ("total", "expected"),
    [
        # 单调下跌：回撤从首个峰值一直持续到谷底，长度为 n-1 而不是 1。
        ([100.0, 90.0, 80.0, 70.0], 3),
        # 谷底前出现小反弹：峰值仍是起点，不能被反弹点截断。
        ([100.0, 90.0, 95.0, 80.0], 3),
        # 无回撤。
        ([100.0, 110.0, 120.0], 0),
    ],
)
def test_max_drawdown_duration_starts_at_the_historical_peak(
    total: list[float], expected: int
) -> None:
    """回归：回撤期必须从谷底之前的历史最高点起算。

    旧实现回扫时停在「第一个高于谷底的点」，单调下跌恒为 1 天，严重低估回撤持续期。
    """

    values = np.asarray(total, dtype=float)
    drawdown, _ = PerformanceAnalyzer._drawdown_arrays(values)
    assert PerformanceAnalyzer._max_drawdown_duration(values, drawdown) == expected


def test_close_at_end_closes_the_open_position_on_the_last_bar() -> None:
    """没有卖出信号时，开启「持有到期末」也要在最后一根收盘价平掉仓位。"""

    result = run_signal_backtest(
        _frame(),
        np.array([False, True, False, False, False, False]),
        np.zeros(6, dtype=bool),
        SignalBacktestConfig(
            initial_cash=10_000, commission=0, min_commission=0, stamp_tax=0, close_at_end=True
        ),
    )

    assert result.trades["direction"].tolist() == ["BUY", "SELL"]
    # 买入在 01-03（信号 01-02 的次日开盘），平仓落在最后一根 01-06。
    assert result.trades["datetime"].dt.strftime("%Y-%m-%d").tolist() == [
        "2024-01-03",
        "2024-01-06",
    ]
    # 01-03 以 12.0 买入 800 股（9600），期末按最后一根 close 13.0 平仓 → 10400 + 400 现金。
    assert result.trades.iloc[0]["price"] == 12.0
    assert result.trades.iloc[1]["price"] == 13.0
    assert result.trades.iloc[1]["size"] == 800
    assert result.performance["end_value"] == pytest.approx(10_800.0)


def test_close_at_end_is_off_by_default() -> None:
    """默认行为不变：没卖出的持仓不产生交易记录。"""

    result = run_signal_backtest(
        _frame(),
        np.array([False, True, False, False, False, False]),
        np.zeros(6, dtype=bool),
        SignalBacktestConfig(initial_cash=10_000, commission=0, min_commission=0, stamp_tax=0),
    )

    assert result.trades["direction"].tolist() == ["BUY"]


def test_close_at_end_charges_the_same_costs_as_a_normal_sell() -> None:
    """收官平仓要照常扣佣金与印花税，不能是一笔零成本的假交易。"""

    result = run_signal_backtest(
        _frame(),
        np.array([False, True, False, False, False, False]),
        np.zeros(6, dtype=bool),
        SignalBacktestConfig(
            initial_cash=10_000, commission=0.001, min_commission=0.1, stamp_tax=0.001
        ),
    )

    assert result.trades["direction"].tolist() == ["BUY"]

    with_tax = run_signal_backtest(
        _frame(),
        np.array([False, True, False, False, False, False]),
        np.zeros(6, dtype=bool),
        SignalBacktestConfig(
            initial_cash=10_000,
            commission=0.001,
            min_commission=0.1,
            stamp_tax=0.001,
            close_at_end=True,
        ),
    )

    trade = with_tax.trades.iloc[1]
    proceeds = 800 * 13.0
    assert trade["commission"] == pytest.approx(proceeds * 0.001)
    assert trade["stamp_tax"] == pytest.approx(proceeds * 0.001)
    # 平仓盈亏要扣掉双边成本，不能等于毛收益。
    assert trade["pnl"] < proceeds - 800 * 12.0
