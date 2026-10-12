from __future__ import annotations

import pandas as pd

from aquest.backtest.performance import PerformanceAnalyzer


def _analyzer(pnl: list[float]) -> PerformanceAnalyzer:
    equity = pd.DataFrame({"total": [100.0, 110.0, 120.0, 130.0]})
    trades = pd.DataFrame(
        {
            "direction": ["SELL"] * len(pnl),
            "pnl": pnl,
            "cost_basis": [1_000.0] * len(pnl),
        }
    )
    return PerformanceAnalyzer(equity, trades)


def test_win_rate_excludes_break_even_trades_from_denominator() -> None:
    metrics = _analyzer([10.0, -5.0, 0.0]).compute()

    assert metrics["win_trades"] == 1.0
    assert metrics["lose_trades"] == 1.0
    assert metrics["total_trades"] == 3.0
    # 保本单既不进分子也不进分母。只修 losses = pnl < 0 而分母仍用全部平仓单的话，
    # 保本单只占分母，照样压低胜率，且 win_trades + lose_trades 与分母对不上账。
    assert metrics["win_rate"] == 0.5


def test_win_rate_is_zero_when_no_trade_is_decided() -> None:
    assert _analyzer([0.0, 0.0]).compute()["win_rate"] == 0.0


def test_win_rate_counts_every_decided_trade() -> None:
    assert _analyzer([10.0, 20.0, -5.0]).compute()["win_rate"] == 2 / 3
