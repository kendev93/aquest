from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from selector_app.adapters.local_day_adapter import StockRef
from selector_app.portfolio_backtest.models import PortfolioBacktestConfig
from selector_app.portfolio_backtest.service import PortfolioBacktestService


def frame(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=len(rows), freq="D"),
            "open": [row[0] for row in rows],
            "high": [row[1] for row in rows],
            "low": [row[2] for row in rows],
            "close": [row[3] for row in rows],
            "volume": np.full(len(rows), 100_000.0),
            "amount": [row[3] * 100_000.0 for row in rows],
        }
    )


class OneStockAdapter:
    def __init__(self, bars: pd.DataFrame) -> None:
        self.bars = bars

    def list_stock_refs(self, vipdoc_path, universe, universe_file=None):
        return [StockRef(market="SH", code="600000", path=Path(vipdoc_path) / "sh600000.day")]

    def read_stock(self, ref: StockRef) -> pd.DataFrame:
        return self.bars.copy(deep=True)


class TwoStockAdapter:
    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        self.frames = frames

    def list_stock_refs(self, vipdoc_path, universe, universe_file=None):
        return [
            StockRef(market="SH", code=code, path=Path(vipdoc_path) / f"sh{code}.day")
            for code in self.frames
        ]

    def read_stock(self, ref: StockRef) -> pd.DataFrame:
        return self.frames[ref.code].copy(deep=True)


def config(**overrides: object) -> PortfolioBacktestConfig:
    values: dict[str, object] = {
        "vipdoc_path": "/tmp/vipdoc",
        "universe": "all",
        "selected_signals": ("custom.buy",),
        "combine_mode": "any",
        "minimum_matches": None,
        "ranking_value": "custom.rank",
        "rank_order": "desc",
        "max_positions": 1,
        "rebalance_frequency": "daily",
        "formula_text": "BUY:C>0; RANK:C;",
        "start_date": None,
        "end_date": None,
        "initial_cash": 10_000.0,
        "commission": 0.0,
        "min_commission": 0.0,
        "stamp_tax": 0.0,
        "slippage": 0.0,
    }
    values.update(overrides)
    return PortfolioBacktestConfig(**values)


def test_stop_loss_uses_daily_low_and_executes_on_the_trigger_bar() -> None:
    bars = frame(
        [
            (10.0, 10.5, 9.5, 10.0),
            (10.0, 10.5, 9.8, 10.0),
            (9.0, 9.5, 8.5, 9.4),
        ]
    )

    report = PortfolioBacktestService(adapter=OneStockAdapter(bars)).run(
        config(stop_loss_pct=0.1, stop_loss_cooldown_days=2)
    )

    sells = [trade for trade in report.trades if trade["direction"] == "SELL"]
    assert len(sells) == 1
    assert sells[0]["date"] == "2024-01-03"
    assert sells[0]["price"] == 9.0
    assert sells[0]["risk_trigger"] == "stop_loss"
    assert sells[0]["cooldown_days"] == 2


def test_take_profit_uses_daily_high_and_trigger_price_not_close() -> None:
    bars = frame(
        [
            (10.0, 10.5, 9.5, 10.0),
            (10.0, 10.5, 9.8, 10.0),
            (10.5, 12.0, 10.2, 10.5),
        ]
    )

    report = PortfolioBacktestService(adapter=OneStockAdapter(bars)).run(
        config(take_profit_pct=0.1)
    )

    sells = [trade for trade in report.trades if trade["direction"] == "SELL"]
    assert len(sells) == 1
    assert sells[0]["date"] == "2024-01-03"
    assert sells[0]["price"] == 11.0
    assert sells[0]["risk_trigger"] == "take_profit"


def test_trailing_stop_tracks_highest_daily_high_and_uses_its_cooldown() -> None:
    bars = frame(
        [
            (10.0, 10.5, 9.5, 10.0),
            (10.0, 10.5, 9.8, 10.0),
            (10.5, 12.0, 11.3, 11.5),
        ]
    )

    report = PortfolioBacktestService(adapter=OneStockAdapter(bars)).run(
        config(trailing_stop_pct=0.05, trailing_stop_cooldown_days=3)
    )

    sells = [trade for trade in report.trades if trade["direction"] == "SELL"]
    assert len(sells) == 1
    assert sells[0]["price"] == 11.4
    assert sells[0]["risk_trigger"] == "trailing_stop"
    assert sells[0]["cooldown_days"] == 3


def test_risk_cooldown_blocks_the_sold_symbol_but_allows_another_ranked_candidate() -> None:
    dates = pd.date_range("2024-01-01", periods=6, freq="D")
    first = frame(
        [
            (10.0, 10.5, 9.5, 10.0),
            (10.0, 10.5, 9.8, 10.0),
            (9.0, 9.5, 8.5, 9.0),
            (9.0, 9.5, 8.8, 9.0),
            (9.0, 9.5, 8.8, 9.0),
            (9.0, 9.5, 8.8, 9.0),
        ]
    )
    second = frame(
        [
            (8.0, 8.5, 7.5, 8.0),
            (8.0, 8.5, 7.8, 8.0),
            (8.0, 8.5, 7.8, 8.0),
            (8.0, 8.5, 7.8, 8.0),
            (8.0, 8.5, 7.8, 8.0),
            (8.0, 8.5, 7.8, 8.0),
        ]
    )
    first["date"] = dates
    second["date"] = dates

    report = PortfolioBacktestService(
        adapter=TwoStockAdapter({"600000": first, "600001": second})
    ).run(config(stop_loss_pct=0.1, stop_loss_cooldown_days=2))

    buys = [trade for trade in report.trades if trade["direction"] == "BUY"]
    assert [trade["code"] for trade in buys] == ["600000", "600001"]
    assert all(trade["code"] != "600000" or trade["date"] != "2024-01-04" for trade in buys)


def test_atr_risk_parameters_require_a_positive_multiplier() -> None:
    try:
        config(stop_loss_mode="atr", stop_loss_atr_multiplier=None)
    except ValueError as exc:
        assert "stop_loss_atr_multiplier" in str(exc)
    else:
        raise AssertionError("ATR 风控缺少倍数时应被拒绝")
