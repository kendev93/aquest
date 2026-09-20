from __future__ import annotations

from datetime import date

import pytest

from selector_app.condition_research.models import ConditionResearchConfig
from selector_app.screening.rules import RuleGroup


def config(**overrides: object) -> ConditionResearchConfig:
    values: dict[str, object] = {
        "vipdoc_path": "/tmp/vipdoc",
        "universe": "all",
        "buy_rule": RuleGroup(signals=("custom.buy",)),
        "sell_rule": RuleGroup(signals=("custom.sell",)),
        "start_date": date(2020, 1, 1),
        "end_date": date(2024, 12, 31),
    }
    values.update(overrides)
    return ConditionResearchConfig(**values)


def test_config_defaults_to_separate_buy_and_sell_search_without_portfolio_limits() -> None:
    research = config()

    assert research.search_sides == "both"
    assert research.min_trades == 20
    assert research.min_instruments == 5
    assert research.position_mode == "full"
    assert not hasattr(research, "max_positions")
    assert research.instrument_types == ("stock",)


def test_stock_research_keeps_the_selected_board_filters() -> None:
    research = config(instrument_types=("stock",), boards=("main", "star"))

    assert research.instrument_types == ("stock",)
    assert research.boards == ("main", "star")


@pytest.mark.parametrize(
    ("instrument_types", "message"),
    [
        (("index",), "只支持股票"),
        (("bond",), "只支持股票"),
        (("fund",), "只支持股票"),
    ],
)
def test_config_rejects_unsupported_instrument_types(
    instrument_types: tuple[str, ...], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        config(instrument_types=instrument_types)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"start_date": date(2025, 1, 1), "end_date": date(2024, 1, 1)}, "开始日期"),
        ({"start_date": date(2025, 1, 1), "end_date": date(2025, 1, 2)}, "至少需要 3 天"),
        ({"train_ratio": 0.9, "validation_ratio": 0.2}, "比例"),
        ({"candidate_limit": 0}, "候选"),
        ({"min_instruments": 0}, "覆盖"),
        ({"search_sides": "portfolio"}, "搜索范围"),
    ],
)
def test_config_rejects_invalid_search_bounds(overrides: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        config(**overrides)
