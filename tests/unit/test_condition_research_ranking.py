"""Ranking-value evidence: does a value predict returns across the signal day?"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from selector_app.condition_research.ranking import (
    MIN_IC_DAYS,
    assess_ranking_values,
)
from selector_app.condition_research.samples import MiningSamples


def _samples(
    frame: pd.DataFrame,
    *values: str,
) -> MiningSamples:
    return MiningSamples(
        features=frame[list(values)],
        outcomes=pd.Series(frame["return"].to_numpy() > 0),
        instruments=pd.Series(["SH600000"] * len(frame)),
        groups=pd.Series([f"g{index}" for index in range(len(frame))]),
        dates=frame["date"],
        returns=frame["return"],
    )


def _monotone_frame(days: int, codes: int, *, reverse: bool = False) -> pd.DataFrame:
    rng = np.random.default_rng(20240917)
    rows: list[dict[str, float | str]] = []
    for day in range(days):
        for _ in range(codes):
            score = float(rng.uniform(0.0, 100.0))
            outcome = score / 100.0
            rows.append(
                {
                    "date": f"2024-01-{day + 1:02d}",
                    "return": -outcome if reverse else outcome,
                    "technical.score": score,
                }
            )
    return pd.DataFrame(rows)


def test_perfect_positive_value_is_recommended_descending() -> None:
    frame = _monotone_frame(days=40, codes=8)

    assessment = assess_ranking_values(
        [_samples(frame, "technical.score")],
        ("technical.score",),
        top_n=3,
        display_names={"technical.score": "综合评分"},
    )

    assert assessment.recommended is not None
    assert assessment.recommended.order == "desc"
    assert assessment.recommended.ic_mean == pytest.approx(1.0, abs=1e-9)
    assert assessment.recommended.confidence == "high"
    assert assessment.recommended.display_name == "综合评分"
    assert assessment.sample_days == 40
    quantiles = [item for item in assessment.recommended.quantile_returns if item is not None]
    assert quantiles == sorted(quantiles, reverse=True)


def test_negative_value_is_flipped_to_ascending() -> None:
    frame = _monotone_frame(days=40, codes=8, reverse=True)

    assessment = assess_ranking_values(
        [_samples(frame, "technical.score")], ("technical.score",), top_n=3
    )

    assert assessment.recommended is not None
    assert assessment.recommended.order == "asc"
    assert assessment.recommended.ic_mean == pytest.approx(1.0, abs=1e-9)


def test_too_few_signal_days_produces_no_recommendation() -> None:
    frame = _monotone_frame(days=MIN_IC_DAYS - 1, codes=8)

    assessment = assess_ranking_values([_samples(frame, "technical.score")], ("technical.score",))

    assert assessment.recommended is None
    assert assessment.reason is not None
    assert "至少需要" in assessment.reason
    assert assessment.to_dict()["available"] is False


def test_stable_moderate_value_beats_unstable_high_value() -> None:
    """62 trials means the biggest lift is usually noise; stability must win."""

    rng = np.random.default_rng(11)
    days = 48
    codes = 8
    rows: list[dict[str, float | str]] = []
    for day in range(days):
        outcomes = rng.normal(size=codes)
        stable_noise = rng.normal(scale=3.0, size=codes)
        unstable_noise = rng.normal(scale=0.2, size=codes)
        # The unstable value is strongly right for the first third of the period
        # and strongly wrong afterwards.
        sign = 1.0 if day < days // 3 else -1.0
        for index in range(codes):
            rows.append(
                {
                    "date": f"2024-01-{day + 1:02d}",
                    "return": float(outcomes[index]),
                    "technical.stable": float(outcomes[index] + stable_noise[index]),
                    "technical.unstable": float(sign * outcomes[index] + unstable_noise[index]),
                }
            )
    frame = pd.DataFrame(rows)

    assessment = assess_ranking_values(
        [_samples(frame, "technical.stable", "technical.unstable")],
        ("technical.unstable", "technical.stable"),
        top_n=3,
    )

    assert assessment.recommended is not None
    assert assessment.recommended.value_id == "technical.stable"
    unstable = next(item for item in assessment.evaluated if item.value_id == "technical.unstable")
    assert unstable.same_sign_segments < unstable.segments
    assert assessment.recommended.confidence != "low"


def test_missing_feature_columns_are_skipped_instead_of_raising() -> None:
    frame = _monotone_frame(days=30, codes=8)

    assessment = assess_ranking_values(
        [_samples(frame, "technical.score")],
        ("technical.score", "custom.buy"),
        top_n=3,
    )

    assert assessment.recommended is not None
    assert [item.value_id for item in assessment.evaluated] == ["technical.score"]


def test_empty_samples_report_a_reason() -> None:
    assessment = assess_ranking_values((), ("technical.score",))

    assert assessment.recommended is None
    assert assessment.reason == "没有可用于评估的买入信号样本"
