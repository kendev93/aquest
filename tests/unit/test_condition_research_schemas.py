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


def test_research_request_defaults_to_stock_only_instrument_types() -> None:
    request = ConditionResearchRequest.model_validate(payload())

    config = request.to_config(start_date=date(2020, 1, 1), end_date=date(2024, 12, 31))

    assert config.instrument_types == ("stock",)


def test_research_request_accepts_an_etf_only_study() -> None:
    body = payload()
    body["instrument_types"] = ["fund"]

    request = ConditionResearchRequest.model_validate(body)

    assert request.instrument_types == ["fund"]


@pytest.mark.parametrize(
    ("instrument_types", "message"),
    [
        (["index"], "只支持股票或 ETF"),
        (["bond"], "只支持股票或 ETF"),
        (["stock", "fund"], "一次只能选择一类证券"),
    ],
)
def test_research_request_rejects_unsupported_instrument_types(
    instrument_types: list[str], message: str
) -> None:
    body = payload()
    body["instrument_types"] = instrument_types

    with pytest.raises(ValidationError, match=message):
        ConditionResearchRequest.model_validate(body)


def test_research_request_requires_both_base_rules() -> None:
    body = payload()
    del body["sell_rule"]

    with pytest.raises(ValidationError, match="sell_rule"):
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

    with pytest.raises(ValidationError, match="只支持股票板块"):
        ConditionResearchRequest.model_validate(body)


def test_custom_formula_needs_a_numeric_output_for_condition_mining() -> None:
    body = payload()
    body["formula_text"] = "C>0"
    body["buy_rule"] = {"signals": ["custom.custom_signal"]}
    body["sell_rule"] = {"signals": ["custom.custom_signal"]}

    with pytest.raises(ValidationError, match="数值输出"):
        ConditionResearchRequest.model_validate(body)
