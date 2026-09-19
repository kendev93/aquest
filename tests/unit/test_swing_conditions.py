"""Tests for turning swing findings into condition-research rules."""

from __future__ import annotations

import pytest

from selector_app.swing_research.conditions import (
    LABEL_SIDES,
    TRADEABLE_LABELS,
    build_condition_plan,
    build_templates,
)
from selector_app.swing_research.scoring import IndicatorScore


def _score(
    value_id: str = "technical.rsi14",
    label: str = "up_onset_ahead",
    order: str = "low",
    *,
    ic_mean: float = 0.2,
    significant: bool = True,
    threshold: float | None = 42.0,
    display_name: str = "RSI14",
) -> IndicatorScore:
    return IndicatorScore(
        value_id=value_id,
        display_name=display_name,
        label=label,
        order=order,
        ic_mean=ic_mean,
        icir=1.5,
        ic_days=600,
        ic_threshold=0.02,
        top_lift=1.4,
        threshold_hint=threshold,
        pvalue=0.001,
        qvalue=0.002,
        significant=significant,
        note="",
    )


def test_every_label_maps_to_a_side() -> None:
    assert LABEL_SIDES["up_onset_ahead"] == "buy"
    assert LABEL_SIDES["down_onset_ahead"] == "sell"
    assert LABEL_SIDES["up_leg_start"] == "buy"
    assert LABEL_SIDES["down_leg_start"] == "sell"


def test_onset_labels_are_tradeable_and_attribution_is_not() -> None:
    assert "up_onset_ahead" in TRADEABLE_LABELS
    assert "down_onset_ahead" in TRADEABLE_LABELS
    assert "up_leg_start" not in TRADEABLE_LABELS
    assert "down_leg_start" not in TRADEABLE_LABELS


def test_low_order_becomes_lte_and_high_order_becomes_gte() -> None:
    low = build_templates([_score(order="low")])[0]
    high = build_templates([_score(order="high")])[0]

    assert low.operator == "lte"
    assert high.operator == "gte"
    assert low.threshold == 42.0


def test_condition_and_rule_carry_the_threshold() -> None:
    template = build_templates([_score(order="low", threshold=31.5)])[0]

    assert template.condition == {
        "left_value": "technical.rsi14",
        "operator": "lte",
        "right_value": None,
        "right_constant": 31.5,
    }
    assert template.rule["conditions"] == [template.condition]
    assert template.rule["combine_mode"] == "all"


def test_findings_without_a_threshold_are_skipped() -> None:
    assert build_templates([_score(threshold=None)]) == []


def test_down_onset_becomes_a_sell_rule() -> None:
    plan = build_condition_plan([_score(label="down_onset_ahead", order="high", threshold=68.0)])

    assert plan["buy_rule"] is None
    assert plan["sell_rule"]["conditions"][0]["operator"] == "gte"
    assert plan["sell_rule"]["conditions"][0]["right_constant"] == 68.0


def test_tradeable_label_wins_over_attribution() -> None:
    """Given both, the rule must come from the label observable in real time."""

    plan = build_condition_plan(
        [
            _score(label="up_leg_start", order="low", ic_mean=0.35, threshold=20.0),
            _score(label="up_onset_ahead", order="low", ic_mean=0.12, threshold=40.0),
        ]
    )

    assert plan["buy_rule"]["conditions"][0]["right_constant"] == 40.0


def test_insignificant_findings_are_never_recommended() -> None:
    """A large IC that failed the noise floor is the false positive to avoid."""

    plan = build_condition_plan([_score(ic_mean=0.9, significant=False, threshold=10.0)])

    assert plan["buy_rule"] is None
    assert plan["sell_rule"] is None
    assert plan["reason"]


def test_attribution_note_warns_it_cannot_be_traded() -> None:
    template = build_templates([_score(label="up_leg_start", order="low")])[0]

    assert template.tradeable is False
    assert "无法确认" in template.note


def test_ranking_hint_follows_the_buy_side_direction() -> None:
    plan = build_condition_plan([_score(order="low", threshold=30.0)])

    assert plan["ranking"]["value"] == "technical.rsi14"
    assert plan["ranking"]["order"] == "asc"

    high = build_condition_plan([_score(label="up_onset_ahead", order="high", threshold=70.0)])
    assert high["ranking"]["order"] == "desc"


def test_ranking_hint_defers_to_the_research_own_scoring() -> None:
    plan = build_condition_plan([_score()])

    assert "实际收益率" in plan["ranking"]["note"]


def test_empty_input_reports_a_reason() -> None:
    plan = build_condition_plan([])

    assert plan["templates"] == []
    assert plan["reason"]
    assert plan["ranking"] is None


def test_thresholds_are_rounded_for_readability() -> None:
    template = build_templates([_score(threshold=42.123456789)])[0]

    assert template.threshold == 42.1235


@pytest.mark.parametrize("label", ["up_onset_ahead", "down_onset_ahead"])
def test_tradeable_count_only_counts_onset_labels(label: str) -> None:
    plan = build_condition_plan([_score(label=label), _score(label="up_leg_start")])

    assert plan["tradeable_count"] == 1
