from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import numpy as np
import pandas as pd

from selector_app.individual_research.models import IndividualResearchConfig
from selector_app.individual_research.service import (
    IndividualStockResearchService,
    _rank_candidates,
    _select_rule_pairs,
)
from selector_app.market_data.models import StockRef
from selector_app.screening.models import ScreenCondition
from selector_app.screening.rules import RuleGroup


class FakeAdapter:
    def stock_ref(self, _vipdoc_path: str, market: str, code: str) -> StockRef:
        return StockRef(market=market, code=code, path=Path("/tmp/600000.day"), name="测试股票")


class FakeRiskService:
    def __init__(self) -> None:
        self.requests = []

    def run(self, request, *, progress_callback=None, abort_check=None):
        self.requests.append(request)
        config = dict(request.base_config)
        config.update(
            {
                "stop_loss_pct": 0.08,
                "take_profit_pct": 0.2,
                "trailing_stop_pct": None,
            }
        )
        metrics = {
            "train": {
                "sharpe": 0.7,
                "annual_return": 0.12,
                "total_trades": 12,
                "closed_trades": 12,
            },
            "validation": {
                "sharpe": 0.8,
                "annual_return": 0.15,
                "total_return": 0.05,
                "max_drawdown": -0.31,
                "win_rate": 0.6,
                "payoff_ratio": 1.6,
                "total_trades": 6,
                "closed_trades": 6,
            },
        }
        if progress_callback is not None:
            progress_callback(1, 1, "completed", {})
        candidate = {
            "candidate_hash": f"risk-{len(self.requests)}",
            "config": config,
            "human_readable_rules": "止损 8% · 止盈 20%",
            "metrics": metrics,
            "data_fingerprint": "data-fingerprint",
        }
        return {
            "results_by_sharpe": [candidate],
            "results_by_return": [candidate],
        }


class FixtureIndividualResearchService(IndividualStockResearchService):
    def _prepare(self, _config, _ref):
        return SimpleNamespace(data_fingerprint="prepared-fingerprint")

    def _research_window(self, _prepared, _config):
        dates = pd.date_range("2020-01-01", periods=800, freq="B").to_numpy()
        features = pd.DataFrame({"technical.rsi14": np.linspace(10.0, 90.0, len(dates))})
        labels = {
            "up_onset_ahead": np.tile([0.0, 1.0], len(dates) // 2),
            "down_onset_ahead": np.tile([1.0, 0.0], len(dates) // 2),
        }
        return dates, features, labels, {}, []

    def _mine_conditions(self, _config, _features, _labels, _signals, _train_slice):
        buy = RuleGroup(
            conditions=(
                ScreenCondition(left_value="technical.rsi14", operator="lte", right_constant=35.0),
            )
        )
        sell = RuleGroup(
            conditions=(
                ScreenCondition(left_value="technical.rsi14", operator="gte", right_constant=65.0),
            )
        )
        return {
            "buy": [
                {
                    "candidate_id": "buy-1",
                    "side": "buy",
                    "kind": "condition",
                    "display": "RSI <= 35",
                    "support_count": 12,
                    "outcome_rate": 0.8,
                    "baseline_rate": 0.5,
                    "lift": 1.6,
                    "rule": buy,
                }
            ],
            "sell": [
                {
                    "candidate_id": "sell-1",
                    "side": "sell",
                    "kind": "condition",
                    "display": "RSI >= 65",
                    "support_count": 11,
                    "outcome_rate": 0.75,
                    "baseline_rate": 0.5,
                    "lift": 1.5,
                    "rule": sell,
                }
            ],
        }

    def _backtest_metrics(self, _config, _ref, _buy_rule, _sell_rule, start_date, _end_date):
        validation = start_date > date(2021, 1, 1)
        return {
            "sharpe": 0.8 if validation else 0.4,
            "annual_return": 0.15 if validation else 0.08,
            "total_return": 0.05,
            "max_drawdown": -0.2,
            "win_rate": 0.6,
            "payoff_ratio": 1.5,
            "total_trades": 6 if validation else 12,
            "closed_trades": 6 if validation else 12,
            "data_fingerprint": "data-fingerprint",
        }

    def _backtest_candidate_metrics(self, _raw_config, _start_date, _end_date):
        return {
            "sharpe": 0.5,
            "annual_return": 0.09,
            "total_return": 0.02,
            "max_drawdown": -0.35,
            "win_rate": 0.5,
            "payoff_ratio": 1.1,
            "total_trades": 3,
            "closed_trades": 3,
            "data_fingerprint": "data-fingerprint",
        }


def _config() -> IndividualResearchConfig:
    return IndividualResearchConfig(
        market="SH",
        code="600000",
        name="测试股票",
        start_date=date(2020, 1, 1),
        end_date=date(2023, 1, 1),
        value_ids=("technical.rsi14",),
        signal_ids=(),
        risk_trials=1000,
    )


def test_individual_pipeline_optimizes_validation_top_rule_and_only_marks_holdout() -> None:
    risk = FakeRiskService()
    service = FixtureIndividualResearchService(
        adapter=FakeAdapter(),
        backtest_service=object(),
        portfolio_service=object(),
        strategy_service=risk,
    )

    result = service.run(_config())

    assert len(risk.requests) == 1
    assert risk.requests[0].max_trials == 1000
    assert risk.requests[0].min_trades == 0
    assert risk.requests[0].max_drawdown is None
    assert risk.requests[0].holdout_candidates == 0
    assert risk.requests[0].base_config["single_security_mode"] is True
    assert risk.requests[0].base_config["ranking_value"] is None
    assert result["results_by_sharpe"][0]["metrics"]["validation"]["sharpe"] == 0.8
    assert result["results_by_sharpe"][0]["metrics"]["holdout"]["closed_trades"] == 3
    assert result["results_by_sharpe"][0]["quality"]["badges"] == ["样本较充分", "风险高"]
    assert result["results_by_sharpe"][0]["condition_rules"]["buy"]["conditions"]


def test_training_mining_creates_and_or_rules_from_two_indicators() -> None:
    patterns = np.asarray([(0.0, 0.0), (0.0, 1.0), (1.0, 0.0), (1.0, 1.0)] * 20)
    features = pd.DataFrame(
        {
            "technical.rsi14": np.repeat(patterns[:, 0], 10),
            "technical.return20": np.repeat(patterns[:, 1], 10),
        }
    )
    up_labels = np.repeat((patterns[:, 0] * patterns[:, 1]).astype(float), 10)
    labels = {
        "up_onset_ahead": up_labels,
        "down_onset_ahead": 1.0 - up_labels,
    }
    service = IndividualStockResearchService(
        adapter=FakeAdapter(),
        backtest_service=object(),
        portfolio_service=object(),
        strategy_service=FakeRiskService(),
    )

    candidates = service._mine_conditions(
        _config(),
        features,
        labels,
        {},
        slice(0, len(up_labels)),
    )

    buy_combinations = [item for item in candidates["buy"] if item.get("kind") == "condition_combo"]
    assert {cast(RuleGroup, item["rule"]).combine_mode for item in buy_combinations} == {
        "all",
        "any",
    }
    combined_buy_rule = cast(RuleGroup, buy_combinations[0]["rule"])
    for candidate in buy_combinations:
        rule = cast(RuleGroup, candidate["rule"])
        assert len(rule.conditions) == 2
        assert {condition.left_value for condition in rule.conditions} == {
            "technical.rsi14",
            "technical.return20",
        }
    sell_rule = cast(RuleGroup, candidates["sell"][0]["rule"])
    risk_base = service._risk_base_payload(_config(), combined_buy_rule, sell_rule)
    assert len(risk_base["buy_rule"]["conditions"]) == 2


def test_validation_rule_shortlist_keeps_five_candidates_per_board() -> None:
    candidates = [
        {
            "candidate_id": f"rules-{index}",
            "validation": {"sharpe": float(index), "annual_return": float(9 - index)},
        }
        for index in range(10)
    ]

    shortlisted = _select_rule_pairs(candidates)

    assert len(shortlisted) == 10
    assert {item["candidate_id"] for item in shortlisted} == {
        *(f"rules-{index}" for index in range(10))
    }


def test_risk_candidates_are_ranked_by_nested_validation_metrics() -> None:
    lower_sharpe = {
        "candidate_id": "risk-1",
        "metrics": {"validation": {"sharpe": 0.4, "annual_return": 0.2}},
    }
    higher_sharpe = {
        "candidate_id": "risk-2",
        "metrics": {"validation": {"sharpe": 1.1, "annual_return": 0.1}},
    }

    assert _rank_candidates([lower_sharpe, higher_sharpe], "sharpe") == [
        higher_sharpe,
        lower_sharpe,
    ]
    assert _rank_candidates([lower_sharpe, higher_sharpe], "annual_return") == [
        lower_sharpe,
        higher_sharpe,
    ]


def test_swing_labels_keep_the_unanswerable_horizon_at_the_end() -> None:
    dates = pd.date_range("2020-01-01", periods=800, freq="B")
    close = 20.0 + np.sin(np.arange(len(dates)) / 7.0) * 1.4 + np.arange(len(dates)) * 0.001
    frame = pd.DataFrame(
        {
            "date": dates,
            "open": close,
            "high": close + 0.25,
            "low": close - 0.25,
            "close": close,
            "volume": 100_000.0,
            "amount": close * 100_000.0,
        }
    )
    features = pd.DataFrame({"date": dates, "technical.rsi14": np.linspace(10, 90, len(dates))})
    prepared = SimpleNamespace(frame=frame, feature_values=features, formula_results={})
    service = IndividualStockResearchService(
        adapter=FakeAdapter(),
        backtest_service=object(),
        portfolio_service=object(),
        strategy_service=FakeRiskService(),
    )

    returned_dates, returned_features, labels, _signals, segments = service._research_window(
        prepared,
        _config(),
    )

    assert len(returned_dates) == len(returned_features) == 783
    assert segments
    assert np.isnan(labels["up_onset_ahead"][-10:]).all()
    assert np.isnan(labels["down_onset_ahead"][-10:]).all()


def test_condition_mining_works_on_one_symbol_time_series() -> None:
    service = FixtureIndividualResearchService(
        adapter=FakeAdapter(),
        backtest_service=object(),
        portfolio_service=object(),
        strategy_service=FakeRiskService(),
    )
    values = np.tile(np.arange(10, dtype=float), 20)
    labels = {
        "up_onset_ahead": (values < 4).astype(float),
        "down_onset_ahead": (values > 5).astype(float),
    }
    features = pd.DataFrame({"technical.rsi14": values})

    mined = service._mine_conditions(_config(), features, labels, {}, slice(None))

    assert mined["buy"]
    assert mined["sell"]
    assert all(
        candidate["rule"].conditions[0].left_value == "technical.rsi14"
        for candidate in mined["buy"]
    )
    assert all(
        candidate["rule"].conditions[0].left_value == "technical.rsi14"
        for candidate in mined["sell"]
    )
