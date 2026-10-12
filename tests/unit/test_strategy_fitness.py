from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from aquest.adapters.local_day_adapter import StockRef
from aquest.market_data.adjustment import CorporateAction, compute_factor_ratio
from aquest.portfolio_backtest.models import PortfolioBacktestConfig
from aquest.screening.models import ScreenCondition
from aquest.screening.rules import RuleGroup
from aquest.strategy_fitness.models import StrategyFitnessConfig
from aquest.strategy_fitness.service import StrategyFitnessService, _CachedAdapter


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
        }

    def list_stock_refs(self, vipdoc_path, universe, universe_file=None):
        return [
            StockRef(
                market="SH",
                code=code,
                name=f"测试{code}",
                path=Path(vipdoc_path) / f"sh{code}.day",
            )
            for code in self.frames
        ]

    def read_stock(self, ref: StockRef) -> pd.DataFrame:
        return self.frames[ref.code].copy(deep=True)


def config(**overrides: object) -> StrategyFitnessConfig:
    values: dict[str, object] = {
        "vipdoc_path": "/tmp/vipdoc",
        "universe": "all",
        "selected_signals": ("custom.buy",),
        "combine_mode": "any",
        "minimum_matches": None,
        "ranking_value": "custom.rank",
        "formula_text": "BUY:C>0; RANK:C;",
        "sell_signal": None,
        "stop_loss_pct": 0.05,
        "start_date": date(2024, 1, 1),
        "end_date": date(2024, 1, 6),
        "initial_cash": 10_000.0,
        "commission": 0.0,
        "min_commission": 0.0,
        "stamp_tax": 0.0,
        "slippage": 0.0,
        "train_ratio": 0.5,
        "validation_ratio": 0.25,
        "min_trades": 1,
        "max_test_drawdown": 0.5,
    }
    values.update(overrides)
    strategy = PortfolioBacktestConfig(
        vipdoc_path=values.pop("vipdoc_path"),
        universe=values.pop("universe"),
        selected_signals=values.pop("selected_signals"),
        combine_mode=values.pop("combine_mode"),
        minimum_matches=values.pop("minimum_matches"),
        ranking_value=values.pop("ranking_value"),
        formula_text=values.pop("formula_text"),
        sell_signal=values.pop("sell_signal"),
        buy_rule=values.pop("buy_rule", None),
        sell_rule=values.pop("sell_rule", None),
        stop_loss_pct=values.pop("stop_loss_pct"),
        start_date=values.pop("start_date"),
        end_date=values.pop("end_date"),
        initial_cash=values.pop("initial_cash"),
        commission=values.pop("commission"),
        min_commission=values.pop("min_commission"),
        stamp_tax=values.pop("stamp_tax"),
        slippage=values.pop("slippage"),
        max_positions=1,
    )
    return StrategyFitnessConfig(strategy=strategy, **values)


def test_strategy_fitness_splits_time_and_scores_each_stock() -> None:
    report = StrategyFitnessService(adapter=FakeAdapter()).run(config())

    assert report.total_candidates == 2
    assert report.processed == 2
    assert len(report.results) == 2
    assert report.train_end_date == "2024-01-03"
    assert report.validation_end_date == "2024-01-04"
    assert {result.code for result in report.results} == {"600000", "600001"}
    assert {result.name for result in report.results} == {"测试600000", "测试600001"}
    for result in report.results:
        assert result.train.bars == 3
        assert result.validation.bars == 1
        assert result.test.bars == 2
        assert result.total_checks == 8
        assert 0 <= result.suitability_score <= 100


def test_strategy_fitness_uses_all_local_dates_when_date_window_is_blank() -> None:
    report = StrategyFitnessService(adapter=FakeAdapter()).run(
        config(start_date=None, end_date=None)
    )

    assert report.bars == 6
    assert report.start_date == "2024-01-01"
    assert report.end_date == "2024-01-06"


def test_strategy_fitness_requires_three_non_empty_time_windows() -> None:
    try:
        config(train_ratio=0.8, validation_ratio=0.3)
    except ValueError as exc:
        assert "训练和验证比例之和" in str(exc)
    else:
        raise AssertionError("expected invalid ratio configuration")


def test_strategy_fitness_reuses_composable_rule_groups() -> None:
    strategy = config(
        selected_signals=(),
        buy_rule=RuleGroup(
            conditions=(
                ScreenCondition(
                    left_value="custom.rank",
                    operator="gt",
                    right_constant=0,
                ),
            )
        ),
        sell_rule=RuleGroup(
            conditions=(
                ScreenCondition(
                    left_value="custom.rank",
                    operator="gte",
                    right_constant=14,
                ),
            )
        ),
        stop_loss_pct=None,
    )

    report = StrategyFitnessService(adapter=FakeAdapter()).run(strategy)

    assert report.processed == 2
    assert all(result.train.total_trades >= 0 for result in report.results)


def test_fitness_batch_preload_carries_corporate_actions() -> None:
    """批量预取行情时，分红送转事件必须跟着一起交给内层回测引擎。

    内层引擎按 ``load_corporate_actions`` 探测事件，探测不到就退回复权前口径：基准
    漏掉全部分红、指标把除权日的跳空当成暴跌。预取行情很容易顺手把事件一起漏掉，
    而且这种退化完全静默，只能靠这条链路上的断言守住。
    """

    class SpyingAdapter(FakeAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.requested: list[tuple[str, str]] = []

        def load_corporate_actions(self, keys):
            pairs = list(keys)
            self.requested.extend(pairs)
            return {key: () for key in pairs}

    adapter = SpyingAdapter()
    StrategyFitnessService(adapter=adapter).run(config())

    assert set(adapter.requested) == {("SH", "600000"), ("SH", "600001")}


def test_fitness_cached_adapter_forwards_actions_to_simulator() -> None:
    """``_CachedAdapter`` 拿到事件后要能原样转发给回测引擎的两个探测口。"""

    ref = StockRef(
        market="SH", code="600000", name="测试600000", path=Path("/tmp/vipdoc/sh600000.day")
    )
    action = CorporateAction(
        market="SH",
        code="600000",
        ex_date=date(2024, 1, 4),
        share_ratio=0.0,
        cash_per_share=0.5,
        factor_ratio=compute_factor_ratio(prev_close=20.0, share_ratio=0.0, cash_per_share=0.5),
        action_type="dividend",
    )
    cached = _CachedAdapter(
        [ref], {"SH600000": stock_frame([10, 11, 12])}, {("SH", "600000"): (action,)}
    )

    assert cached.read_corporate_actions(ref) == (action,)
    assert cached.load_corporate_actions([("SH", "600000")]) == {("SH", "600000"): (action,)}
