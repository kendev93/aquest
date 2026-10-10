"""fast_engine 内核与 run_signal_backtest 原循环的新旧对拍。

内核路径的立场是「移植不重设计」：同运算顺序、同分支。这里用同一批行情和
信号，分别强制走原循环与内核路径（JIT 版与纯 Python 版都测），断言净值、
持仓、交易逐笔完全一致（bit-exact）。任何一侧的语义漂移都会在这里炸出来。
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from selector_app.backtest import fast_engine
from selector_app.backtest.engine import SignalBacktestConfig, run_signal_backtest


def _synthetic_frame(n: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = np.maximum(np.round(10.0 + np.cumsum(rng.normal(0.0, 0.4, size=n)), 2), 1.5)
    open_ = np.round(np.maximum(close + rng.normal(0.0, 0.2, size=n), 1.5), 2)
    high = np.round(np.maximum(open_, close) + np.abs(rng.normal(0.0, 0.15, size=n)), 2)
    low = np.maximum(
        np.round(np.minimum(open_, close) - np.abs(rng.normal(0.0, 0.15, size=n)), 2), 1.0
    )
    dates = pd.bdate_range("2023-01-02", periods=n)
    volume = rng.integers(10_000, 100_000, size=n).astype(float)
    return pd.DataFrame(
        {
            "date": dates,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": close * volume,
        }
    )


def _mask_scenarios(n: int, seed: int) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    rng = np.random.default_rng(seed + 1)
    buy_random = rng.random(n) < 0.1
    sell_random = rng.random(n) < 0.08
    buy_first = np.zeros(n, dtype=bool)
    buy_first[0] = True
    buy_first[n // 2] = True
    sell_every = np.zeros(n, dtype=bool)
    sell_every[::5] = True
    alternating_buy = np.zeros(n, dtype=bool)
    alternating_buy[::2] = True
    alternating_sell = np.ones(n, dtype=bool)
    none = np.zeros(n, dtype=bool)
    all_true = np.ones(n, dtype=bool)
    return {
        "random": (buy_random, sell_random),
        "buy_first_sell_every": (buy_first, sell_every),
        "all_buy_never_sell": (all_true, none),
        "alternating": (alternating_buy, alternating_sell),
        "no_signals": (none, none),
    }


_CONFIGS = [
    SignalBacktestConfig(),
    SignalBacktestConfig(execution="next_close"),
    SignalBacktestConfig(position_mode="fixed", fixed_size=1000),
    SignalBacktestConfig(slippage=0.01),
    SignalBacktestConfig(close_at_end=True),
    SignalBacktestConfig(commission=0.0005, min_commission=5.0, stamp_tax=0.002),
    SignalBacktestConfig(initial_cash=37_000.0),
]

_CONFIG_PARAMS = [pytest.param(config, id=f"cfg{index}") for index, config in enumerate(_CONFIGS)]

_SCENARIO_NAMES = sorted(_mask_scenarios(60, 0))


def _assert_metrics_equal(left: dict[str, Any], right: dict[str, Any]) -> None:
    assert set(left) == set(right)
    for key in left:
        left_value = left[key]
        right_value = right[key]
        if isinstance(left_value, float) and isinstance(right_value, float):
            if math.isnan(left_value) or math.isnan(right_value):
                assert math.isnan(left_value) and math.isnan(right_value), key
            else:
                assert left_value == right_value, key
        else:
            assert left_value == right_value, key


def _assert_results_equal(
    baseline: Any,
    candidate: Any,
    *,
    summary_only: bool,
) -> None:
    _assert_metrics_equal(baseline.performance, candidate.performance)
    assert baseline.diagnostic == candidate.diagnostic
    if summary_only:
        assert list(baseline.equity_curve.columns) == list(candidate.equity_curve.columns)
        assert_frame_equal(baseline.equity_curve, candidate.equity_curve, check_exact=True)
    else:
        assert_frame_equal(baseline.equity_curve, candidate.equity_curve, check_exact=True)
    assert_frame_equal(baseline.trades, candidate.trades, check_exact=True)
    assert_frame_equal(baseline.positions, candidate.positions, check_exact=True)


@pytest.mark.parametrize("seed", [1, 7, 42])
@pytest.mark.parametrize("config", _CONFIG_PARAMS)
@pytest.mark.parametrize("scenario", _SCENARIO_NAMES)
def test_fast_kernel_matches_original_loop(
    monkeypatch: pytest.MonkeyPatch,
    seed: int,
    config: SignalBacktestConfig,
    scenario: str,
) -> None:
    frame = _synthetic_frame(360, seed)
    buys, sells = _mask_scenarios(len(frame), seed)[scenario]

    monkeypatch.setenv("SELECTOR_FAST_SIM", "0")
    baseline = run_signal_backtest(
        frame, buys, sells, config, market="SH", code="600000", name="对拍"
    )

    monkeypatch.setenv("SELECTOR_FAST_SIM", "1")
    jitted = run_signal_backtest(
        frame, buys, sells, config, market="SH", code="600000", name="对拍"
    )
    _assert_results_equal(baseline, jitted, summary_only=False)

    # 内核未包装的纯 Python 版本也要对拍：这条不依赖 numba，验证移植逻辑本身。
    monkeypatch.setattr(fast_engine, "_SIGNAL_KERNEL", fast_engine._signal_backtest_kernel_impl)
    pure = run_signal_backtest(frame, buys, sells, config, market="SH", code="600000", name="对拍")
    _assert_results_equal(baseline, pure, summary_only=False)


@pytest.mark.parametrize("summary_only", [False, True])
def test_fast_kernel_matches_original_in_summary_mode(
    monkeypatch: pytest.MonkeyPatch, summary_only: bool
) -> None:
    frame = _synthetic_frame(240, 11)
    buys, sells = _mask_scenarios(len(frame), 11)["random"]
    config = SignalBacktestConfig()

    monkeypatch.setenv("SELECTOR_FAST_SIM", "0")
    baseline = run_signal_backtest(
        frame, buys, sells, config, market="SZ", code="000001", summary_only=summary_only
    )

    monkeypatch.setenv("SELECTOR_FAST_SIM", "1")
    jitted = run_signal_backtest(
        frame, buys, sells, config, market="SZ", code="000001", summary_only=summary_only
    )
    _assert_results_equal(baseline, jitted, summary_only=summary_only)


def test_fast_kernel_matches_original_with_date_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """start/end 截断后的掩码切片流入内核时同样逐笔一致。"""

    frame = _synthetic_frame(300, 5)
    buys, sells = _mask_scenarios(len(frame), 5)["random"]
    config = SignalBacktestConfig(
        start_date=frame["date"].iloc[50].date(),
        end_date=frame["date"].iloc[250].date(),
    )

    monkeypatch.setenv("SELECTOR_FAST_SIM", "0")
    baseline = run_signal_backtest(frame, buys, sells, config, market="SH", code="600036")

    monkeypatch.setenv("SELECTOR_FAST_SIM", "1")
    jitted = run_signal_backtest(frame, buys, sells, config, market="SH", code="600036")
    _assert_results_equal(baseline, jitted, summary_only=False)
