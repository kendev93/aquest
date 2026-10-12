from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from aquest.condition_research.samples import MiningSamples
from aquest.condition_research.sensitivity import (
    DEFAULT_SENSITIVITY_OFFSETS,
    SensitivityProfile,
    build_sensitivity,
    build_sensitivity_for_samples,
    sensitivity_dict,
)
from aquest.screening.models import ScreenCondition


def _samples(
    features: pd.DataFrame,
    outcomes: list[bool],
    *,
    groups: list[str] | None = None,
    instruments: list[str] | None = None,
) -> MiningSamples:
    count = len(outcomes)
    return MiningSamples(
        features=features,
        outcomes=pd.Series(outcomes, dtype=bool),
        instruments=pd.Series(instruments or [f"SH60000{i % 3}" for i in range(count)]),
        groups=pd.Series(groups or [f"g{i}" for i in range(count)]),
        dates=pd.Series(["2024-01-01"] * count),
        returns=pd.Series([0.01] * count),
    )


def _profile_for(
    condition: ScreenCondition,
    sample: MiningSamples,
    *,
    min_support: int = 2,
) -> SensitivityProfile:
    profile = build_sensitivity_for_samples(condition, [sample], min_support=min_support)
    assert profile is not None
    return profile


def test_base_point_reproduces_the_scored_threshold() -> None:
    """邻域表的基点必须和候选表里的数是同一个数，否则整条曲线没法读。"""

    values = np.arange(1.0, 21.0)
    sample = _samples(
        pd.DataFrame({"f.one": values}),
        [value > 10.0 for value in values],
        groups=[f"g{int(value)}" for value in values],
    )
    condition = ScreenCondition(left_value="f.one", operator="gte", right_constant=11.0)

    profile = _profile_for(condition, sample)

    base = next(point for point in profile.points if point.offset == 0.0)
    assert base.threshold == pytest.approx(11.0)
    assert base.support_count == 10
    assert base.outcome_rate == pytest.approx(1.0)
    assert base.baseline_rate == pytest.approx(0.5)
    assert base.lift == pytest.approx(2.0)
    # 分位是从阈值反推出来的：11.0 是这 20 个值里第 10 小的，下面正好一半。
    assert base.quantile == pytest.approx(0.5)


def test_offsets_move_the_cut_point_in_both_directions() -> None:
    values = np.arange(1.0, 21.0)
    sample = _samples(
        pd.DataFrame({"f.one": values}),
        [value > 10.0 for value in values],
        groups=[f"g{int(value)}" for value in values],
    )
    condition = ScreenCondition(left_value="f.one", operator="gte", right_constant=11.0)

    profile = _profile_for(condition, sample)
    thresholds = {point.offset: point.threshold for point in profile.points}

    # 负偏移=更宽松（阈值降、命中变多），正偏移=更严格。
    assert thresholds[-0.10] < thresholds[-0.05] < thresholds[0.0] < thresholds[0.05]
    assert thresholds[0.05] < thresholds[0.10]


def test_a_plateau_keeps_its_edge_across_the_neighbourhood() -> None:
    """胜率随特征值单调上升：切点往哪边挪都还在台地上。"""

    values = np.arange(1.0, 41.0)
    sample = _samples(
        pd.DataFrame({"f.one": values}),
        [value > 20.0 for value in values],
        groups=[f"g{int(value)}" for value in values],
    )
    condition = ScreenCondition(left_value="f.one", operator="gte", right_constant=21.0)

    profile = _profile_for(condition, sample)

    assert profile.stable_offsets == profile.evaluated_offsets
    assert profile.worst_retention is not None
    assert profile.worst_retention > 0.5


def test_a_cliff_loses_its_edge_as_soon_as_the_threshold_moves() -> None:
    """只有正中间那一段有效：挪一步优势就塌，再挪一步打反。"""

    values = np.arange(1.0, 41.0)
    sample = _samples(
        pd.DataFrame({"f.one": values}),
        [19.0 < value < 23.0 for value in values],
        groups=[f"g{int(value)}" for value in values],
    )
    condition = ScreenCondition(left_value="f.one", operator="gte", right_constant=20.0)

    profile = _profile_for(condition, sample)

    assert profile.worst_retention is not None
    assert profile.worst_retention < 0.0
    assert profile.stable_offsets < profile.evaluated_offsets


def test_pairwise_conditions_are_perturbed_on_their_margin() -> None:
    """`A > B` 没有常数可挪，扰动的是差值序列 `A − B` 的分位（基点为 0）。"""

    left = np.arange(1.0, 21.0)
    right = np.full(20, 10.0)
    sample = _samples(
        pd.DataFrame({"f.left": left, "f.right": right}),
        [value > 10.0 for value in left],
        groups=[f"g{int(value)}" for value in left],
    )
    condition = ScreenCondition(left_value="f.left", operator="gt", right_value="f.right")

    profile = _profile_for(condition, sample)
    base = next(point for point in profile.points if point.offset == 0.0)

    assert base.threshold == pytest.approx(0.0)
    # 正偏移要求更大的领先幅度，命中的样本随之减少。
    strict = next(point for point in profile.points if point.offset == 0.10)
    assert strict.threshold > base.threshold
    assert strict.support_count < base.support_count


def test_less_than_conditions_are_mirrored_not_reinterpreted() -> None:
    """`X ≤ c` 归约成 `−X ≥ −c`：正偏移同样是「更严格」，方向不会反过来。"""

    values = np.arange(1.0, 41.0)
    sample = _samples(
        pd.DataFrame({"f.one": values}),
        [value < 21.0 for value in values],
        groups=[f"g{int(value)}" for value in values],
    )
    condition = ScreenCondition(left_value="f.one", operator="lte", right_constant=20.0)

    profile = _profile_for(condition, sample)
    support = {point.offset: point.support_count for point in profile.points}

    # 正偏移收紧 ⇒ 命中变少；负偏移放宽 ⇒ 命中变多。
    assert support[0.10] < support[0.0] < support[-0.10]
    assert all(point.lift is not None and point.lift > 1.0 for point in profile.points)


def test_points_without_enough_support_are_excluded_not_counted_as_failures() -> None:
    """没样本不等于没通过：这些点要从分子分母里一起剔掉。"""

    values = np.arange(1.0, 61.0)
    sample = _samples(
        pd.DataFrame({"f.one": values}),
        [value > 30.0 for value in values],
        groups=[f"g{int(value)}" for value in values],
    )
    condition = ScreenCondition(left_value="f.one", operator="gte", right_constant=31.0)

    # min_support 抬到 30：最严格的两档只剩 27 / 24 个命中样本，不够。
    profile = _profile_for(condition, sample, min_support=30)

    strict = [point for point in profile.points if point.offset > 0.0]
    assert all(point.insufficient for point in strict)
    assert all(point.lift is None for point in strict)
    assert profile.skipped == 2
    assert profile.evaluated_offsets == 2
    assert profile.note and "未计入统计" in profile.note


def test_retention_is_none_when_the_base_point_has_no_edge() -> None:
    """基点没有优势时分母为 0，保持率给 None，不硬算。"""

    values = np.arange(1.0, 21.0)
    sample = _samples(
        pd.DataFrame({"f.one": values}),
        [value <= 10.0 for value in values],
        groups=[f"g{int(value)}" for value in values],
    )
    # 这条条件选中的正好是输家那半边，lift < 1，没有优势可谈保持率。
    condition = ScreenCondition(left_value="f.one", operator="gte", right_constant=11.0)

    profile = _profile_for(condition, sample)

    assert all(point.retention is None for point in profile.points)
    assert profile.median_retention is None
    assert profile.worst_retention is None


def test_conditions_without_a_scalar_parameter_have_no_profile() -> None:
    values = np.arange(1.0, 11.0)
    sample = _samples(pd.DataFrame({"f.one": values}), [True] * 5 + [False] * 5)

    crossing = ScreenCondition(left_value="f.one", operator="cross_up", right_value="f.two")
    assert build_sensitivity_for_samples(crossing, [sample], min_support=2) is None
    missing = ScreenCondition(left_value="f.absent", operator="gte", right_constant=1.0)
    assert build_sensitivity_for_samples(missing, [sample], min_support=2) is None


def test_profile_skips_empty_sample_sets() -> None:
    condition = ScreenCondition(left_value="f.one", operator="gte", right_constant=1.0)
    empty = _samples(pd.DataFrame({"f.one": []}), [])
    assert build_sensitivity_for_samples(condition, [empty], min_support=2) is None


def test_sensitivity_dict_round_trips_every_point() -> None:
    values = np.arange(1.0, 21.0)
    sample = _samples(
        pd.DataFrame({"f.one": values}),
        [value > 10.0 for value in values],
        groups=[f"g{int(value)}" for value in values],
    )
    condition = ScreenCondition(left_value="f.one", operator="gte", right_constant=11.0)
    profile = _profile_for(condition, sample)

    payload = sensitivity_dict(profile)
    assert payload is not None
    assert len(payload["points"]) == len(DEFAULT_SENSITIVITY_OFFSETS)
    assert payload["stable_offsets"] == profile.stable_offsets
    assert payload["median_retention"] == pytest.approx(profile.median_retention or 0.0)
    assert sensitivity_dict(None) is None
    assert set(payload["points"][0]) == {
        "offset",
        "quantile",
        "threshold",
        "support_count",
        "outcome_rate",
        "baseline_rate",
        "lift",
        "retention",
        "insufficient",
        "degenerate",
    }


def test_build_sensitivity_rejects_mismatched_sample_lengths() -> None:
    features = pd.DataFrame({"f.one": np.arange(1.0, 6.0)})
    condition = ScreenCondition(left_value="f.one", operator="gte", right_constant=3.0)
    assert (
        build_sensitivity(
            condition,
            features,
            pd.Series([True, False]),
            pd.Series(["SH600000", "SH600001"]),
            pd.Series(["g1", "g2"]),
            min_support=1,
        )
        is None
    )
