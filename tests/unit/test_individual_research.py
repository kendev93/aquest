from __future__ import annotations

from datetime import date

import pytest

from selector_app.individual_research.metrics import candidate_quality
from selector_app.web.schemas import IndividualResearchRequest, PortfolioBacktestRequest


def _single_security_request(**overrides: object) -> dict[str, object]:
    return {
        "universe": "codes",
        "codes": ["SH:600000"],
        "single_security_mode": True,
        "ranking_value": None,
        "buy_rule": {
            "conditions": [
                {
                    "left_value": "technical.rsi14",
                    "operator": "lte",
                    "right_constant": 35,
                }
            ],
            "combine_mode": "all",
        },
        "sell_rule": {
            "conditions": [
                {
                    "left_value": "technical.rsi14",
                    "operator": "gte",
                    "right_constant": 65,
                }
            ],
            "combine_mode": "all",
        },
        "date_mode": "custom",
        "start_date": "2021-01-01",
        "end_date": "2025-12-31",
    } | overrides


def test_single_security_request_does_not_need_or_expose_a_ranking_value() -> None:
    request = PortfolioBacktestRequest.model_validate(_single_security_request())

    config = request.to_config()

    assert config.single_security_mode is True
    assert config.codes == ("SH:600000",)
    assert config.max_positions == 1
    assert config.ranking_value == "__single_security__"


def test_single_security_mode_requires_exactly_one_explicit_code() -> None:
    with pytest.raises(ValueError, match="只能指定一只股票"):
        PortfolioBacktestRequest.model_validate(
            _single_security_request(codes=["SH:600000", "SZ:000001"])
        )


def test_regular_portfolio_still_requires_a_ranking_value() -> None:
    payload = _single_security_request(single_security_mode=False)

    with pytest.raises(ValueError, match="排序指标不能为空"):
        PortfolioBacktestRequest.model_validate(payload)


def test_individual_research_defaults_to_ten_condition_candidates() -> None:
    request = IndividualResearchRequest(market="SH", code="600000")

    config = request.to_config(start_date=date(2021, 1, 1), end_date=date(2025, 12, 31))

    assert request.candidate_limit == 10
    assert config.candidate_limit == 10
    assert "technical.ma5" in config.value_ids


def test_individual_research_accepts_non_rankable_numeric_indicators() -> None:
    request = IndividualResearchRequest(
        market="SH",
        code="600000",
        value_ids=["technical.ma5"],
    )

    config = request.to_config(start_date=date(2021, 1, 1), end_date=date(2025, 12, 31))

    assert config.value_ids == ("technical.ma5",)


@pytest.mark.parametrize(
    ("validation_trades", "holdout_trades", "max_drawdown", "expected"),
    [
        (5, 3, 0.30, "样本较充分"),
        (4, 3, 0.30, "样本不足"),
        (5, 2, 0.30, "样本不足"),
        (5, 3, 0.3001, "风险高"),
        (0, 0, None, "样本不足"),
    ],
)
def test_candidate_quality_uses_sample_badges_without_filtering(
    validation_trades: int,
    holdout_trades: int,
    max_drawdown: float | None,
    expected: str,
) -> None:
    quality = candidate_quality(validation_trades, holdout_trades, max_drawdown)

    assert expected in quality["badges"]
    assert quality["eligible_for_display"] is True
