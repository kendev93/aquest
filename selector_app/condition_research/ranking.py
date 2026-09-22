"""Cross-sectional ranking evidence for mined entry signals.

Condition research studies one stock at a time, so its findings say nothing
about which stock to buy when several qualify on the same day. A portfolio
backtest, however, is a slot machine: the entry rule decides who is *eligible*
and the ranking decides who actually gets one of the few slots. This module
bridges the two by scoring every candidate ranking value on the signal days the
research already produced - no extra indicator passes, no re-simulation.

Three independent pieces of evidence are collected, because they fail in
different ways:

* **Information coefficient** (rank correlation between the value and the
  realised return on each signal day) - does the value predict anything?
* **IC information ratio** plus **segment sign agreement** - is the edge stable,
  or was one lucky month carrying it?
* **Quantile monotonicity** - is the edge spread across the cross-section, or
  does it live only in the extreme tail that rarely survives live trading?

The recommendation is ordered by stability, never by the single largest lift:
31 values times 2 directions is 62 trials, and picking the biggest lift out of
that reliably selects noise.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

from .samples import MiningSamples

# A day with only a couple of qualifying stocks carries no cross-sectional
# information - a rank correlation over three points is meaningless.
MIN_CROSS_SECTION = 5
# Below this many signal days the standard error of the mean IC is of the same
# order as the IC itself, so nothing should be recommended at all.
MIN_IC_DAYS = 20
WEAK_IC = 0.02
STRONG_IC = 0.05
STABLE_ICIR = 0.5
QUANTILE_COUNT = 5
SEGMENT_COUNT = 6

Confidence = Literal["high", "medium", "low", "none"]


@dataclass(frozen=True)
class RankingEvidence:
    """One candidate ranking value, scored on the mined signal days."""

    value_id: str
    display_name: str
    order: Literal["asc", "desc"]
    ic_mean: float
    icir: float | None
    segments: int
    same_sign_segments: int
    quantile_returns: tuple[float | None, ...]
    top_n_lift: float | None
    sample_days: int
    confidence: Confidence
    note: str

    def to_dict(self) -> dict[str, object]:
        return {
            "value_id": self.value_id,
            "display_name": self.display_name,
            "order": self.order,
            "ic_mean": self.ic_mean,
            "icir": self.icir,
            "segments": self.segments,
            "same_sign_segments": self.same_sign_segments,
            "quantile_returns": list(self.quantile_returns),
            "top_n_lift": self.top_n_lift,
            "sample_days": self.sample_days,
            "confidence": self.confidence,
            "note": self.note,
        }


@dataclass(frozen=True)
class RankingAssessment:
    """Everything the UI needs to show a ranking recommendation."""

    top_n: int
    min_cross_section: int
    sample_days: int
    evaluated: tuple[RankingEvidence, ...]
    recommended: RankingEvidence | None
    reason: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "top_n": self.top_n,
            "min_cross_section": self.min_cross_section,
            "sample_days": self.sample_days,
            "evaluated": [item.to_dict() for item in self.evaluated],
            "recommended": self.recommended.to_dict() if self.recommended else None,
            "reason": self.reason,
            "available": self.recommended is not None,
        }


def _panel(
    samples: Sequence[MiningSamples],
    value_ids: Sequence[str],
) -> tuple[pd.DataFrame, tuple[str, ...]]:
    """Stack every stock's signal-day rows into one cross-sectional frame.

    Returns the frame plus the value ids actually present. A research feature
    list may name columns the feature frame never materialised - a custom
    formula's signal statements are one example - so callers must work from
    what came back instead of what they asked for.
    """

    if not samples:
        return pd.DataFrame(), ()
    available = tuple(value_id for value_id in value_ids if value_id in samples[0].features.columns)
    columns: dict[str, np.ndarray] = {
        "date": np.concatenate([item.dates.to_numpy(dtype=object) for item in samples]),
        "return": np.concatenate([item.returns.to_numpy(dtype=float) for item in samples]),
    }
    for value_id in available:
        columns[value_id] = np.concatenate(
            [item.features[value_id].to_numpy(dtype=float) for item in samples]
        )
    return pd.DataFrame(columns), available


def _spearman(values: np.ndarray, outcomes: np.ndarray) -> float:
    """Rank correlation, computed as Pearson on ranks (NaN when degenerate)."""

    left = pd.Series(values).rank().to_numpy(dtype=float)
    right = pd.Series(outcomes).rank().to_numpy(dtype=float)
    if np.nanstd(left) == 0 or np.nanstd(right) == 0:
        return float("nan")
    correlation = float(np.corrcoef(left, right)[0, 1])
    return correlation if np.isfinite(correlation) else float("nan")


def _quantile_returns(sorted_returns: np.ndarray) -> list[float | None]:
    means: list[float | None] = []
    for chunk in np.array_split(sorted_returns, QUANTILE_COUNT):
        means.append(float(np.mean(chunk)) if len(chunk) else None)
    return means


def _inversions(quantiles: Sequence[float | None]) -> int:
    ordered = [item for item in quantiles if item is not None]
    return sum(1 for first, second in zip(ordered, ordered[1:]) if first + 1e-12 < second)


def _confidence(
    *,
    sample_days: int,
    ic_mean: float,
    icir: float | None,
    same_sign: int,
    segments: int,
) -> tuple[Confidence, str]:
    if sample_days < MIN_IC_DAYS:
        return (
            "none",
            f"有效信号日只有 {sample_days} 天（至少需要 {MIN_IC_DAYS} 天），样本不足以判断排序效果",
        )
    if ic_mean < WEAK_IC:
        return "low", f"平均信息系数仅 {ic_mean:.3f}，低于 {WEAK_IC:.2f}，几乎看不出预测力"
    if same_sign * 2 < segments:
        return (
            "low",
            f"{segments} 个时间段里只有 {same_sign} 段同向，说明时灵时不灵，不适合直接采用",
        )
    if icir is not None and icir < STABLE_ICIR * 0.6:
        return "low", f"信息系数波动过大（信息比率 {icir:.2f}），稳定性不足"
    ratio_text = f"信息比率 {icir:.2f}" if icir is not None else "信息系数几乎无波动"
    if ic_mean >= STRONG_IC and same_sign == segments and (icir is None or icir >= STABLE_ICIR):
        return (
            "high",
            f"三条证据同向：平均信息系数 {ic_mean:.3f}、{ratio_text}、{segments}/{segments} 段同号",
        )
    return (
        "medium",
        f"平均信息系数 {ic_mean:.3f}、{ratio_text}、{same_sign}/{segments} 段同号，"
        "可用但建议先在组合回测里复验",
    )


def _score(evidence: RankingEvidence) -> tuple[int, float, float]:
    """Order by stability first - never by the single largest lift."""

    rank = {"high": 0, "medium": 1, "low": 2, "none": 3}
    # A zero IC standard deviation is perfect stability, not a missing value.
    ratio = evidence.icir if evidence.icir is not None else STABLE_ICIR * 2
    stability = (evidence.same_sign_segments / max(1, evidence.segments)) * ratio
    # Descending on quality: confidence rank ascending, then stability and edge
    # size descending (negated so the ascending tuple sort puts them first).
    return (rank[evidence.confidence], -stability, -evidence.ic_mean)


def assess_ranking_values(
    samples: Sequence[MiningSamples],
    value_ids: Sequence[str],
    *,
    top_n: int = 5,
    display_names: Mapping[str, str] | None = None,
    min_cross_section: int = MIN_CROSS_SECTION,
    segments: int = SEGMENT_COUNT,
) -> RankingAssessment:
    """Score every ranking value on the signal days mined from real trades."""

    if not samples or not value_ids:
        return RankingAssessment(
            top_n=top_n,
            min_cross_section=min_cross_section,
            sample_days=0,
            evaluated=(),
            recommended=None,
            reason="没有可用于评估的买入信号样本",
        )
    frame, value_ids = _panel(samples, value_ids)
    if frame.empty or not value_ids:
        return RankingAssessment(
            top_n=top_n,
            min_cross_section=min_cross_section,
            sample_days=0,
            evaluated=(),
            recommended=None,
            reason="没有可用于评估的买入信号样本",
        )

    daily_ic: dict[str, list[float]] = {value_id: [] for value_id in value_ids}
    daily_lift_desc: dict[str, list[float]] = {value_id: [] for value_id in value_ids}
    daily_lift_asc: dict[str, list[float]] = {value_id: [] for value_id in value_ids}
    quantile_accumulator: dict[str, list[list[float]]] = {
        value_id: [[] for _ in range(QUANTILE_COUNT)] for value_id in value_ids
    }
    day_count = 0
    for _, group in frame.groupby("date", sort=True):
        if len(group) < min_cross_section:
            continue
        day_count += 1
        for value_id in value_ids:
            pair = group[[value_id, "return"]].dropna()
            if len(pair) < min_cross_section:
                continue
            values = pair[value_id].to_numpy(dtype=float)
            returns = pair["return"].to_numpy(dtype=float)
            ic = _spearman(values, returns)
            if np.isfinite(ic):
                daily_ic[value_id].append(ic)
            ordered = pair.sort_values(value_id, ascending=False)
            ordered_returns = ordered["return"].to_numpy(dtype=float)
            daily_lift_desc[value_id].append(
                float(np.mean(ordered_returns[:top_n])) - float(np.mean(returns))
            )
            daily_lift_asc[value_id].append(
                float(np.mean(ordered_returns[-top_n:])) - float(np.mean(returns))
            )
            for index, mean in enumerate(_quantile_returns(ordered_returns)):
                if mean is not None:
                    quantile_accumulator[value_id][index].append(mean)

    evaluated: list[RankingEvidence] = []
    for value_id in value_ids:
        ic_series = np.asarray(daily_ic[value_id], dtype=float)
        if ic_series.size == 0:
            continue
        ic_mean = float(np.mean(ic_series))
        ic_std = float(np.std(ic_series, ddof=1)) if ic_series.size > 1 else 0.0
        icir = float(ic_mean / ic_std) if ic_std > 1e-12 else None
        split = np.array_split(ic_series, segments) if ic_series.size >= segments else []
        segment_means = [float(np.mean(chunk)) for chunk in split if len(chunk)]
        same_sign = (
            sum(1 for value in segment_means if (value > 0) == (ic_mean > 0))
            if segment_means
            else 0
        )
        # Negative IC simply means the value works upside down: report it in the
        # direction a user would actually configure instead of dropping it.
        order: Literal["asc", "desc"] = "desc" if ic_mean >= 0 else "asc"
        strength = abs(ic_mean)
        quantiles = [
            float(np.mean(bucket)) if bucket else None for bucket in quantile_accumulator[value_id]
        ]
        if order == "asc":
            quantiles = quantiles[::-1]
        lift_source = daily_lift_desc[value_id] if order == "desc" else daily_lift_asc[value_id]
        top_n_excess = float(np.mean(lift_source)) if lift_source else None
        confidence, note = _confidence(
            sample_days=len(ic_series),
            ic_mean=strength,
            icir=abs(icir) if icir is not None else None,
            same_sign=same_sign,
            segments=len(segment_means),
        )
        inversions = _inversions(quantiles)
        if inversions and confidence in {"high", "medium"}:
            note = f"{note}；分层收益有 {inversions} 处倒挂，收益可能集中在极端样本上"
        evaluated.append(
            RankingEvidence(
                value_id=value_id,
                display_name=(display_names or {}).get(value_id, value_id),
                order=order,
                ic_mean=strength,
                icir=abs(icir) if icir is not None else None,
                segments=len(segment_means),
                same_sign_segments=same_sign,
                quantile_returns=tuple(quantiles),
                top_n_lift=top_n_excess,
                sample_days=len(ic_series),
                confidence=confidence,
                note=note,
            )
        )

    evaluated.sort(key=_score)
    recommended = next((item for item in evaluated if item.confidence in {"high", "medium"}), None)
    reason: str | None = None
    if recommended is None:
        if not evaluated:
            reason = "没有任何指标能形成有效的横截面样本"
        else:
            reason = evaluated[0].note
    return RankingAssessment(
        top_n=top_n,
        min_cross_section=min_cross_section,
        sample_days=day_count,
        evaluated=tuple(evaluated),
        recommended=recommended,
        reason=reason,
    )
