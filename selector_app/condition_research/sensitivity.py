"""参数敏感性：挖出来的阈值是站在台地上还是悬崖尖上。

条件研究最后交出来的是「某个指标在某个切点上」这一句话，其中最脆弱的是那个切点：
它可能真的抓住了一段区间，也可能只是碰巧最好的一个数。把切点往两边挪一点再看
优势塌不塌，就能分开这两种情况——邻域里都还在是台地，只有这一个数好用是悬崖，
而悬崖是过拟合最典型的形状。

三个刻意的选择，详细理由见 ``docs/archive/sensitivity-plan.md``：

1. **在分位空间扰动，不在阈值的算术空间。** 阈值 ±10% 对不同量纲的指标含义完全不同
   （一个指标阈值 10.2，另一个 0.03），而且「A > B」这类两个指标相比的条件根本没有
   常数可挪。任何条件都先归约成「差值序列 + 阈值」，再在序列的分位上挪，于是
   「松一点 / 紧一点」对所有条件都是同一个意思。
2. **度量用挖掘自己的口径。** 邻域曲线上的 lift 和候选表里的 lift 必须是同一个数，
   否则「峰值 1.42、邻居 1.31」没法读。所以命中样本的交易组去重、胜率、基线胜率
   全部照抄挖掘的实现，基点还直接用原始阈值算而不是按分位重建。
3. **不给稳健度评分。** 只给「几个偏移仍有提升 / 中位保持率 / 最差保持率」这些事实，
   多少算稳健取决于拿它干什么——和衰减比、滚动一致性是同一个立场。

另有一条必须说清的边界：**这是样本内**（挖掘样本＝寻优期）的邻域形状，回答的是
「这个切点是不是噪声尖峰」，不是「样本外还能不能行」。后者由验证期、留出期和
滚动复验负责。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

import numpy as np
import pandas as pd

from selector_app.screening.models import ScreenCondition

# 私有但是故意复用：邻域曲线上的 lift 必须和候选表里的 lift 用同一个口径算，
# 复制一份实现就等于埋一个"两处算法悄悄分叉"的坑。
from .mining import _grouped_outcome_rate
from .samples import MiningSamples

# 分位偏移：负=更宽松（阈值降、命中变多），正=更严格。0 是基点，用原始阈值直接算。
DEFAULT_SENSITIVITY_OFFSETS: tuple[float, ...] = (-0.10, -0.05, 0.0, 0.05, 0.10)
# 分位推到两端会让命中集合退化成「几乎没有」或「几乎所有」，两边都没有判读价值。
_MIN_QUANTILE = 0.01
_MAX_QUANTILE = 0.99
# 基点优势低于这个值就不算有优势，保持率没有分母（与衰减比同一个门槛：
# 分母没有意义时给 None，不硬算）。
_EDGE_EPSILON = 1e-12


@dataclass(frozen=True)
class SensitivityPoint:
    """One neighbouring cut point, scored the way mining scored the original."""

    offset: float
    # 分位与阈值都留着：分位说清"挪了多少"，阈值说清"挪到哪儿"。
    quantile: float
    threshold: float
    support_count: int
    outcome_rate: float | None
    baseline_rate: float
    lift: float | None
    # (lift - 1) / (基点 lift - 1)。基点没有优势时为 None。
    retention: float | None
    # 命中样本数没到 min_support：这一格没有判读价值，不计入统计。
    insufficient: bool
    # 阈值退化到条件恒真（不再筛掉任何样本）：同样不构成一个「邻域条件」。
    degenerate: bool


@dataclass(frozen=True)
class SensitivityProfile:
    """The whole neighbourhood curve for one mined condition."""

    points: tuple[SensitivityPoint, ...]
    # 仍然有提升（lift > 1）的偏移数 / 计入统计的偏移数。
    stable_offsets: int
    evaluated_offsets: int
    # 被剔除的偏移数（样本不足或恒真）。
    skipped: int
    median_retention: float | None
    worst_retention: float | None
    note: str | None


def build_sensitivity_for_samples(
    condition: ScreenCondition,
    samples: Sequence[MiningSamples],
    *,
    min_support: int,
    offsets: Sequence[float] = DEFAULT_SENSITIVITY_OFFSETS,
) -> SensitivityProfile | None:
    """Score one condition's neighbourhood over a study's mining samples."""

    usable = [sample for sample in samples if len(sample)]
    if not usable:
        return None
    return build_sensitivity(
        condition,
        pd.concat([sample.features for sample in usable], ignore_index=True),
        pd.concat([sample.outcomes for sample in usable], ignore_index=True),
        pd.concat([sample.instruments for sample in usable], ignore_index=True),
        pd.concat([sample.groups for sample in usable], ignore_index=True),
        min_support=min_support,
        offsets=offsets,
    )


def build_sensitivity(
    condition: ScreenCondition,
    features: pd.DataFrame,
    outcomes: pd.Series,
    instruments: pd.Series,
    groups: pd.Series,
    *,
    min_support: int,
    offsets: Sequence[float] = DEFAULT_SENSITIVITY_OFFSETS,
) -> SensitivityProfile | None:
    """Score one condition at neighbouring cut points.

    Returns None when the condition has no scalar parameter to move - a
    cross-over rule, say, or a feature the samples do not carry. Mining only
    emits threshold and pairwise comparisons, so this is a guard rather than a
    routine case.
    """

    resolved = _margin_series(condition, features)
    if resolved is None:
        return None
    values, base_threshold, strict, valid = resolved
    target = np.asarray(outcomes, dtype=bool).astype(np.float64)
    symbols = np.asarray(instruments, dtype=object)
    group_values = np.asarray(groups, dtype=object)
    if target.size != values.size or symbols.size != values.size:
        return None
    if int(valid.sum()) == 0:
        return None
    usable_rows = valid & pd.notna(symbols) & pd.notna(group_values)
    if int(usable_rows.sum()) == 0:
        return None

    series = values[usable_rows]
    sample_target = target[usable_rows]
    # 分组去重只需要"哪些样本同组"，不需要和挖掘拿到同一套编码，所以在压缩后的
    # 样本上重新编码即可——分区相同，结果就相同。
    group_codes = pd.factorize(group_values[usable_rows])[0]
    baseline_rate = _grouped_outcome_rate(sample_target, group_codes)
    if not baseline_rate > 0.0:
        return None
    base_quantile = float(np.mean(series <= base_threshold if strict else series < base_threshold))
    ordered = np.sort(series)

    points: list[SensitivityPoint] = []
    for offset in offsets:
        if offset == 0.0:
            threshold = base_threshold
            quantile = base_quantile
        else:
            quantile = min(_MAX_QUANTILE, max(_MIN_QUANTILE, base_quantile + offset))
            threshold = float(np.quantile(ordered, quantile))
        matched = series > threshold if strict else series >= threshold
        points.append(
            _score_point(
                offset=float(offset),
                quantile=quantile,
                threshold=threshold,
                matched=matched,
                target=sample_target,
                group_codes=group_codes,
                baseline_rate=baseline_rate,
                min_support=min_support,
                degenerate=bool(np.all(matched)),
            )
        )
    # 保持率以基点优势为分母，而基点在 offsets 里的位置不一定在前面
    # （默认在最中间），所以每个点先只算 lift，拿到基点之后再统一换算。
    base_lift = next((point.lift for point in points if point.offset == 0.0), None)
    if base_lift is not None and base_lift - 1.0 > _EDGE_EPSILON:
        resolved_points: list[SensitivityPoint] = []
        for point in points:
            value = 1.0 if point.offset == 0.0 else _retention(point, base_lift)
            resolved_points.append(point if value is None else _with_retention(point, value))
        points = resolved_points
    return _profile(tuple(points), min_support=min_support)


def _retention(point: SensitivityPoint, base_lift: float) -> float | None:
    if point.lift is None:
        return None
    return (point.lift - 1.0) / (base_lift - 1.0)


def sensitivity_dict(profile: SensitivityProfile | None) -> dict[str, Any] | None:
    """Report payload for one profile; None in, None out."""

    if profile is None:
        return None
    return {
        "points": [
            {
                "offset": point.offset,
                "quantile": point.quantile,
                "threshold": point.threshold,
                "support_count": point.support_count,
                "outcome_rate": point.outcome_rate,
                "baseline_rate": point.baseline_rate,
                "lift": point.lift,
                "retention": point.retention,
                "insufficient": point.insufficient,
                "degenerate": point.degenerate,
            }
            for point in profile.points
        ],
        "stable_offsets": profile.stable_offsets,
        "evaluated_offsets": profile.evaluated_offsets,
        "skipped": profile.skipped,
        "median_retention": profile.median_retention,
        "worst_retention": profile.worst_retention,
        "note": profile.note,
    }


def _margin_series(
    condition: ScreenCondition,
    features: pd.DataFrame,
) -> tuple[np.ndarray, float, bool, np.ndarray] | None:
    """Reduce one condition to ``(差值序列, 阈值, 是否严格不等, 可评估掩码)``.

    ``命中 ⇔ 序列 ≥ 阈值``（严格时为 ``>``）对阈值条件和两两比较都成立，后面的
    偏移和计分就只需要处理一种形式。
    """

    left = _numeric(features, condition.left_value)
    if left is None:
        return None
    operator = str(condition.operator)
    right_id = condition.right_value
    if right_id is not None:
        right = _numeric(features, right_id)
        if right is None:
            return None
        valid = np.isfinite(left) & np.isfinite(right)
        if operator == "gt":
            return left - right, 0.0, True, valid
        if operator == "lt":
            return right - left, 0.0, True, valid
        return None
    constant = condition.right_constant
    if constant is None:
        return None
    valid = np.isfinite(left)
    if operator == "gte":
        return left, float(constant), False, valid
    if operator == "lte":
        # 取负后统一成 ≥：``x ≤ c`` 等价于 ``−x ≥ −c``。
        return -left, -float(constant), False, valid
    return None


def _numeric(features: pd.DataFrame, column: str) -> np.ndarray | None:
    if column not in features.columns:
        return None
    return np.asarray(
        pd.to_numeric(features[column], errors="coerce").to_numpy(dtype=float), dtype=float
    )


def _score_point(
    *,
    offset: float,
    quantile: float,
    threshold: float,
    matched: np.ndarray,
    target: np.ndarray,
    group_codes: np.ndarray,
    baseline_rate: float,
    min_support: int,
    degenerate: bool,
) -> SensitivityPoint:
    order = np.flatnonzero(matched)
    if order.size == 0 or degenerate:
        return SensitivityPoint(
            offset=offset,
            quantile=quantile,
            threshold=threshold,
            support_count=int(order.size),
            outcome_rate=None,
            baseline_rate=baseline_rate,
            lift=None,
            retention=None,
            insufficient=True,
            degenerate=degenerate,
        )
    # 一组最多贡献一个样本，取首次命中——与挖掘的 support_count 同一口径。
    _, first_positions = np.unique(group_codes[order], return_index=True)
    indices = np.sort(order[first_positions])
    support_count = int(indices.size)
    if support_count < min_support:
        return SensitivityPoint(
            offset=offset,
            quantile=quantile,
            threshold=threshold,
            support_count=support_count,
            outcome_rate=None,
            baseline_rate=baseline_rate,
            lift=None,
            retention=None,
            insufficient=True,
            degenerate=False,
        )
    outcome_rate = float(np.mean(target[indices]))
    return SensitivityPoint(
        offset=offset,
        quantile=quantile,
        threshold=threshold,
        support_count=support_count,
        outcome_rate=outcome_rate,
        baseline_rate=baseline_rate,
        lift=float(outcome_rate / baseline_rate),
        retention=None,
        insufficient=False,
        degenerate=False,
    )


def _with_retention(point: SensitivityPoint, retention: float) -> SensitivityPoint:
    return replace(point, retention=retention)


def _profile(points: tuple[SensitivityPoint, ...], *, min_support: int) -> SensitivityProfile:
    neighbours = [
        point
        for point in points
        if point.offset != 0.0 and not point.insufficient and not point.degenerate
    ]
    retentions: list[float] = []
    for point in neighbours:
        if point.retention is not None:
            retentions.append(point.retention)
    stable = sum(1 for point in neighbours if point.lift is not None and point.lift > 1.0)
    skipped = len([point for point in points if point.insufficient or point.degenerate])
    notes: list[str] = []
    if skipped:
        notes.append(
            f"{skipped} 个偏移没有形成可判定的邻域条件"
            f"（命中样本不足 {min_support} 个或阈值退化到恒真），未计入统计"
        )
    return SensitivityProfile(
        points=points,
        stable_offsets=stable,
        evaluated_offsets=len(neighbours),
        skipped=skipped,
        median_retention=_median(retentions),
        worst_retention=min(retentions) if retentions else None,
        note="；".join(notes) or None,
    )


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2
