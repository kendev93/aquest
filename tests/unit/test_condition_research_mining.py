from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from selector_app.condition_research.mining import (
    build_top_n_context,
    equivalent_condition_signatures,
    mine_binary_conditions,
)
from selector_app.screening.models import ScreenCondition


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


def _condition_masks(features: pd.DataFrame, candidates: tuple) -> list[np.ndarray]:
    """Rebuild each candidate's sample mask so duplicates can be detected."""

    masks: list[np.ndarray] = []
    for candidate in candidates:
        condition = candidate.condition
        left = features[condition.left_value].to_numpy(dtype=float)
        if condition.right_value is not None:
            right = features[condition.right_value].to_numpy(dtype=float)
        else:
            right = np.full(len(left), float(condition.right_constant))
        if condition.operator == "gte":
            masks.append(left >= right)
        elif condition.operator == "lte":
            masks.append(left <= right)
        elif condition.operator == "gt":
            masks.append(left > right)
        else:
            masks.append(left < right)
    return masks


def test_mining_drops_comparisons_whose_scales_are_incompatible() -> None:
    features = pd.DataFrame(
        {
            "technical.ma5": [12.0, 14.0, 8.0, 9.0],
            "technical.ma20": [10.0, 12.0, 10.0, 11.0],
            "indicator_two.saturation": [0.001, 0.003, 0.002, 0.004],
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

    # The 0-1 saturation value is orders of magnitude away from a price moving
    # average, so a pairwise comparison between them only means "the average vs
    # ~0". Threshold conditions on the feature itself stay legitimate.
    assert all(
        candidate.condition.right_value is None
        or "indicator_two.saturation"
        not in (candidate.condition.left_value, candidate.condition.right_value)
        for candidate in candidates
    )
    assert any(
        candidate.condition.left_value == "technical.ma5"
        and candidate.condition.right_value == "technical.ma20"
        for candidate in candidates
    )


def test_mining_keeps_only_one_candidate_per_sample_mask() -> None:
    features = pd.DataFrame(
        {
            "indicator.x": [1.0, 2.0, 3.0, 4.0],
            "indicator.y": [1.0, 1.0, 1.0, 5.0],
            "indicator.z": [1.0, 1.0, 1.0, 4.0],
        }
    )
    winners = pd.Series([False, True, True, False])
    symbols = pd.Series(["A", "B", "C", "D"])

    candidates = mine_binary_conditions(
        features,
        winners,
        symbols,
        quantiles=(0.5,),
        min_support=2,
        min_instruments=2,
        limit=20,
    )

    masks = _condition_masks(features, candidates)
    assert candidates
    for index, mask in enumerate(masks):
        for other in masks[index + 1 :]:
            assert not np.array_equal(mask, other)


def test_mining_never_reproposes_a_condition_the_base_rule_already_applies() -> None:
    features = pd.DataFrame(
        {
            "indicator_one.main_force_raise": [5.0, 1.0, 5.0, 1.0],
            "indicator_three.var1": [1.0, 5.0, 1.0, 5.0],
        }
    )
    winners = pd.Series([True, False, True, False])
    symbols = pd.Series(["A", "B", "C", "D"])
    base_condition = ScreenCondition(
        left_value="indicator_one.main_force_raise",
        operator="gt",
        right_value="indicator_three.var1",
    )

    without_exclusion = mine_binary_conditions(
        features,
        winners,
        symbols,
        quantiles=(0.5,),
        min_support=2,
        min_instruments=2,
        limit=20,
    )
    excluded = frozenset(signature for signature in equivalent_condition_signatures(base_condition))
    with_exclusion = mine_binary_conditions(
        features,
        winners,
        symbols,
        quantiles=(0.5,),
        min_support=2,
        min_instruments=2,
        limit=20,
        excluded=excluded,
    )

    def touches_base_pair(candidate) -> bool:
        return {
            candidate.condition.left_value,
            candidate.condition.right_value,
        } == {"indicator_one.main_force_raise", "indicator_three.var1"}

    assert any(touches_base_pair(candidate) for candidate in without_exclusion)
    assert not any(touches_base_pair(candidate) for candidate in with_exclusion)


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


def _slot_frame() -> tuple[pd.DataFrame, pd.Series, pd.Series, pd.Series, pd.Series]:
    """3 天 × 4 只：每天排序值 1~4，排序值 ≥ 3 的盈利，行序打乱。"""

    rows = [
        {"date": f"2024-01-0{day + 1}", "score": float(score), "win": score >= 3}
        for day in range(3)
        for score in (1, 2, 3, 4)
    ]
    frame = pd.DataFrame(rows).sample(frac=1.0, random_state=3).reset_index(drop=True)
    return (
        frame[["score"]],
        frame["win"],
        frame["date"],
        pd.Series([f"SH60000{index}" for index in range(len(frame))]),
        pd.Series([f"g{index}" for index in range(len(frame))]),
    )


def _mine_with_slots(
    top_n: int,
) -> tuple[object, ...]:
    features, outcomes, dates, symbols, groups = _slot_frame()
    return tuple(
        mine_binary_conditions(
            features,
            outcomes,
            symbols,
            sample_groups=groups,
            min_support=3,
            min_instruments=2,
            limit=20,
            top_n_context=build_top_n_context(
                dates, features["score"], top_n=top_n, ascending=False
            ),
        )
    )


def test_top_n_diagnosis_scores_matches_as_a_slot_limited_pool() -> None:
    """前 N 名口径要能看出「这个条件在组合里更值钱」。

    命中 2/3/4 三条时全池只有 2/3 盈利，但每天排序第 1 名（score=4）全盈利 ——
    这正是「条件够宽、真正决定买谁的是排序」的场景。
    """

    candidates = _mine_with_slots(1)
    broad = next(item for item in candidates if item.support_count == 9)

    assert broad.outcome_rate == pytest.approx(2 / 3)
    assert broad.top_n_outcome_rate == pytest.approx(1.0)
    assert broad.top_n_support_count == 3
    assert broad.top_n_filtered_days == 3


def test_top_n_diagnosis_reports_when_the_slot_limit_never_binds() -> None:
    """每天命中数不超过槽位数时，前 N 就是全池，必须报 0 天而不是假装筛过。"""

    candidates = _mine_with_slots(1)
    narrow = next(item for item in candidates if item.support_count == 3)

    assert narrow.top_n_filtered_days == 0
    # 没有发生筛选时胜率自然等于全池口径，界面据此提示「排序未起作用」。
    assert narrow.top_n_outcome_rate == pytest.approx(narrow.outcome_rate)


def test_top_n_diagnosis_maps_compressed_indices_back_to_full_samples() -> None:
    """被挖特征含 NaN 时压缩下标与全量下标会错位，诊断必须映射回去。

    第一天特征全为 NaN（预热期/停牌常见），于是候选的 matched_indices 是
    压缩空间的下标，而 TopNContext 按全量样本构建。少了映射就会读到别的
    交易日的排序值，胜率和发生筛选的天数都会静默出错。
    """

    rows: list[dict[str, object]] = []
    for day in (1, 2, 3):
        for index in range(4):
            if day == 1:
                # 特征缺失，但排序值有值 —— 错位时正是这些行被误读。
                rows.append(
                    {
                        "date": f"2024-01-0{day}",
                        "score": None,
                        "rank": 400.0 - 100.0 * index,
                        "win": False,
                    }
                )
            elif day == 2:
                rows.append(
                    {
                        "date": f"2024-01-0{day}",
                        "score": float(index + 1),
                        "rank": float(10 * (index + 1)),
                        "win": index >= 2,
                    }
                )
            else:
                rows.append(
                    {
                        "date": f"2024-01-0{day}",
                        "score": float(index + 1),
                        "rank": float(10 * (4 - index)),
                        "win": index >= 1,
                    }
                )
    frame = pd.DataFrame(rows)
    candidates = mine_binary_conditions(
        frame[["score"]],
        frame["win"],
        pd.Series([f"SH60000{index}" for index in range(len(frame))]),
        sample_groups=pd.Series([f"g{index}" for index in range(len(frame))]),
        min_support=2,
        min_instruments=2,
        limit=20,
        top_n_context=build_top_n_context(frame["date"], frame["rank"], top_n=1, ascending=False),
    )

    broad = next(item for item in candidates if item.support_count == 6)
    assert broad.outcome_rate == pytest.approx(5 / 6)
    # 每天第 1 名都盈利；错位实现会给出 0.5。
    assert broad.top_n_outcome_rate == pytest.approx(1.0)
    assert broad.top_n_support_count == 2
    assert broad.top_n_filtered_days == 2


def test_top_n_diagnosis_is_absent_without_a_ranking_value() -> None:
    """没解析出排序值就不该编一个，字段保持为空由界面决定要不要显示。"""

    features, outcomes, _, symbols, groups = _slot_frame()
    candidates = mine_binary_conditions(
        features,
        outcomes,
        symbols,
        sample_groups=groups,
        min_support=3,
        min_instruments=2,
        limit=1,
    )

    assert candidates[0].top_n_outcome_rate is None
    assert candidates[0].top_n_support_count == 0
    assert candidates[0].top_n_filtered_days == 0


def _permutation_frame(
    *,
    strength: float,
    seed: int = 20260918,
) -> tuple[pd.DataFrame, pd.Series, pd.Series, pd.Series]:
    """3,000 个决策样本，第 0 号特征高分段的胜率更高。

    ``strength=0`` 时标签与特征完全无关，是纯噪声基线。
    """

    stocks = 300
    rows = 3_000
    rng = np.random.default_rng(seed)
    codes = np.repeat(np.arange(stocks), rows // stocks)
    trades = np.arange(rows) // 2
    driver = rng.normal(size=rows)
    cut = np.quantile(driver, 0.7)
    probability = np.where(driver >= cut, 0.42 + strength, 0.42)
    features = pd.DataFrame(
        {"technical.f0": driver, **{f"technical.f{i}": rng.normal(size=rows) for i in range(1, 8)}}
    )
    outcomes = pd.Series(rng.random(rows) < probability)
    symbols = pd.Series([f"SH{code:06d}" for code in codes])
    groups = pd.Series([f"SH{code:06d}:{trade}" for code, trade in zip(codes, trades)])
    return features, outcomes, symbols, groups


def _mine_with_permutations(strength: float, *, permutations: int = 20) -> tuple:
    features, outcomes, symbols, groups = _permutation_frame(strength=strength)
    candidates = mine_binary_conditions(
        features,
        outcomes,
        symbols,
        sample_groups=groups,
        min_support=20,
        min_instruments=5,
        limit=5,
        permutations=permutations,
    )
    return tuple(candidates)


def test_permutation_threshold_rejects_pure_noise() -> None:
    """纯噪声里最好的那个条件不该被当成发现。"""

    candidates = _mine_with_permutations(0.0)
    assert candidates
    best = max(candidates, key=lambda item: item.lift)
    assert best.lift_threshold is not None
    assert best.lift > 1.0
    assert best.significant is False
    assert best.permutation_pvalue is not None
    assert best.permutation_pvalue > 0.05


def test_permutation_threshold_accepts_a_strong_signal() -> None:
    candidates = _mine_with_permutations(0.2)
    assert candidates
    best = max(candidates, key=lambda item: item.lift)
    assert best.condition.left_value == "technical.f0"
    assert best.significant is True
    assert best.permutation_pvalue is not None
    assert best.permutation_pvalue <= 0.05


def test_permutation_threshold_is_deterministic() -> None:
    """打乱必须用固定种子，否则研究本身的可复现性就没了。"""

    first = _mine_with_permutations(0.1)
    second = _mine_with_permutations(0.1)
    assert [item.lift_threshold for item in first] == [item.lift_threshold for item in second]
    assert [item.permutation_pvalue for item in first] == [
        item.permutation_pvalue for item in second
    ]


def test_significance_fields_are_absent_without_a_permutation_test() -> None:
    candidates = _mine_with_permutations(0.2, permutations=0)
    assert candidates
    best = max(candidates, key=lambda item: item.lift)
    assert best.lift_threshold is None
    assert best.permutation_pvalue is None
    assert best.significant is None
