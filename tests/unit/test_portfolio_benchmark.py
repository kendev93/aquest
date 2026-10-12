"""等权买入持有基准：口径对不对，比数字好不好看重要。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from aquest.adapters.local_day_adapter import StockRef
from aquest.portfolio_backtest.benchmark import (
    BENCHMARK_LABEL,
    BenchmarkCurve,
    equal_weight_buy_hold,
)
from aquest.portfolio_backtest.context import _StockSeries
from aquest.portfolio_backtest.models import PortfolioBacktestConfig
from aquest.portfolio_backtest.service import PortfolioBacktestService

_DATES = [20240102, 20240103, 20240104]


def _series(
    code: str,
    closes: list[float],
    *,
    dates: list[int] | None = None,
    market: str = "SH",
    adjusted_closes: list[float] | None = None,
) -> _StockSeries:
    own_dates = dates if dates is not None else _DATES
    return _StockSeries(
        market=market,
        code=code,
        instrument_type="stock",
        board="main",
        dates=np.asarray(own_dates, dtype=np.int64),
        opens=np.asarray(closes, dtype=float),
        highs=np.asarray(closes, dtype=float),
        lows=np.asarray(closes, dtype=float),
        closes=np.asarray(closes, dtype=float),
        atrs=np.full(len(own_dates), np.nan),
        buy_mask=np.zeros(len(own_dates), dtype=bool),
        sell_mask=np.zeros(len(own_dates), dtype=bool),
        signals={},
        values={},
        date_to_index={int(value): index for index, value in enumerate(own_dates)},
        data_fingerprint="fingerprint",
        adjusted_closes=(
            None if adjusted_closes is None else np.asarray(adjusted_closes, dtype=float)
        ),
    )


def test_equal_weight_averages_normalised_prices() -> None:
    """A 涨到 1.2 倍、B 跌到 0.5 倍，等权基准就是两者的平均。"""

    curve = equal_weight_buy_hold(
        [
            _series("600000", [10.0, 11.0, 12.0]),
            _series("600001", [20.0, 20.0, 10.0]),
        ],
        _DATES,
        1_000_000.0,
    )

    assert curve is not None
    assert curve.label == BENCHMARK_LABEL
    assert curve.instrument_count == 2
    assert curve.totals[0] == pytest.approx(1_000_000.0)
    assert curve.totals[1] == pytest.approx(1_050_000.0)  # (1.1 + 1.0) / 2
    assert curve.totals[2] == pytest.approx(850_000.0)  # (1.2 + 0.5) / 2
    assert curve.performance["total_return"] == pytest.approx(-0.15)


def test_benchmark_credits_cash_dividends() -> None:
    """基准必须把现金分红算进去。

    这只票不复权看是 10 → 8（-20%），但中间每股派息 1 元，持有者手上多出 1 元现金，
    真实收益只有 -10%。用不复权价当基准，基准被系统性低估，而所有策略相对它的
    "超额"都虚高——跑输的策略看起来在跑赢。
    """

    curve = equal_weight_buy_hold(
        [_series("600000", [10.0, 9.0, 8.0], adjusted_closes=[10.0, 10.0, 9.0])],
        _DATES,
        1_000_000.0,
    )

    assert curve is not None
    assert curve.totals == pytest.approx([1_000_000.0, 1_000_000.0, 900_000.0])
    assert curve.performance["total_return"] == pytest.approx(-0.1)


def test_single_instrument_matches_its_own_price_change() -> None:
    curve = equal_weight_buy_hold([_series("600000", [10.0, 12.0, 9.0])], _DATES, 500_000.0)

    assert curve is not None
    assert curve.instrument_count == 1
    assert curve.totals == pytest.approx([500_000.0, 600_000.0, 450_000.0])
    assert curve.performance["total_return"] == pytest.approx(-0.1)


def test_instruments_without_a_first_day_price_are_excluded() -> None:
    """窗口开始后才出现的标的不能回填进基准：那等于默认上市前就买得到。"""

    late = _series("600001", [20.0, 21.0], dates=[20240103, 20240104])
    curve = equal_weight_buy_hold(
        [_series("600000", [10.0, 11.0, 12.0]), late],
        _DATES,
        1_000_000.0,
    )

    assert curve is not None
    assert curve.instrument_count == 1
    assert curve.totals == pytest.approx([1_000_000.0, 1_100_000.0, 1_200_000.0])


def test_missing_days_carry_the_previous_close_forward() -> None:
    """停牌期间价格不变，不是收益为零。"""

    curve = equal_weight_buy_hold(
        [_series("600000", [10.0, np.nan, 12.0])],
        _DATES,
        1_000_000.0,
    )

    assert curve is not None
    assert curve.totals == pytest.approx([1_000_000.0, 1_000_000.0, 1_200_000.0])


def test_dates_outside_the_window_are_ignored() -> None:
    curve = equal_weight_buy_hold(
        [_series("600000", [8.0, 10.0, 11.0, 12.0, 13.0], dates=[20240101, *_DATES, 20240105])],
        _DATES,
        1_000_000.0,
    )

    assert curve is not None
    assert curve.dates == tuple(_DATES)
    assert curve.totals == pytest.approx([1_000_000.0, 1_100_000.0, 1_200_000.0])


@pytest.mark.parametrize(
    "contexts,dates,cash",
    [
        ([], _DATES, 1_000_000.0),  # 空池
        ([_series("600000", [10.0, 11.0])], [20240102], 1_000_000.0),  # 窗口不足两天
        ([_series("600000", [10.0, 11.0, 12.0])], _DATES, 0.0),  # 没有本金
        ([_series("600000", [np.nan, np.nan, np.nan])], _DATES, 1_000_000.0),  # 全无价格
    ],
)
def test_returns_none_when_no_benchmark_can_be_built(
    contexts: list[_StockSeries], dates: list[int], cash: float
) -> None:
    assert equal_weight_buy_hold(contexts, dates, cash) is None


def test_performance_excludes_trade_metrics() -> None:
    """基准不交易，成交笔数、胜率这些留在基准里只会误导。"""

    curve = equal_weight_buy_hold([_series("600000", [10.0, 11.0, 12.0])], _DATES, 1_000_000.0)

    assert curve is not None
    assert set(curve.performance) == {
        "total_return",
        "annual_return",
        "max_drawdown",
        "max_dd_duration",
        "sharpe",
        "sortino",
        "calmar",
        "volatility",
        "start_cash",
        "end_value",
    }
    assert curve.performance["start_cash"] == pytest.approx(1_000_000.0)
    assert curve.performance["end_value"] == pytest.approx(1_200_000.0)


def test_curve_serialises_to_json_friendly_values() -> None:
    curve = equal_weight_buy_hold([_series("600000", [10.0, 11.0, 12.0])], _DATES, 1_000_000.0)

    assert isinstance(curve, BenchmarkCurve)
    payload = curve.to_dict()
    assert payload["instrument_count"] == 1
    assert payload["dates"] == _DATES
    assert payload["totals"] == pytest.approx([1_000_000.0, 1_100_000.0, 1_200_000.0])


# --------------------------------------------------------------------------
# 接进真实回测报告
# --------------------------------------------------------------------------

_CLOSES = {
    "600000": [10.0, 11.0, 12.0, 13.0, 14.0, 15.0],  # 1.5 倍
    "600001": [20.0, 19.0, 18.0, 17.0, 16.0, 15.0],  # 0.75 倍
    "600002": [5.0, 6.0, 7.0, 8.0, 9.0, 10.0],  # 2.0 倍
}


class _ThreeStockAdapter:
    def list_stock_refs(self, vipdoc_path, universe, universe_file=None):
        return [
            StockRef(market="SH", code=code, path=Path(vipdoc_path) / f"sh{code}.day")
            for code in _CLOSES
        ]

    def read_stock(self, ref: StockRef) -> pd.DataFrame:
        values = np.asarray(_CLOSES[ref.code], dtype=float)
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


def _config(**overrides: object) -> PortfolioBacktestConfig:
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


def test_report_carries_the_benchmark_next_to_the_strategy() -> None:
    report = PortfolioBacktestService(adapter=_ThreeStockAdapter()).run(_config())

    assert report.benchmark is not None
    assert report.benchmark["label"] == BENCHMARK_LABEL
    assert report.benchmark["instrument_count"] == 3
    totals = report.benchmark["totals"]
    assert len(totals) == len(report.equity_curve)
    assert totals[0] == pytest.approx(10_000.0)
    # 1.5 / 0.75 / 2.0 三者的等权平均 = 1.4167 倍
    assert totals[-1] == pytest.approx(14_166.666, rel=1e-4)
    assert report.benchmark["performance"]["total_return"] == pytest.approx(0.41666, rel=1e-4)
    # 策略自己也在报告里，两者口径一致才可比。
    assert report.performance["start_cash"] == pytest.approx(10_000.0)


def test_report_without_a_benchmark_stays_valid() -> None:
    """窗口只有一天时算不出基准，报告其余部分照常。"""

    report = PortfolioBacktestService(adapter=_ThreeStockAdapter()).run(
        _config(start_date=date(2024, 1, 1), end_date=date(2024, 1, 1))
    )

    assert report.benchmark is None
