"""Validation labels shared by individual-security research reports."""

from __future__ import annotations

from collections.abc import Mapping


def candidate_quality(
    validation_trades: int,
    holdout_trades: int,
    max_drawdown: float | None,
) -> Mapping[str, object]:
    """Annotate sample size and drawdown without removing a candidate from a board."""

    badges: list[str] = []
    if validation_trades >= 5 and holdout_trades >= 3:
        badges.append("样本较充分")
    else:
        badges.append("样本不足")
    if max_drawdown is not None and abs(max_drawdown) > 0.30:
        badges.append("风险高")
    return {
        "badges": badges,
        "sample_adequate": "样本较充分" in badges,
        "high_drawdown_risk": "风险高" in badges,
        "eligible_for_display": True,
    }


__all__ = ["candidate_quality"]
