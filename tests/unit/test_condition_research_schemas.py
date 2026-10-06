from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from selector_app.web.schemas import ConditionResearchRequest


def payload() -> dict[str, object]:
    return {
        "universe": "all",
        "buy_rule": {"signals": ["indicator_three.begin_zone"]},
        "sell_rule": {"signals": ["indicator_three.end_zone"]},
        "date_mode": "custom",
        "start_date": "2020-01-01",
        "end_date": "2024-12-31",
    }


def test_research_request_resolves_into_independent_stock_config() -> None:
    request = ConditionResearchRequest.model_validate(payload())

    config = request.to_config(start_date=date(2020, 1, 1), end_date=date(2024, 12, 31))

    assert config.universe == "all"
    assert config.search_sides == "both"
    assert config.buy_rule.signals == ("indicator_three.begin_zone",)
    assert config.sell_rule.signals == ("indicator_three.end_zone",)
    assert config.min_trades == 20
    assert config.position_mode == "full"
    assert config.train_ratio == 0.6
    assert config.validation_ratio == 0.2


def test_research_request_passes_validated_split_ratios_to_config() -> None:
    body = payload()
    body.update(train_ratio=0.5, validation_ratio=0.3)

    config = ConditionResearchRequest.model_validate(body).to_config()

    assert config.train_ratio == 0.5
    assert config.validation_ratio == 0.3


def test_research_request_rejects_split_ratios_without_a_holdout() -> None:
    body = payload()
    body.update(train_ratio=0.6, validation_ratio=0.4)

    with pytest.raises(ValidationError, match="比例之和必须小于 1"):
        ConditionResearchRequest.model_validate(body)


def test_research_request_defaults_to_stock_only_instrument_types() -> None:
    request = ConditionResearchRequest.model_validate(payload())

    config = request.to_config(start_date=date(2020, 1, 1), end_date=date(2024, 12, 31))

    assert config.instrument_types == ("stock",)


def test_research_request_accepts_an_explicit_stock_study() -> None:
    body = payload()
    body["instrument_types"] = ["stock"]

    request = ConditionResearchRequest.model_validate(body)

    assert request.instrument_types == ["stock"]


@pytest.mark.parametrize(
    ("instrument_types", "message"),
    [
        (["index"], "Input should be 'stock'"),
        (["bond"], "Input should be 'stock'"),
        (["fund"], "Input should be 'stock'"),
    ],
)
def test_research_request_rejects_unsupported_instrument_types(
    instrument_types: list[str], message: str
) -> None:
    body = payload()
    body["instrument_types"] = instrument_types

    with pytest.raises(ValidationError, match=message):
        ConditionResearchRequest.model_validate(body)


def test_research_request_allows_an_empty_sell_rule_for_hold_to_end() -> None:
    """不传卖出规则是合法的：表示持有到期末，让研究自己找卖点。"""

    body = payload()
    del body["sell_rule"]

    request = ConditionResearchRequest.model_validate(body)
    config = request.to_config(start_date=date(2020, 1, 1), end_date=date(2024, 12, 31))

    assert config.sell_rule is None


def test_research_request_rejects_an_empty_sell_rule_object() -> None:
    """传了卖出规则但没有内容时直接拒绝：要么给条件，要么整个留空表示持有到期末。"""

    body = payload()
    body["sell_rule"] = {"signals": [], "conditions": []}

    with pytest.raises(ValidationError, match="至少需要一个信号或指标条件"):
        ConditionResearchRequest.model_validate(body)


def test_research_request_allows_an_empty_buy_rule_for_always_in_market() -> None:
    """不传买入规则是合法的：表示持续满仓，让研究自己找买点。"""

    body = payload()
    del body["buy_rule"]

    request = ConditionResearchRequest.model_validate(body)
    config = request.to_config(start_date=date(2020, 1, 1), end_date=date(2024, 12, 31))

    assert config.buy_rule is None


def test_research_request_rejects_an_empty_buy_rule_object() -> None:
    """传了买入规则但没有内容时直接拒绝：要么给条件，要么整个留空表示持续满仓。"""

    body = payload()
    body["buy_rule"] = {"signals": [], "conditions": []}

    with pytest.raises(ValidationError, match="至少需要一个信号或指标条件"):
        ConditionResearchRequest.model_validate(body)


def test_research_request_rejects_both_rules_missing() -> None:
    body = payload()
    del body["buy_rule"]
    del body["sell_rule"]

    with pytest.raises(ValidationError, match="不能同时留空"):
        ConditionResearchRequest.model_validate(body)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("search_sides", "portfolio"),
        ("min_trades", 0),
        ("candidate_limit", 201),
        ("universe", "custom"),
    ],
)
def test_research_request_rejects_invalid_search_scope(field: str, value: object) -> None:
    body = payload()
    body[field] = value

    with pytest.raises(ValidationError):
        ConditionResearchRequest.model_validate(body)


def test_research_request_rejects_non_stock_board_filters() -> None:
    body = payload()
    body["boards"] = ["fund"]

    with pytest.raises(ValidationError, match="Input should be 'main'"):
        ConditionResearchRequest.model_validate(body)


def test_custom_formula_needs_a_numeric_output_for_condition_mining() -> None:
    body = payload()
    body["formula_text"] = "C>0"
    body["buy_rule"] = {"signals": ["custom.custom_signal"]}
    body["sell_rule"] = {"signals": ["custom.custom_signal"]}

    with pytest.raises(ValidationError, match="数值输出"):
        ConditionResearchRequest.model_validate(body)
