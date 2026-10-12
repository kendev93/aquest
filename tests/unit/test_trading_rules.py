from __future__ import annotations

import numpy as np
import pytest

from aquest.formulas.registry import FORMULA_REGISTRY
from aquest.formulas.types import FormulaResult
from aquest.screening.engine import combine_masks, evaluate_rule_group
from aquest.screening.models import ScreenCondition
from aquest.screening.rules import RuleGroup


def result() -> dict[str, FormulaResult]:
    return {
        "custom": FormulaResult.build(
            formula_id="custom",
            display_name="测试公式",
            minimum_bars=1,
            recommended_bars=1,
            data_length=4,
            values={"fast": [1.0, 3.0, 4.0, 5.0], "slow": [2.0, 2.0, 3.0, 4.0]},
            signals={
                "custom.buy_a": [True, False, True, False],
                "custom.buy_b": [False, True, True, False],
                "custom.sell": [False, False, False, True],
            },
        )
    }


def test_rule_group_supports_signal_and_numeric_conditions() -> None:
    rule = RuleGroup(
        signals=("custom.buy_a",),
        conditions=(
            ScreenCondition(
                left_value="custom.fast",
                operator="gt",
                right_value="custom.slow",
            ),
        ),
        combine_mode="all",
    )

    mask = evaluate_rule_group(rule, result(), FORMULA_REGISTRY, custom=True)

    assert mask.tolist() == [False, False, True, False]


def test_rule_group_supports_constant_comparison_and_crossing() -> None:
    constant_rule = RuleGroup(
        conditions=(
            ScreenCondition(
                left_value="custom.fast",
                operator="gte",
                right_constant=3.0,
            ),
        )
    )
    cross_rule = RuleGroup(
        conditions=(
            ScreenCondition(
                left_value="custom.fast",
                operator="cross_up",
                right_value="custom.slow",
            ),
        )
    )

    assert evaluate_rule_group(constant_rule, result(), FORMULA_REGISTRY, custom=True).tolist() == [
        False,
        True,
        True,
        True,
    ]
    assert evaluate_rule_group(cross_rule, result(), FORMULA_REGISTRY, custom=True).tolist() == [
        False,
        True,
        False,
        False,
    ]


def test_rule_group_supports_any_and_at_least_modes() -> None:
    masks = [
        np.array([True, False, True]),
        np.array([False, True, True]),
        np.array([False, False, True]),
    ]

    assert combine_masks(masks, "all", None).tolist() == [False, False, True]
    assert combine_masks(masks, "any", None).tolist() == [True, True, True]
    assert combine_masks(masks, "at_least", 2).tolist() == [False, False, True]


def test_rule_group_requires_a_valid_combination() -> None:
    with pytest.raises(ValueError, match="至少需要一个"):
        RuleGroup()
    with pytest.raises(ValueError, match="minimum_matches"):
        RuleGroup(signals=("custom.buy_a",), combine_mode="at_least", minimum_matches=2)
    with pytest.raises(ValueError, match="历史长度不一致"):
        combine_masks([np.array([True]), np.array([True, False])], "all", None)
