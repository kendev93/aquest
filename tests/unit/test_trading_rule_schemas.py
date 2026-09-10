from __future__ import annotations

from selector_app.web.schemas import BacktestRequest, PortfolioBacktestRequest


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
