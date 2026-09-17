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
    _backtest_config,
    _compact_report,
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


def _report_with_trades() -> BacktestReport:
    trade = {
        "datetime": pd.Timestamp("2024-01-03"),
        "market": "SH",
        "code": "600000",
        "name": "测试股份",
        "direction": "SELL",
        "size": 100.0,
        "price": 11.0,
        "commission": 0.11,
        "stamp_tax": 1.1,
        "slippage": 0.0,
        "pnl": 90.0,
        "cost_basis": 1000.0,
        "rejected": False,
    }
    return BacktestReport(
        market="SH",
        code="600000",
        name="测试股份",
        bars=5,
        start_date="2024-01-02",
        end_date="2024-01-06",
        buy_signal="custom.buy",
        sell_signal="custom.sell",
        performance={"total_return": 0.09, "win_rate": 1.0},
        equity_curve=({"date": "2024-01-02", "total": 10_000.0},),
        trades=(trade,),
        positions=({"datetime": pd.Timestamp("2024-01-03"), "size": 0.0},),
        configuration={"initial_cash": 10_000.0},
        diagnostic=None,
    )


def test_compact_report_equals_the_full_serialisation_projection() -> None:
    """回归：精简报告必须与「先 to_dict 再裁剪」的旧写法逐字段一致。"""

    report = _report_with_trades()

    legacy = report.to_dict()
    legacy["trades"] = [
        {key: trade.get(key) for key in ("direction", "pnl", "cost_basis", "rejected")}
        for trade in legacy["trades"]
    ]
    legacy["equity_curve"] = []
    legacy["positions"] = []

    compact = _compact_report(report)

    assert compact == legacy
    assert list(compact) == list(legacy)
    assert compact["trades"] == [
        {"direction": "SELL", "pnl": 90.0, "cost_basis": 1000.0, "rejected": False}
    ]


def _service_for_features() -> ConditionResearchService:
    return ConditionResearchService(adapter=SimpleNamespace(), backtest_service=SimpleNamespace())


def _config_with_formula(formula_text: str) -> ConditionResearchConfig:
    return ConditionResearchConfig(
        vipdoc_path="/tmp/vipdoc",
        universe="all",
        buy_rule=RuleGroup(signals=("custom.buy",)),
        sell_rule=RuleGroup(signals=("custom.sell",)),
        formula_text=formula_text,
        start_date=date(2020, 1, 1),
        end_date=date(2020, 12, 31),
    )


def test_price_scale_custom_outputs_are_excluded_from_mining() -> None:
    service = _service_for_features()
    # 用 ``:=`` 让 SCORE/RATIO 只作为数值输出，避免信号语句也带出同名输出。
    config = _config_with_formula("SCORE:=C; RATIO:=C/MA(C,20); CROSS(REF(C,1),C);")

    assert service._feature_ids(config) == ("custom.ratio",)


def test_a_purely_price_scale_formula_is_rejected_with_a_readable_error() -> None:
    service = _service_for_features()
    config = _config_with_formula("SCORE:=C; CROSS(REF(C,1),C);")

    with pytest.raises(ValueError, match="价格/成交量量纲"):
        service._feature_ids(config)


class FakeMixedResearchAdapter(FakeResearchAdapter):
    def __init__(self) -> None:
        self.refs = [
            StockRef("SH", "600000", Path("/tmp/vipdoc"), name="甲", instrument_type="stock"),
            StockRef(
                "SZ",
                "159919",
                Path("/tmp/vipdoc"),
                name="乙ETF",
                instrument_type="fund",
                board="fund",
            ),
        ]


def _research_config(**overrides: object) -> ConditionResearchConfig:
    values: dict[str, object] = {
        "vipdoc_path": "/tmp/vipdoc",
        "universe": "all",
        "buy_rule": RuleGroup(signals=("custom.buy",)),
        "sell_rule": RuleGroup(signals=("custom.sell",)),
        "start_date": date(2020, 1, 1),
        "end_date": date(2020, 12, 31),
        "min_trades": 1,
        "min_instruments": 1,
    }
    values.update(overrides)
    return ConditionResearchConfig(**values)  # type: ignore[arg-type]


def test_research_pool_follows_the_selected_instrument_type() -> None:
    service = ConditionResearchService(
        adapter=FakeMixedResearchAdapter(),
        backtest_service=FakePreparedBacktests(),  # type: ignore[arg-type]
    )

    stock_pool = service._list_stock_refs(_research_config())
    fund_pool = service._list_stock_refs(_research_config(instrument_types=("fund",)))

    assert [ref.code for ref in stock_pool] == ["600000"]
    assert [ref.code for ref in fund_pool] == ["159919"]


def test_etf_research_uses_fund_costs_without_stamp_tax() -> None:
    config = _research_config()
    stock_ref = StockRef("SH", "600000", Path("/tmp/vipdoc"), instrument_type="stock")
    fund_ref = StockRef(
        "SZ", "159919", Path("/tmp/vipdoc"), instrument_type="fund", board="fund"
    )

    stock_backtest = _backtest_config(config, stock_ref)
    fund_backtest = _backtest_config(config, fund_ref)

    assert stock_backtest.commission == 0.0001
    assert stock_backtest.stamp_tax == 0.001
    assert fund_backtest.commission == 0.00005
    assert fund_backtest.min_commission == 0.1
    assert fund_backtest.stamp_tax == 0.0


def test_etf_research_keeps_fund_commission_when_stamp_tax_is_cleared() -> None:
    """回归：只改印花税不应让 ETF 退回股票佣金。"""

    config = _research_config(stamp_tax=0.0)
    fund_ref = StockRef(
        "SZ", "159919", Path("/tmp/vipdoc"), instrument_type="fund", board="fund"
    )

    fund_backtest = _backtest_config(config, fund_ref)

    assert fund_backtest.commission == 0.00005
    assert fund_backtest.stamp_tax == 0.0


def test_etf_research_pool_ignores_stock_board_filters() -> None:
    service = ConditionResearchService(
        adapter=FakeMixedResearchAdapter(),
        backtest_service=FakePreparedBacktests(),  # type: ignore[arg-type]
    )
    config = _research_config(instrument_types=("fund",), boards=("main",))

    assert config.boards == ()
    assert [ref.code for ref in service._list_stock_refs(config)] == ["159919"]
