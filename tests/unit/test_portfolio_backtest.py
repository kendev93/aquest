from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from selector_app.adapters.local_day_adapter import StockRef
from selector_app.portfolio_backtest.models import PortfolioBacktestConfig
from selector_app.portfolio_backtest.service import PortfolioBacktestService
from selector_app.screening.models import ScreenCondition
from selector_app.screening.rules import RuleGroup


def stock_frame(close: list[float]) -> pd.DataFrame:
    values = np.asarray(close, dtype=float)
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=len(values), freq="D"),
            "open": values,
            "high": values + 0.5,
            "low": values - 0.5,
            "close": values,
            "volume": np.full(len(values), 100_000.0),
            "amount": values * 100_000.0,
        }
    )


class FakeAdapter:
    def __init__(self) -> None:
        self.frames = {
            "600000": stock_frame([10, 11, 12, 13, 14, 15]),
            "600001": stock_frame([20, 19, 18, 17, 16, 15]),
            "600002": stock_frame([5, 6, 7, 8, 9, 10]),
        }

    def list_stock_refs(self, vipdoc_path, universe, universe_file=None):
        return [
            StockRef(market="SH", code=code, path=Path(vipdoc_path) / f"sh{code}.day")
            for code in self.frames
        ]

    def read_stock(self, ref: StockRef) -> pd.DataFrame:
        return self.frames[ref.code].copy(deep=True)


class BrokenAdapter:
    def list_stock_refs(self, vipdoc_path, universe, universe_file=None):
        return [StockRef(market="SH", code="600999", path=Path(vipdoc_path) / "sh600999.day")]

    def read_stock(self, ref: StockRef) -> pd.DataFrame:
        raise ValueError("读取失败")


class SparseAdapter:
    def __init__(self) -> None:
        sparse = stock_frame([10, 10])
        sparse["date"] = pd.to_datetime(["2024-01-01", "2024-01-03"])
        self.frames = {
            "600000": stock_frame([1, 1, 1]),
            "600001": sparse,
        }

    def list_stock_refs(self, vipdoc_path, universe, universe_file=None):
        return [
            StockRef(market="SH", code=code, path=Path(vipdoc_path) / f"sh{code}.day")
            for code in self.frames
        ]

    def read_stock(self, ref: StockRef) -> pd.DataFrame:
        return self.frames[ref.code].copy(deep=True)


class StaggeredAdapter:
    def __init__(self) -> None:
        first = stock_frame([20, 20, 20, 20])
        second = stock_frame([10, 8, 8])
        second["date"] = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
        self.frames = {"600000": first, "600001": second}

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
        "max_positions": 2,
        "rebalance_frequency": "daily",
        "formula_text": "BUY:C>0; RANK:C;",
        "start_date": date(2024, 1, 1),
        "end_date": date(2024, 1, 6),
        "stop_loss_pct": 0.05,
        "initial_cash": 10_000.0,
        "commission": 0.0,
        "min_commission": 0.0,
        "stamp_tax": 0.0,
        "slippage": 0.0,
    }
    values.update(overrides)
    return PortfolioBacktestConfig(**values)


def test_portfolio_fills_a_vacated_slot_by_rank_after_stop_loss() -> None:
    service = PortfolioBacktestService(adapter=FakeAdapter())

    report = service.run(config())

    assert report.total_candidates == 3
    assert report.processed == 3
    assert [trade["direction"] for trade in report.trades] == [
        "BUY",
        "BUY",
        "SELL",
        "BUY",
        "SELL",
        "BUY",
    ]
    assert [(trade["code"], trade["date"]) for trade in report.trades] == [
        ("600001", "2024-01-02"),
        ("600000", "2024-01-02"),
        ("600001", "2024-01-03"),
        ("600001", "2024-01-04"),
        ("600001", "2024-01-05"),
        ("600001", "2024-01-06"),
    ]
    assert {holding["code"] for holding in report.states[-1]["holdings"]} == {"600000", "600001"}
    assert report.states[-1]["positions_count"] == 2
    assert report.performance["end_value"] == 11_100.0
    json.dumps(report.to_dict(), ensure_ascii=False)


def test_portfolio_supports_indicator_threshold_exit() -> None:
    service = PortfolioBacktestService(adapter=FakeAdapter())

    report = service.run(
        config(
            formula_text="BUY:C>0; RANK:C; LEVEL:C;",
            ranking_value="custom.rank",
            sell_value="custom.level",
            sell_value_operator="gte",
            sell_value_threshold=14.0,
            stop_loss_pct=None,
        )
    )

    assert any(
        trade["direction"] == "SELL" and trade["code"] == "600000" for trade in report.trades
    )


def test_portfolio_accepts_signed_indicator_thresholds() -> None:
    configured = config(
        formula_text="BUY:C>0; RANK:C; LEVEL:C-20;",
        ranking_value="custom.rank",
        sell_value="custom.level",
        sell_value_operator="lte",
        sell_value_threshold=-10.0,
        stop_loss_pct=None,
    )

    assert configured.sell_value_threshold == -10.0


def test_portfolio_supports_indicator_comparison_exit() -> None:
    service = PortfolioBacktestService(adapter=FakeAdapter())

    report = service.run(
        config(
            formula_text="BUY:C>0; RANK:C; FAST:C; SLOW:MA(C,2);",
            ranking_value="custom.rank",
            compare_left_value="custom.fast",
            compare_operator="lt",
            compare_right_value="custom.slow",
            stop_loss_pct=None,
        )
    )

    assert any(trade["direction"] == "SELL" for trade in report.trades)


def test_empty_portfolio_report_counts_failed_stocks_as_skipped() -> None:
    report = PortfolioBacktestService(adapter=BrokenAdapter()).run(config())

    assert report.total_candidates == 1
    assert report.processed == 0
    assert report.skipped == 1
    assert report.errors == 1


def test_portfolio_does_not_carry_an_unfilled_buy_into_a_later_bar() -> None:
    report = PortfolioBacktestService(adapter=SparseAdapter()).run(
        config(
            rebalance_frequency="monthly",
            max_positions=1,
            stop_loss_pct=None,
            take_profit_pct=0.5,
        )
    )

    assert report.trades == ()


def test_portfolio_holding_days_are_paired_per_stock() -> None:
    report = PortfolioBacktestService(adapter=StaggeredAdapter()).run(
        config(
            formula_text="BUY:C>0; RANK:C; LEVEL:C;",
            ranking_value="custom.rank",
            max_positions=2,
            stop_loss_pct=None,
            sell_value="custom.level",
            sell_value_operator="lte",
            sell_value_threshold=8.0,
        )
    )

    assert [(trade["code"], trade["direction"]) for trade in report.trades] == [
        ("600000", "BUY"),
        ("600001", "BUY"),
        ("600001", "SELL"),
    ]
    assert report.performance["avg_holding_days"] == 1.0


def test_portfolio_rejects_unknown_sell_comparison_operators() -> None:
    with pytest.raises(ValueError, match="指标阈值比较方式"):
        config(
            sell_value="custom.rank",
            sell_value_operator="unknown",
            sell_value_threshold=1.0,
        )

    with pytest.raises(ValueError, match="指标比较方式"):
        config(
            compare_left_value="custom.rank",
            compare_operator="unknown",
            compare_right_value="custom.rank",
        )


def test_portfolio_fitness_filter_does_not_use_current_day_results() -> None:
    report = PortfolioBacktestService(adapter=FakeAdapter()).run(
        config(
            fitness_filter_enabled=True,
            fitness_min_score=0.0,
            fitness_min_trades=1,
            fitness_max_drawdown=1.0,
        )
    )

    first_event = report.ranking_events[0]
    assert first_event["date"] == "2024-01-01"
    assert first_event["candidates"]
    assert all(candidate["fitness_trades"] == 0 for candidate in first_event["candidates"])
    assert all(candidate["selected"] is False for candidate in first_event["candidates"])


def test_portfolio_uses_composable_buy_and_sell_rule_groups() -> None:
    service = PortfolioBacktestService(adapter=FakeAdapter())
    report = service.run(
        config(
            selected_signals=(),
            combine_mode="any",
            minimum_matches=None,
            buy_rule=RuleGroup(
                signals=("custom.buy",),
                conditions=(
                    ScreenCondition(
                        left_value="custom.rank",
                        operator="gte",
                        right_constant=10.0,
                    ),
                ),
                combine_mode="all",
            ),
            sell_rule=RuleGroup(
                conditions=(
                    ScreenCondition(
                        left_value="custom.rank",
                        operator="gte",
                        right_constant=14.0,
                    ),
                )
            ),
            formula_text="BUY:C>0; RANK:C;",
            stop_loss_pct=None,
            take_profit_pct=None,
        )
    )

    assert any(trade["direction"] == "SELL" for trade in report.trades)
