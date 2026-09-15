from __future__ import annotations

from datetime import date

import pandas as pd

from selector_app.backtest.models import BacktestConfig
from selector_app.condition_research.samples import (
    build_buy_trade_samples,
    build_early_exit_samples,
    feature_rows_by_date,
)


def feature_frame() -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=5, freq="D")
    return pd.DataFrame(
        {
            "date": dates,
            "custom.strength": [0.1, 0.2, 0.3, 0.4, 0.5],
        }
    )


def test_buy_samples_use_entry_decision_features_and_closed_trade_results() -> None:
    trades = [
        {"direction": "BUY", "signal_date": "2024-01-01", "date": "2024-01-02"},
        {"direction": "SELL", "signal_date": "2024-01-03", "date": "2024-01-04", "pnl": 20.0},
        {"direction": "BUY", "signal_date": "2024-01-04", "date": "2024-01-05"},
    ]

    samples = build_buy_trade_samples(
        trades,
        feature_frame(),
        market="SH",
        code="600000",
    )

    assert samples.features["custom.strength"].tolist() == [0.1]
    assert samples.outcomes.tolist() == [True]
    assert samples.instruments.tolist() == ["SH600000"]
    assert len(samples.groups) == 1


def test_sell_samples_label_only_early_exits_that_beat_the_base_exit() -> None:
    bars = pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=5, freq="D"),
            "open": [10.0, 10.0, 8.0, 9.0, 7.0],
            "close": [10.0, 10.0, 8.0, 9.0, 7.0],
        }
    )
    trades = [
        {
            "direction": "BUY",
            "signal_date": "2024-01-01",
            "date": "2024-01-02",
        },
        {
            "direction": "SELL",
            "signal_date": "2024-01-04",
            "date": "2024-01-05",
            "size": 100.0,
            "cost_basis": 1_000.0,
            "pnl": -300.0,
        },
    ]
    config = BacktestConfig(
        market="SH",
        code="600000",
        vipdoc_path="/tmp/vipdoc",
        buy_signal="custom.buy",
        sell_signal="custom.sell",
        formula_text="BUY:C>0; SELL:C>0;",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 5),
        initial_cash=10_000.0,
        commission=0.0,
        min_commission=0.0,
        stamp_tax=0.0,
        slippage=0.0,
    )

    samples = build_early_exit_samples(
        trades,
        feature_frame(),
        bars,
        config,
        market="SH",
        code="600000",
    )

    assert samples.features["custom.strength"].tolist() == [0.2, 0.3]
    assert samples.outcomes.tolist() == [True, True]
    assert samples.groups[0] == samples.groups[1]


def test_sell_sample_mining_caps_decision_rows_per_trade() -> None:
    dates = pd.date_range("2024-01-01", periods=50, freq="D")
    bars = pd.DataFrame(
        {
            "date": dates,
            "open": [10.0] * len(dates),
            "close": [10.0] * len(dates),
        }
    )
    features = pd.DataFrame(
        {"date": dates, "custom.strength": [float(index) for index in range(len(dates))]}
    )
    trades = [
        {"direction": "BUY", "signal_date": "2024-01-01", "date": "2024-01-02"},
        {
            "direction": "SELL",
            "signal_date": "2024-02-15",
            "date": "2024-02-16",
            "size": 100.0,
            "cost_basis": 1_000.0,
            "pnl": 0.0,
        },
    ]
    config = BacktestConfig(
        market="SH",
        code="600000",
        vipdoc_path="/tmp/vipdoc",
        buy_signal="custom.buy",
        sell_signal="custom.sell",
        formula_text="BUY:C>0; SELL:C>0;",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 2, 19),
    )

    samples = build_early_exit_samples(
        trades,
        features,
        bars,
        config,
        market="SH",
        code="600000",
    )

    assert 0 < len(samples) <= 8
    assert len(set(samples.groups)) == 1


def test_prebuilt_feature_index_matches_the_internal_lookup() -> None:
    """买卖两侧共用一份预建索引时，样本必须与内部现建索引完全一致。"""

    feature_values = feature_frame()
    feature_rows = feature_rows_by_date(feature_values)
    buy_trades = [
        {"direction": "BUY", "signal_date": "2024-01-01", "date": "2024-01-02"},
        {"direction": "SELL", "signal_date": "2024-01-03", "date": "2024-01-04", "pnl": 20.0},
    ]

    built = build_buy_trade_samples(buy_trades, feature_values, market="SH", code="600000")
    reused = build_buy_trade_samples(
        buy_trades,
        feature_values,
        market="SH",
        code="600000",
        feature_rows=feature_rows,
    )

    assert len(reused) == 1
    assert reused.features.equals(built.features)
    assert reused.outcomes.equals(built.outcomes)
    assert reused.instruments.equals(built.instruments)
    assert reused.groups.equals(built.groups)

    bars = pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=5, freq="D"),
            "open": [10.0, 10.0, 8.0, 9.0, 7.0],
            "close": [10.0, 10.0, 8.0, 9.0, 7.0],
        }
    )
    config = BacktestConfig(
        market="SH",
        code="600000",
        vipdoc_path="/tmp/vipdoc",
        buy_signal="custom.buy",
        sell_signal="custom.sell",
        formula_text="BUY:C>0; SELL:C>0;",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 5),
        initial_cash=10_000.0,
        commission=0.0,
        min_commission=0.0,
        stamp_tax=0.0,
        slippage=0.0,
    )
    sell_trades = [
        {"direction": "BUY", "signal_date": "2024-01-01", "date": "2024-01-02"},
        {
            "direction": "SELL",
            "signal_date": "2024-01-04",
            "date": "2024-01-05",
            "size": 100.0,
            "cost_basis": 1_000.0,
            "pnl": -300.0,
        },
    ]

    sell_built = build_early_exit_samples(
        sell_trades,
        feature_values,
        bars,
        config,
        market="SH",
        code="600000",
    )
    sell_reused = build_early_exit_samples(
        sell_trades,
        feature_values,
        bars,
        config,
        market="SH",
        code="600000",
        feature_rows=feature_rows,
    )

    assert len(sell_reused) == 2
    assert sell_reused.features.equals(sell_built.features)
    assert sell_reused.outcomes.equals(sell_built.outcomes)
    assert sell_reused.instruments.equals(sell_built.instruments)
    assert sell_reused.groups.equals(sell_built.groups)
