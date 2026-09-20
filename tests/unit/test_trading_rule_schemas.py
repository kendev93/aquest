from __future__ import annotations

import pytest
from pydantic import ValidationError

from selector_app.web.schemas import (
    BacktestRequest,
    PortfolioBacktestRequest,
    StrategyDiscoveryRequest,
    StrategyFitnessRequest,
)


def condition(left: str, operator: str, **right: object) -> dict[str, object]:
    return {"left_value": left, "operator": operator, **right}


def test_backtest_request_accepts_signal_and_indicator_rule_groups() -> None:
    request = BacktestRequest.model_validate(
        {
            "market": "SH",
            "code": "600000",
            "formula_text": "BUY:C>10; FAST:C; SLOW:MA(C,2); SELL:C<9;",
            "buy_rule": {
                "signals": ["custom.buy"],
                "conditions": [condition("custom.fast", "gt", right_value="custom.slow")],
                "combine_mode": "all",
            },
            "sell_rule": {
                "conditions": [condition("custom.fast", "lt", right_constant=9)],
            },
        }
    )

    config = request.to_config()
    assert config.buy_rule is not None
    assert config.buy_rule.conditions[0].right_value == "custom.slow"
    assert config.sell_rule is not None
    assert config.sell_rule.conditions[0].right_constant == 9


def test_backtest_request_defaults_to_recent_five_years_mode() -> None:
    request = BacktestRequest.model_validate(
        {
            "market": "SH",
            "code": "600000",
            "buy_signal": "indicator_three.prepare_rally",
            "sell_signal": "indicator_three.end_zone",
        }
    )

    assert request.date_mode == "recent5"


def test_portfolio_and_fitness_requests_default_to_recent_five_years_mode() -> None:
    portfolio = PortfolioBacktestRequest.model_validate(
        {
            "ranking_value": "technical.ma5",
            "buy_rule": {"conditions": [condition("technical.ma5", "gt", right_constant=10)]},
            "sell_rule": {"conditions": [condition("technical.ma5", "lt", right_constant=5)]},
        }
    )
    fitness = StrategyFitnessRequest.model_validate(
        {
            "ranking_value": "technical.ma5",
            "buy_rule": {"conditions": [condition("technical.ma5", "gt", right_constant=10)]},
            "sell_rule": {"conditions": [condition("technical.ma5", "lt", right_constant=5)]},
        }
    )

    assert portfolio.date_mode == "recent5"
    assert fitness.date_mode == "recent5"


def test_portfolio_request_accepts_condition_only_buy_and_sell_groups() -> None:
    request = PortfolioBacktestRequest.model_validate(
        {
            "selected_signals": [],
            "ranking_value": "custom.fast",
            "formula_text": "FAST:C; SLOW:MA(C,2);",
            "buy_rule": {
                "conditions": [condition("custom.fast", "gt", right_value="custom.slow")],
            },
            "sell_rule": {
                "conditions": [condition("custom.fast", "gte", right_constant=20)],
            },
        }
    )

    config = request.to_config()
    assert config.selected_signals == ()
    assert config.buy_rule is not None
    assert config.sell_rule is not None


def test_portfolio_request_preserves_required_conditions_for_or_rules() -> None:
    request = PortfolioBacktestRequest.model_validate(
        {
            "ranking_value": "technical.ma5",
            "buy_rule": {
                "required_conditions": [
                    condition("technical.ma5", "gt", right_constant=10),
                ],
                "conditions": [
                    condition("technical.ma20", "gt", right_constant=20),
                    condition("technical.ma20", "lt", right_constant=5),
                ],
                "combine_mode": "any",
            },
            "sell_rule": {
                "conditions": [condition("technical.ma5", "lt", right_constant=5)],
            },
        }
    )

    config = request.to_config()

    assert config.buy_rule is not None
    assert config.buy_rule.required_conditions[0].right_constant == 10
    assert config.buy_rule.combine_mode == "any"


def test_portfolio_request_supports_atr_and_trailing_risk_controls() -> None:
    request = PortfolioBacktestRequest.model_validate(
        {
            "ranking_value": "technical.ma5",
            "buy_rule": {"conditions": [condition("technical.ma5", "gt", right_constant=10)]},
            "stop_loss_mode": "atr",
            "stop_loss_atr_multiplier": 2,
            "stop_loss_cooldown_days": 3,
            "trailing_stop_mode": "pct",
            "trailing_stop_pct": 0.05,
            "trailing_stop_cooldown_days": 5,
        }
    )

    config = request.to_config()
    assert config.atr_period == 14
    assert config.stop_loss_atr_multiplier == 2
    assert config.stop_loss_cooldown_days == 3
    assert config.trailing_stop_pct == 0.05


def test_strategy_discovery_request_builds_non_overlapping_date_splits_and_fees() -> None:
    request = StrategyDiscoveryRequest.model_validate(
        {
            "start_date": "2024-01-01",
            "end_date": "2024-01-31",
            "max_positions": 4,
        }
    )

    discovery = request.to_discovery_request()
    assert discovery.base_config["max_positions"] == 4
    assert discovery.base_config["start_date"] == "2024-01-01"
    assert discovery.base_config["end_date"] == "2024-01-31"
    assert discovery.base_config["commission"] == 0.0001
    assert discovery.base_config["min_commission"] == 0.1
    assert discovery.splits["search"]["end_date"] < discovery.splits["validation"]["start_date"]
    assert discovery.splits["validation"]["end_date"] < discovery.splits["holdout"]["start_date"]


def test_strategy_discovery_request_can_use_the_full_local_history() -> None:
    request = StrategyDiscoveryRequest.model_validate(
        {
            "max_positions": 3,
        }
    )

    discovery = request.to_discovery_request(
        default_start="2023-01-01",
        default_end="2025-04-30",
    )

    assert discovery.splits["search"]["start_date"] == "2023-01-01"
    assert discovery.splits["holdout"]["end_date"] == "2025-04-30"


def test_strategy_discovery_request_defaults_to_recent_five_years_mode() -> None:
    request = StrategyDiscoveryRequest.model_validate({"max_positions": 3})

    assert request.date_mode == "recent5"


def test_strategy_discovery_request_requires_dates_for_custom_mode() -> None:
    with pytest.raises(ValueError, match="自定义日期范围必须同时设置开始日期和结束日期"):
        StrategyDiscoveryRequest.model_validate({"date_mode": "custom", "max_positions": 3})


def test_strategy_discovery_request_records_custom_date_mode() -> None:
    request = StrategyDiscoveryRequest.model_validate(
        {
            "date_mode": "custom",
            "start_date": "2024-01-01",
            "end_date": "2024-01-31",
            "max_positions": 3,
        }
    )

    discovery = request.to_discovery_request()

    assert discovery.base_config["date_mode"] == "custom"


def test_strategy_discovery_request_preserves_required_buy_and_sell_conditions() -> None:
    request = StrategyDiscoveryRequest.model_validate(
        {
            "max_positions": 3,
            "required_buy_conditions": [
                {
                    "left_value": "technical.ma5",
                    "operator": "cross_up",
                    "right_value": "technical.ma20",
                }
            ],
            "required_sell_conditions": [
                {
                    "left_value": "technical.rsi14",
                    "operator": "gte",
                    "right_constant": 70,
                }
            ],
        }
    )

    discovery = request.to_discovery_request(
        default_start="2023-01-01",
        default_end="2025-04-30",
    )

    assert discovery.required_buy_conditions[0].operator == "cross_up"
    assert discovery.required_sell_conditions[0].right_constant == 70
    assert discovery.to_dict()["required_buy_conditions"]


def test_portfolio_request_rejects_a_fixed_size_that_is_not_a_whole_lot() -> None:
    """150 股会被模拟器静默取整成 100 股，请求层必须直接拒绝。"""

    base = {
        "ranking_value": "technical.ma5",
        "buy_rule": {"conditions": [condition("technical.ma5", "gt", right_constant=10)]},
        "sell_rule": {"conditions": [condition("technical.ma5", "lt", right_constant=5)]},
        "position_mode": "fixed",
    }

    with pytest.raises(ValidationError, match="100 的整数倍"):
        PortfolioBacktestRequest.model_validate({**base, "fixed_size": 150})

    accepted = PortfolioBacktestRequest.model_validate({**base, "fixed_size": 300})
    assert accepted.fixed_size == 300
