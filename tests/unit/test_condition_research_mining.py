from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from selector_app.condition_research.mining import mine_binary_conditions


def test_mines_a_threshold_that_enriches_for_profitable_trades() -> None:
    features = pd.DataFrame({"technical.rsi14": np.arange(1.0, 9.0)})
    winners = pd.Series([False, False, False, False, True, True, True, True])
    symbols = pd.Series(["SH600000", "SZ000001"] * 4)

    candidates = mine_binary_conditions(
        features,
        winners,
        symbols,
        quantiles=(0.5,),
        min_support=3,
        min_instruments=2,
        limit=10,
    )

    best = next(candidate for candidate in candidates if candidate.condition.operator == "gte")
    assert best.condition.left_value == "technical.rsi14"
    assert best.condition.right_constant == pytest.approx(4.5)
    assert best.support_count == 4
    assert best.instrument_count == 2
    assert best.outcome_rate == pytest.approx(1.0)
    assert best.baseline_rate == pytest.approx(0.5)
    assert best.lift == pytest.approx(2.0)


def test_mines_scale_free_comparisons_between_indicators() -> None:
    features = pd.DataFrame(
        {
            "technical.ma5": [12.0, 14.0, 8.0, 9.0],
            "technical.ma20": [10.0, 12.0, 10.0, 11.0],
        }
    )
    winners = pd.Series([True, True, False, False])
    symbols = pd.Series(["SH600000", "SZ000001", "SH600000", "SZ000001"])

    candidates = mine_binary_conditions(
        features,
        winners,
        symbols,
        quantiles=(0.5,),
        min_support=2,
        min_instruments=2,
        limit=20,
    )

    moving_average_cross = next(
        candidate
        for candidate in candidates
        if candidate.condition.left_value == "technical.ma5"
        and candidate.condition.operator == "gt"
        and candidate.condition.right_value == "technical.ma20"
    )
    assert moving_average_cross.outcome_rate == pytest.approx(1.0)
    assert moving_average_cross.baseline_rate == pytest.approx(0.5)
    assert all(
        candidate.condition.right_constant is None
        for candidate in candidates
        if candidate.condition.left_value in {"technical.ma5", "technical.ma20"}
    )


def test_mining_respects_minimum_trade_support_and_stock_coverage() -> None:
    features = pd.DataFrame({"indicator_one.strength": np.arange(1.0, 9.0)})
    winners = pd.Series([False, False, False, False, True, True, True, True])
    one_symbol = pd.Series(["SH600000"] * 8)

    too_few_trades = mine_binary_conditions(
        features,
        winners,
        one_symbol,
        quantiles=(0.75,),
        min_support=3,
        min_instruments=1,
        limit=10,
    )
    too_few_symbols = mine_binary_conditions(
        features,
        winners,
        one_symbol,
        quantiles=(0.5,),
        min_support=3,
        min_instruments=2,
        limit=10,
    )

    assert too_few_trades == ()
    assert too_few_symbols == ()


def test_mining_ignores_non_finite_feature_rows_and_constant_features() -> None:
    features = pd.DataFrame(
        {
            "indicator_one.constant": [1.0] * 8,
            "indicator_two.score": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, np.nan],
        }
    )
    winners = pd.Series([False, False, False, False, True, True, True, True])
    symbols = pd.Series(["A", "A", "A", "A", "B", "B", "B", "B"])

    candidates = mine_binary_conditions(
        features,
        winners,
        symbols,
        quantiles=(0.5,),
        min_support=3,
        min_instruments=2,
        limit=10,
    )

    assert candidates
    assert all(candidate.condition.left_value == "indicator_two.score" for candidate in candidates)
    assert all(candidate.support_count <= 6 for candidate in candidates)


def test_mining_returns_no_rules_when_outcome_has_no_positive_examples() -> None:
    candidates = mine_binary_conditions(
        pd.DataFrame({"indicator.value": [1.0, 2.0, 3.0, 4.0]}),
        pd.Series([False, False, False, False]),
        pd.Series(["A", "A", "B", "B"]),
        quantiles=(0.5,),
        min_support=1,
        min_instruments=1,
        limit=10,
    )

    assert candidates == ()


def test_sell_samples_count_a_trade_once_even_when_it_has_many_holding_days() -> None:
    features = pd.DataFrame({"technical.rsi14": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]})
    losing_trade = [True] * 4
    winning_trade = [False] * 4

    candidates = mine_binary_conditions(
        features,
        pd.Series(losing_trade + winning_trade),
        pd.Series(["SH600000"] * 4 + ["SZ000001"] * 4),
        sample_groups=pd.Series(["loss-1"] * 4 + ["win-1"] * 4),
        quantiles=(0.5,),
        min_support=2,
        min_instruments=1,
        limit=10,
    )

    assert candidates == ()


def test_mining_rejects_misaligned_sample_columns() -> None:
    with pytest.raises(ValueError, match="样本数量"):
        mine_binary_conditions(
            pd.DataFrame({"indicator.value": [1.0, 2.0]}),
            pd.Series([True]),
            pd.Series(["A", "B"]),
            min_support=1,
            min_instruments=1,
        )


def test_baseline_rate_weights_every_sample_group_equally() -> None:
    """基准胜率按每组等权（2/3），而不是按样本池化（1/2）。

    组内结局混合且各组样本数不同时，两种口径才会分开；只按样本池化会让
    多决策点的交易主导基准，进而放宽候选条件的准入门槛。
    """

    features = pd.DataFrame({"technical.rsi14": [8.0, 1.0, 2.0, 3.0]})
    outcomes = pd.Series([True, True, False, False])
    instruments = pd.Series(["SH600000", "SH600000", "SZ000001", "SZ000001"])
    # A 组 1 个样本（盈利）；B 组 3 个样本（1 盈 2 亏）。
    groups = pd.Series(["A", "B", "B", "B"])

    candidates = mine_binary_conditions(
        features,
        outcomes,
        instruments,
        sample_groups=groups,
        quantiles=(0.5,),
        min_support=1,
        min_instruments=1,
        limit=10,
    )

    assert len(candidates) == 1
    best = candidates[0]
    assert best.condition.operator == "lte"
    assert best.condition.right_constant == pytest.approx(2.5)
    assert best.baseline_rate == pytest.approx((1.0 + 1 / 3) / 2)
    assert best.baseline_rate != pytest.approx(0.5)
