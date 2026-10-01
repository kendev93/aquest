from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from selector_app.condition_research.mining import (
    _bits,
    _jaccard,
    _phi_correlation,
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


def test_single_security_mode_can_mine_price_scaled_values() -> None:
    features = pd.DataFrame({"technical.ma5": np.arange(1.0, 9.0)})
    winners = pd.Series([False, False, False, False, True, True, True, True])
    symbols = pd.Series(["SH600000"] * 8)
    query = {
        "quantiles": (0.5,),
        "min_support": 3,
        "min_instruments": 1,
        "limit": 10,
    }

    cross_sectional = mine_binary_conditions(features, winners, symbols, **query)
    single_security = mine_binary_conditions(
        features,
        winners,
        symbols,
        allow_price_scale_features=True,
        **query,
    )

    assert cross_sectional == ()
    assert any(item.condition.left_value == "technical.ma5" for item in single_security)


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


# --- 同义条件合并 -----------------------------------------------------------


def _duplicate_frame() -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """一个单调递增的特征：高分段的样本全部盈利，低分段全部亏损。

    默认分位会给出 4 个嵌套阈值（命中前 80% / 60% / 40% / 20%），正是
    「同一个发现的四个写法」——合并逻辑要收敛它们，而不是全列出来。
    """

    size = 200
    values = np.arange(size, dtype=float)
    return (
        pd.DataFrame({"technical.f0": values}),
        pd.Series(values >= size / 2),
        pd.Series([f"SH60000{index % 5}" for index in range(size)]),
    )


def _mine_duplicates(*, duplicate_correlation: float, limit: int = 5) -> tuple:
    features, outcomes, symbols = _duplicate_frame()
    return mine_binary_conditions(
        features,
        outcomes,
        symbols,
        min_support=20,
        min_instruments=3,
        limit=limit,
        duplicate_correlation=duplicate_correlation,
    )


def test_phi_correlation_matches_the_closed_form_of_nested_masks() -> None:
    """嵌套掩码可以解析地算：√(p(1−q) / q(1−p))，用它把实现锚住。"""

    valid = np.ones(100, dtype=bool)
    bits = _bits(valid)
    # 外层命中 40%、内层命中 20% ⇒ φ = √(0.2·0.6 / 0.4·0.8) = 0.612
    outer = np.arange(40)
    inner = np.arange(20)
    value = _phi_correlation(
        inner, outer, valid=valid, other_valid=valid, valid_bits=bits, other_valid_bits=bits
    )
    assert value == pytest.approx(0.612, abs=1e-3)
    assert _phi_correlation(
        inner, inner, valid=valid, other_valid=valid, valid_bits=bits, other_valid_bits=bits
    ) == pytest.approx(1.0)


def test_phi_correlation_is_negative_for_complementary_conditions() -> None:
    """互补条件是相反的发现（ρ = −1），绝不能被当成同义合并掉。"""

    valid = np.ones(100, dtype=bool)
    bits = _bits(valid)
    value = _phi_correlation(
        np.arange(50),
        np.arange(50, 100),
        valid=valid,
        other_valid=valid,
        valid_bits=bits,
        other_valid_bits=bits,
    )
    assert value == pytest.approx(-1.0)


def test_phi_correlation_is_restricted_to_samples_both_can_evaluate() -> None:
    """两条条件的可评估样本不同，φ 只能在交集上算，否则会白送命中。"""

    everything = np.ones(100, dtype=bool)
    first_half = np.zeros(100, dtype=bool)
    first_half[:50] = True
    # 交集只有前 50 个样本：A 命中其中 40 个，B 命中其中 30 个且全在 A 里。
    value = _phi_correlation(
        np.arange(40),
        np.arange(30),
        valid=everything,
        other_valid=first_half,
        valid_bits=_bits(everything),
        other_valid_bits=_bits(first_half),
    )
    assert value == pytest.approx(0.612, abs=1e-3)


def test_phi_never_exceeds_the_square_root_of_the_jaccard() -> None:
    """预筛靠的就是这条上界：φ ≤ √j。

    随机列联表扫一遍，别让这个界只在我手写的例子上成立。上界取 √j 而不是
    2j/(1+j)：后者是「两个集合大小必须相等」时才有的形状，比真值严，会把嵌套
    的同义条件剪掉。
    """

    rng = np.random.default_rng(20240918)
    for _ in range(2000):
        both, only_left, only_right, neither = (int(value) for value in rng.integers(0, 30, 4))
        total = both + only_left + only_right + neither
        if total == 0:
            continue
        left = np.arange(both + only_left)
        right = np.concatenate(
            [np.arange(both), np.arange(both + only_left, both + only_left + only_right)]
        )
        valid = np.ones(total, dtype=bool)
        value = _phi_correlation(
            left,
            right,
            valid=valid,
            other_valid=valid,
            valid_bits=_bits(valid),
            other_valid_bits=_bits(valid),
        )
        overlap = _jaccard(left, right, valid=valid, other_valid=valid)
        if value is None:
            continue
        assert value <= np.sqrt(overlap) + 1e-9


def test_nested_conditions_survive_the_jaccard_prefilter() -> None:
    """嵌套（更严的阈值完全落在宽松阈值里）是同义条件最常见的形状。

    它的 Jaccard 只有 0.4，而 φ 是 0.61 —— 旧上界 t/(2−t)=0.4286 会把它剪掉，
    漏掉一次合并；改成 t²=0.36 才剪得对。
    """

    valid = np.ones(1000, dtype=bool)
    bits = _bits(valid)
    wide = np.arange(100)
    strict = np.arange(40)

    overlap = _jaccard(strict, wide, valid=valid, other_valid=valid)
    assert overlap == pytest.approx(0.4)
    assert overlap >= 0.6**2

    value = _phi_correlation(
        strict, wide, valid=valid, other_valid=valid, valid_bits=bits, other_valid_bits=bits
    )
    assert value is not None and value >= 0.6


def test_strict_and_loose_thresholds_of_one_finding_are_merged() -> None:
    """同一条发现的两个激进度（94% 分位 ⊂ 85% 分位）必须被合并掉。

    它们的 Jaccard 恰好 0.4、φ 约 0.60：刚好够 0.6 的门槛，却低于旧上界
    t/(2−t)=0.4286。用旧上界这一对会被剪掉、当成两条独立发现（candidates 有两条、
    duplicates 为空）——同义合并漏一次，候选列表就多一条重复。
    """

    size = 1000
    features = pd.DataFrame({"technical.f0": np.arange(size, dtype=float)})
    outcomes = pd.Series(np.arange(size) >= 900)
    symbols = pd.Series([f"SH60000{index % 5}" for index in range(size)])

    candidates = mine_binary_conditions(
        features,
        outcomes,
        symbols,
        quantiles=(0.85, 0.94),
        min_support=20,
        min_instruments=3,
        limit=5,
    )

    # 更严的那条（f0 >= 939）排在最前，宽松的那条被它吸收。
    assert [round(item.condition.right_constant) for item in candidates] == [939]
    assert len(candidates[0].duplicates) == 1
    assert candidates[0].duplicates[0].correlation == pytest.approx(0.601, abs=5e-3)


def test_jaccard_is_taken_on_samples_both_can_evaluate() -> None:
    """两条条件的可评估样本不同：Jaccard 只能在交集上取，否则上界失效。

    B 只能评估前 50 行，A 的命中有一半落在 B 评估不了的区间里。把那些行算进并
    集，Jaccard 会从 0.5 被稀释到 0.25，直接跌破 t² —— 明明是同义的一对被剪掉。
    """

    everything = np.ones(100, dtype=bool)
    first_half = np.zeros(100, dtype=bool)
    first_half[:50] = True
    wide = np.concatenate([np.arange(40), np.arange(60, 100)])
    strict = np.arange(20)

    assert _jaccard(wide, strict, valid=everything, other_valid=first_half) == pytest.approx(0.5)


def test_phi_correlation_is_none_when_it_cannot_be_computed() -> None:
    """并集覆盖全部可评估样本时列联表退化：返回 None，不猜一个数。"""

    valid = np.ones(100, dtype=bool)
    bits = _bits(valid)
    assert (
        _phi_correlation(
            np.arange(100),
            np.arange(50),
            valid=valid,
            other_valid=valid,
            valid_bits=bits,
            other_valid_bits=bits,
        )
        is None
    )


def test_adjacent_thresholds_of_one_finding_are_merged_and_listed() -> None:
    candidates = _mine_duplicates(duplicate_correlation=0.6)

    assert [item.condition.right_constant for item in candidates] == pytest.approx(
        [119.4, 39.8], abs=0.05
    )
    merged = candidates[0].duplicates
    assert len(merged) == 2
    # 相邻分位阈值：40%/20% 与 60%/40% 分别约 0.667 与 0.612。
    assert sorted(item.correlation for item in merged) == pytest.approx([0.612, 0.667], abs=1e-3)
    assert {round(item.support_count) for item in merged} == {40, 120}
    # 被合并掉的条件必须留痕：条件文本 + lift 都在，用户能自己判断门槛合不合适。
    assert all(item.lift > 1.0 for item in merged)
    assert candidates[-1].duplicates == ()


def test_thresholds_two_quantiles_apart_stay_separate() -> None:
    """相隔一个分位的相关性只有 ~0.41：那是不同的激进度，不是重复。"""

    candidates = _mine_duplicates(duplicate_correlation=0.6)

    assert len(candidates) == 2
    assert candidates[1].condition.right_constant == pytest.approx(39.8, abs=0.05)


def test_merging_frees_the_slot_for_a_different_condition() -> None:
    """合并不是"少给几条"，空出的名额由更靠后的不同义条件顶上。"""

    merged = _mine_duplicates(duplicate_correlation=0.6, limit=2)
    unmerged = _mine_duplicates(duplicate_correlation=1.0, limit=2)

    assert [item.condition.right_constant for item in unmerged] == pytest.approx(
        [119.4, 159.2], abs=0.05
    )
    # 第二名从"同一个发现的另一个阈值"换成了一个真正不同的切点。
    assert merged[1].condition.right_constant == pytest.approx(39.8, abs=0.05)


def test_a_correlation_of_one_keeps_repeats_but_still_merges_identical_masks() -> None:
    """门槛调到 1 ≈ 关闭：重复写法会保留下来，由用户自己看。"""

    candidates = _mine_duplicates(duplicate_correlation=1.0)

    assert len(candidates) == 4
    assert all(item.duplicates == () for item in candidates)


def test_duplicate_correlation_must_stay_in_a_meaningful_range() -> None:
    features, outcomes, symbols = _duplicate_frame()
    for value in (0.0, 0.49, 1.01, 2.0):
        with pytest.raises(ValueError, match="同义条件合并门槛"):
            mine_binary_conditions(
                features,
                outcomes,
                symbols,
                min_support=20,
                min_instruments=3,
                limit=5,
                duplicate_correlation=value,
            )


def _cross_frames() -> tuple[pd.DataFrame, pd.DataFrame, pd.Series, pd.Series]:
    """两样本上穿、两样本下穿、四样本一直在一侧，用来看穿越条件挖不挖得出来。"""

    features = pd.DataFrame(
        {
            "technical.ma5": [11.0, 11.0, 10.0, 10.0, 12.0, 12.0, 9.0, 9.0],
            "technical.ma20": [10.0, 10.0, 11.0, 11.0, 10.0, 10.0, 10.0, 10.0],
        }
    )
    previous = pd.DataFrame(
        {
            "technical.ma5": [9.0, 9.0, 12.0, 12.0, 13.0, 13.0, 8.0, 8.0],
            "technical.ma20": [10.0, 10.0, 11.0, 11.0, 10.0, 10.0, 10.0, 10.0],
        }
    )
    # 上穿两条全盈利，下穿两条全亏，另外四条一直站在同一侧（两盈两亏）——
    # 这样即使把上穿样本整条排除，剩下的窗口里仍有盈有亏，基准还算得出来。
    outcomes = pd.Series([True, True, False, False, True, True, False, False])
    symbols = pd.Series(["SH600000", "SZ000001"] * 4)
    return features, previous, outcomes, symbols


def test_mines_cross_conditions_when_previous_day_features_are_available() -> None:
    """「MA5 上穿 MA20」这类时点条件：命中 2 条，两条都盈利。

    只给大小比较的话，同样的命中里混着两条一直站在上面的样本，胜率被拉到 1/2。
    """

    features, previous, outcomes, symbols = _cross_frames()

    candidates = mine_binary_conditions(
        features,
        outcomes,
        symbols,
        quantiles=(0.5,),
        min_support=2,
        min_instruments=2,
        limit=20,
        previous_features=previous,
    )

    cross_up = next(
        candidate
        for candidate in candidates
        if candidate.condition.operator == "cross_up"
        and candidate.condition.left_value == "technical.ma5"
    )
    assert cross_up.condition.right_value == "technical.ma20"
    assert cross_up.outcome_rate == pytest.approx(1.0)
    assert cross_up.support_count == 2
    assert cross_up.instrument_count == 2
    assert cross_up.baseline_rate == pytest.approx(0.5)
    assert cross_up.lift == pytest.approx(2.0)


def test_cross_conditions_are_skipped_without_previous_day_features() -> None:
    """没有昨天的值就绝不猜：宁可少一类候选，也不能拿错位的行编造穿越。"""

    features, _previous, outcomes, symbols = _cross_frames()

    candidates = mine_binary_conditions(
        features,
        outcomes,
        symbols,
        quantiles=(0.5,),
        min_support=2,
        min_instruments=2,
        limit=20,
    )

    assert not any(candidate.condition.operator.startswith("cross") for candidate in candidates)


def test_cross_conditions_ignore_samples_without_a_previous_day() -> None:
    """上穿样本的前一天取不到时整条退出：挖不出来就是挖不出来，不能蒙一个。

    窗口里还有盈有亏（基准算得出来），所以「没有候选」只能是因为那两条样本
    被排除了，不是因为基准退化成 0 被跳过。
    """

    features, previous, outcomes, symbols = _cross_frames()
    previous.loc[[0, 1], "technical.ma5"] = float("nan")

    candidates = mine_binary_conditions(
        features,
        outcomes,
        symbols,
        quantiles=(0.5,),
        min_support=2,
        min_instruments=2,
        limit=20,
        previous_features=previous,
    )

    assert candidates  # 窗口本身仍然可用，缺的只是那两条样本
    assert not any(
        candidate.condition.operator == "cross_up"
        and candidate.condition.left_value == "technical.ma5"
        for candidate in candidates
    )
