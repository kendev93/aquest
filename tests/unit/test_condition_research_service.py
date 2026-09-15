from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from selector_app.backtest.models import BacktestReport
from selector_app.condition_research.models import ConditionResearchConfig
from selector_app.condition_research.service import (
    ConditionResearchService,
    candidate_constraint_failures,
)
from selector_app.market_data.models import StockRef
from selector_app.screening.jobs import TaskUserError
from selector_app.screening.rules import RuleGroup


def test_candidate_must_lift_win_rate_and_preserve_trade_quality() -> None:
    baseline = {
        "pooled_trades": {
            "closed_trades": 100,
            "win_rate": 0.55,
            "payoff_ratio": 1.4,
            "mean_trade_return": 0.01,
            "stocks_with_trades": 20,
        }
    }
    candidate = {
        "pooled_trades": {
            "closed_trades": 80,
            "win_rate": 0.62,
            "payoff_ratio": 1.5,
            "mean_trade_return": 0.012,
            "stocks_with_trades": 18,
        }
    }

    failures = candidate_constraint_failures(
        candidate,
        baseline,
        min_trades=50,
        min_instruments=10,
    )

    assert failures == ()


def test_candidate_fails_when_it_only_improves_win_rate_on_too_few_trades() -> None:
    baseline = {
        "pooled_trades": {
            "closed_trades": 100,
            "win_rate": 0.55,
            "payoff_ratio": 1.4,
            "mean_trade_return": 0.01,
            "stocks_with_trades": 20,
        }
    }
    candidate = {
        "pooled_trades": {
            "closed_trades": 12,
            "win_rate": 0.9,
            "payoff_ratio": 0.8,
            "mean_trade_return": -0.02,
            "stocks_with_trades": 2,
        }
    }

    failures = candidate_constraint_failures(
        candidate,
        baseline,
        min_trades=50,
        min_instruments=10,
    )

    assert "交易样本不足" in failures
    assert "覆盖股票不足" in failures
    assert "盈亏比低于基础规则" in failures
    assert "平均单笔收益低于基础规则" in failures


def test_candidate_without_baseline_win_rate_is_not_claimed_as_improved() -> None:
    baseline = {"pooled_trades": {"win_rate": None}}
    candidate = {"pooled_trades": {"win_rate": 0.8, "closed_trades": 40, "stocks_with_trades": 8}}

    failures = candidate_constraint_failures(
        candidate,
        baseline,
        min_trades=20,
        min_instruments=5,
    )

    assert "基础规则样本不足，无法比较胜率" in failures


class FakeResearchAdapter:
    def __init__(self) -> None:
        self.refs = [
            StockRef("SH", "600000", Path("/tmp/vipdoc"), name="甲", instrument_type="stock"),
            StockRef("SZ", "000001", Path("/tmp/vipdoc"), name="乙", instrument_type="stock"),
        ]

    def list_stock_refs(
        self,
        vipdoc_path,
        universe,
        universe_file=None,
        instrument_types=None,
        boards=None,
    ):
        del vipdoc_path, universe, universe_file, boards
        return [
            ref
            for ref in self.refs
            if instrument_types is None or ref.instrument_type in instrument_types
        ]


class FakePreparedBacktests:
    def prepare_for_ref(self, config, ref, *, feature_ids=()):
        dates = pd.date_range(config.start_date, config.end_date, freq="D")
        values = [5.0 if index == 4 else 1.0 if index == 19 else 3.0 for index in range(len(dates))]
        features = pd.DataFrame({"date": dates, "custom.score": values})
        return SimpleNamespace(
            config=config,
            ref=ref,
            frame=pd.DataFrame({"date": dates, "open": values, "close": values}),
            feature_values=features,
            data_fingerprint="stable",
        )

    def run_prepared(
        self,
        prepared,
        *,
        buy_filter=None,
        sell_trigger=None,
        start_date=None,
        end_date=None,
        summary_only=False,
    ):
        del sell_trigger, start_date, end_date, summary_only
        ref = prepared.ref
        if buy_filter is not None:
            trades = [
                {"direction": "SELL", "pnl": 10.0, "cost_basis": 100.0, "rejected": False},
                {"direction": "SELL", "pnl": 10.0, "cost_basis": 100.0, "rejected": False},
                {"direction": "SELL", "pnl": -10.0, "cost_basis": 100.0, "rejected": False},
            ]
        else:
            trades = [
                {"direction": "BUY", "signal_date": "2020-01-05", "date": "2020-01-06"},
                {
                    "direction": "SELL",
                    "signal_date": "2020-01-10",
                    "date": "2020-01-11",
                    "pnl": 10.0,
                    "cost_basis": 100.0,
                    "rejected": False,
                },
                {"direction": "BUY", "signal_date": "2020-01-20", "date": "2020-01-21"},
                {
                    "direction": "SELL",
                    "signal_date": "2020-01-25",
                    "date": "2020-01-26",
                    "pnl": -10.0,
                    "cost_basis": 100.0,
                    "rejected": False,
                },
            ]
        return BacktestReport(
            market=ref.market,
            code=ref.code,
            name=ref.name,
            bars=10,
            start_date="2020-01-01",
            end_date="2020-04-30",
            buy_signal=None,
            sell_signal=None,
            performance={
                "total_return": 0.1,
                "annual_return": 0.1,
                "max_drawdown": 0.1,
                "sharpe": 1.0,
            },
            equity_curve=(),
            trades=tuple(trades),
            positions=(),
            configuration={},
        )


def test_service_mines_on_train_and_replays_winner_filter_on_validation_and_holdout() -> None:
    service = ConditionResearchService(
        adapter=FakeResearchAdapter(),
        backtest_service=FakePreparedBacktests(),  # type: ignore[arg-type]
    )
    config = ConditionResearchConfig(
        vipdoc_path="/tmp/vipdoc",
        universe="all",
        buy_rule=RuleGroup(signals=("custom.buy",)),
        sell_rule=RuleGroup(signals=("custom.sell",)),
        formula_text="BUY:C>0; SELL:C<0; SCORE:C;",
        start_date=date(2020, 1, 1),
        end_date=date(2020, 4, 30),
        min_trades=1,
        min_instruments=1,
        candidate_limit=5,
        holdout_candidates=1,
    )

    result = service.run(config)

    assert result["processed"] == 2
    assert isinstance(result["data_fingerprint"], str)
    assert len(result["data_fingerprint"]) == 64
    assert result["baseline"]["full_period"]["pooled_trades"]["closed_trades"] > 0
    assert result["candidates"]
    candidate = result["candidates"][0]
    assert candidate["side"] == "buy"
    assert candidate["passed_constraints"] is True
    assert candidate["validation"]["pooled_trades"]["win_rate"] == pytest.approx(2 / 3)
    assert candidate["full_period"]["pooled_trades"]["win_rate"] == pytest.approx(2 / 3)
    assert candidate["holdout"] is not None


def test_service_aborts_if_market_data_changes_between_time_split_passes() -> None:
    class ChangingBacktests(FakePreparedBacktests):
        def __init__(self) -> None:
            self.prepare_counts: dict[str, int] = {}

        def prepare_for_ref(self, config, ref, *, feature_ids=()):
            prepared = super().prepare_for_ref(config, ref, feature_ids=feature_ids)
            count = self.prepare_counts.get(ref.code, 0) + 1
            self.prepare_counts[ref.code] = count
            prepared.data_fingerprint = "initial" if count == 1 else "changed"
            return prepared

    service = ConditionResearchService(
        adapter=FakeResearchAdapter(),
        backtest_service=ChangingBacktests(),  # type: ignore[arg-type]
    )
    config = ConditionResearchConfig(
        vipdoc_path="/tmp/vipdoc",
        universe="all",
        buy_rule=RuleGroup(signals=("custom.buy",)),
        sell_rule=RuleGroup(signals=("custom.sell",)),
        formula_text="BUY:C>0; SELL:C<0; SCORE:C;",
        start_date=date(2020, 1, 1),
        end_date=date(2020, 4, 30),
        search_sides="buy",
        min_trades=1,
        min_instruments=1,
        candidate_limit=5,
        holdout_candidates=1,
    )

    with pytest.raises(TaskUserError, match="行情数据在研究期间发生变化"):
        service.run(config)
