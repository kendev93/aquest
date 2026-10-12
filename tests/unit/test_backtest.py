from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from aquest.adapters.local_day_adapter import StockRef
from aquest.backtest.models import BacktestConfig
from aquest.backtest.service import BacktestService
from aquest.screening.models import ScreenCondition
from aquest.screening.rules import RuleGroup


def make_frame() -> pd.DataFrame:
    close = np.array([10.0, 11.0, 12.0, 13.0, 14.0, 13.0, 12.0, 14.0, 15.0, 14.0])
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=len(close), freq="D"),
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.full(len(close), 100_000.0),
            "amount": close * 100_000.0,
        }
    )


def make_long_frame() -> pd.DataFrame:
    close = np.linspace(10.0, 30.0, 120)
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=len(close), freq="D"),
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.full(len(close), 100_000.0),
            "amount": close * 100_000.0,
        }
    )


class FakeAdapter:
    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame

    def stock_ref(self, vipdoc_path: str | Path, market: str, code: str) -> StockRef:
        return StockRef(
            market=market,
            code=code,
            name="测试股份",
            path=Path(vipdoc_path) / "sh600000.day",
        )

    def read_stock(self, ref: StockRef) -> pd.DataFrame:
        return self.frame.copy(deep=True)


def config(**overrides: object) -> BacktestConfig:
    values: dict[str, object] = {
        "market": "SH",
        "code": "600000",
        "vipdoc_path": "/tmp/vipdoc",
        "buy_signal": "custom.buy",
        "sell_signal": "custom.sell",
        "formula_text": "BUY:C=11; SELL:C=14;",
        "start_date": date(2024, 1, 2),
        "end_date": date(2024, 1, 6),
        "initial_cash": 10_000.0,
        "commission": 0.0,
        "min_commission": 0.0,
        "stamp_tax": 0.0,
        "slippage": 0.0,
    }
    values.update(overrides)
    return BacktestConfig(**values)


def test_formula_backtest_uses_next_bar_execution_and_returns_metrics() -> None:
    service = BacktestService(adapter=FakeAdapter(make_frame()))

    report = service.run(config())

    assert report.market == "SH"
    assert report.code == "600000"
    assert report.name == "测试股份"
    assert report.bars == 5
    assert [trade["direction"] for trade in report.trades] == ["BUY", "SELL"]
    assert all(trade["name"] == "测试股份" for trade in report.trades)
    assert [trade["date"] for trade in report.trades] == ["2024-01-03", "2024-01-06"]
    assert report.performance["start_cash"] == 10_000.0
    assert report.performance["end_value"] == 10_800.0
    assert report.equity_curve[0]["date"] == "2024-01-02"
    assert report.equity_curve[-1]["date"] == "2024-01-06"
    json.dumps(report.to_dict(), ensure_ascii=False)


def test_prepared_backtest_exposes_features_and_reuses_them_for_rule_variants() -> None:
    service = BacktestService(adapter=FakeAdapter(make_frame()))
    prepared = service.prepare(
        config(
            formula_text="BUY:C=11; SELL:C=14; MOM:C-REF(C,1);",
        ),
        feature_ids=("custom.mom",),
    )

    assert prepared.feature_values["custom.mom"].iloc[1] == 1.0
    report = service.run_prepared(
        prepared,
        buy_filter=ScreenCondition(
            left_value="custom.mom",
            operator="gt",
            right_constant=2.0,
        ),
    )

    assert report.performance["total_trades"] == 0


def test_summary_backtest_preserves_metrics_and_trades_without_daily_details() -> None:
    service = BacktestService(adapter=FakeAdapter(make_frame()))
    prepared = service.prepare(config())

    complete = service.run_prepared(prepared)
    summary = service.run_prepared(prepared, summary_only=True)

    assert summary.performance == complete.performance
    assert summary.trades == complete.trades
    assert summary.equity_curve == ()
    assert summary.positions == ()


def test_trade_records_expose_signal_date_for_condition_mining() -> None:
    """回归：买卖条件研究按信号日抽取样本，成交记录投影不能丢掉 signal_date。"""

    service = BacktestService(adapter=FakeAdapter(make_frame()))

    report = service.run(config())

    assert [trade["direction"] for trade in report.trades] == ["BUY", "SELL"]
    assert all("signal_date" in trade for trade in report.trades)
    assert all(str(trade["signal_date"]) <= str(trade["date"]) for trade in report.trades)


def test_formula_backtest_calculates_signals_before_date_window() -> None:
    frame = make_frame()
    frame.loc[1, "close"] = 10.0
    service = BacktestService(adapter=FakeAdapter(frame))

    report = service.run(
        config(
            formula_text="BUY:CROSS(C,REF(C,1)); SELL:C=14;",
            start_date=date(2024, 1, 3),
            end_date=date(2024, 1, 6),
        )
    )

    assert report.bars == 4
    assert [trade["date"] for trade in report.trades] == ["2024-01-04", "2024-01-06"]
    assert report.performance["end_value"] == 10_000.0


def test_preset_backtest_uses_registry_signals() -> None:
    service = BacktestService(adapter=FakeAdapter(make_long_frame()))

    report = service.run(
        config(
            formula_text=None,
            buy_signal="indicator_three.begin_zone",
            sell_signal="indicator_three.end_zone",
            start_date=None,
            end_date=None,
        )
    )

    assert report.bars == 120
    assert report.buy_signal == "indicator_three.begin_zone"
    assert report.sell_signal == "indicator_three.end_zone"


def test_fixed_size_backtest_passes_order_size_to_engine() -> None:
    service = BacktestService(adapter=FakeAdapter(make_frame()))

    report = service.run(config(position_mode="fixed", fixed_size=200))

    assert report.trades[0]["size"] == 200


def test_formula_backtest_rejects_empty_date_window() -> None:
    service = BacktestService(adapter=FakeAdapter(make_frame()))

    with pytest.raises(ValueError, match="日期范围"):
        service.run(config(start_date=date(2025, 1, 1), end_date=date(2025, 1, 2)))


def test_formula_backtest_accepts_composable_buy_and_sell_rules() -> None:
    service = BacktestService(adapter=FakeAdapter(make_frame()))
    report = service.run(
        config(
            buy_signal=None,
            sell_signal=None,
            buy_rule=RuleGroup(
                signals=("custom.buy",),
                conditions=(
                    ScreenCondition(
                        left_value="custom.fast",
                        operator="gte",
                        right_constant=11.0,
                    ),
                ),
                combine_mode="all",
            ),
            sell_rule=RuleGroup(
                conditions=(
                    ScreenCondition(
                        left_value="custom.fast",
                        operator="gt",
                        right_value="custom.slow",
                    ),
                )
            ),
            formula_text="BUY:C>0; FAST:C; SLOW:MA(C,2);",
        )
    )

    assert report.buy_signal is None
    assert report.sell_signal is None
    assert report.trades


def test_backtest_without_a_sell_rule_holds_until_the_last_bar() -> None:
    """开启「持有到期末」后，没有卖出规则也能回测，并在最后一根按收盘价平仓。"""

    service = BacktestService(adapter=FakeAdapter(make_frame()))
    report = service.run(
        config(
            buy_signal=None,
            sell_signal=None,
            buy_rule=RuleGroup(signals=("custom.buy",)),
            sell_rule=None,
            formula_text="BUY:C=11;",
            close_at_end=True,
            end_date=date(2024, 1, 10),
        )
    )

    assert [trade["direction"] for trade in report.trades] == ["BUY", "SELL"]
    # 买入落在 01-03，平仓落在窗口最后一根 01-10。
    assert [trade["date"] for trade in report.trades] == ["2024-01-03", "2024-01-10"]


def test_backtest_without_a_sell_rule_is_rejected_unless_hold_to_end_is_on() -> None:
    """不给卖出规则又不勾「持有到期末」必须报错，不能悄悄跑出一个永不卖出的回测。"""

    with pytest.raises(ValueError, match="卖出规则不能为空"):
        config(
            buy_signal=None,
            sell_signal=None,
            buy_rule=RuleGroup(signals=("custom.buy",)),
            sell_rule=None,
            formula_text="BUY:C=11;",
        )


def test_hold_to_end_replay_treats_a_candidate_sell_trigger_as_the_whole_rule() -> None:
    """基础卖出为空时，候选条件是 OR 上去的，所以它本身就是完整卖出规则。"""

    service = BacktestService(adapter=FakeAdapter(make_frame()))
    prepared = service.prepare(
        config(
            buy_signal=None,
            sell_signal=None,
            buy_rule=RuleGroup(signals=("custom.buy",)),
            sell_rule=None,
            formula_text="BUY:C=11; FAST:C;",
            close_at_end=True,
            end_date=date(2024, 1, 10),
        )
    )

    triggered = service.run_prepared(
        prepared,
        sell_trigger=ScreenCondition(left_value="custom.fast", operator="gt", right_constant=11.5),
    )

    # 12.0 那天触发卖出，次日以 13.0 离场，而不是一路持有到 01-10。
    assert [trade["date"] for trade in triggered.trades] == ["2024-01-03", "2024-01-04"]


def test_backtest_without_a_buy_rule_reenters_right_after_each_exit() -> None:
    """开启「持续满仓」后，没有买入规则也能回测：每次离场后下一个交易日立即再入场。"""

    service = BacktestService(adapter=FakeAdapter(make_frame()))
    report = service.run(
        config(
            buy_signal=None,
            sell_signal=None,
            buy_rule=None,
            sell_rule=RuleGroup(signals=("custom.sell",)),
            formula_text="SELL:C=14;",
            always_in_market=True,
            end_date=date(2024, 1, 10),
        )
    )

    assert [trade["direction"] for trade in report.trades] == [
        "BUY",
        "SELL",
        "BUY",
        "SELL",
        "BUY",
    ]
    assert [trade["date"] for trade in report.trades] == [
        "2024-01-03",
        "2024-01-06",
        "2024-01-07",
        "2024-01-09",
        "2024-01-10",
    ]


def test_backtest_without_a_buy_rule_is_rejected_unless_always_in_market_is_on() -> None:
    """不给买入规则又不勾「持续满仓」必须报错，不能悄悄跑出一个永不入场的回测。"""

    with pytest.raises(ValueError, match="买入规则不能为空"):
        config(
            buy_signal=None,
            sell_signal=None,
            buy_rule=None,
            sell_rule=RuleGroup(signals=("custom.sell",)),
            formula_text="SELL:C=14;",
        )


def test_always_in_market_replay_treats_a_candidate_buy_filter_as_the_whole_rule() -> None:
    """基础买入为空时，候选条件是 AND 到「天天买」上的，所以它本身就是完整买入规则。"""

    service = BacktestService(adapter=FakeAdapter(make_frame()))
    prepared = service.prepare(
        config(
            buy_signal=None,
            sell_signal=None,
            buy_rule=None,
            sell_rule=RuleGroup(signals=("custom.sell",)),
            formula_text="SELL:C=14; FAST:C;",
            always_in_market=True,
            end_date=date(2024, 1, 10),
        )
    )

    filtered = service.run_prepared(
        prepared,
        buy_filter=ScreenCondition(left_value="custom.fast", operator="lt", right_constant=12.5),
    )

    # 01-06 离场后收盘价已经高于 12.5，直到 01-07 才重新满足入场，比持续满仓晚一天。
    assert [trade["date"] for trade in filtered.trades] == [
        "2024-01-03",
        "2024-01-06",
        "2024-01-08",
        "2024-01-09",
    ]
