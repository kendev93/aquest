"""Validation labels shared by individual-security research reports."""

from __future__ import annotations

from collections.abc import Mapping

#: 单只股票、单个槽位在一个验证段里能完成的交易本来就少，所以这里的门槛刻意
#: 低于条件研究 / 策略发现的 ``min_trades``（那两者是全市场或多槽位口径，一个
#: 组合在验证段能做出几十笔）。别把它们统一成同一个数：照搬 20 会让几乎每个
#: 候选都挂上「样本不足」，标记反而失去区分度。
MIN_VALIDATION_TRADES = 5
MIN_HOLDOUT_TRADES = 3

#: 超过这个回撤就额外标「风险高」。
MAX_DRAWDOWN_FOR_BADGE = 0.30


def candidate_quality(
    validation_trades: int,
    holdout_trades: int,
    max_drawdown: float | None,
) -> Mapping[str, object]:
    """Annotate sample size and drawdown without removing a candidate from a board."""

    badges: list[str] = []
    if validation_trades >= MIN_VALIDATION_TRADES and holdout_trades >= MIN_HOLDOUT_TRADES:
        badges.append("样本较充分")
    else:
        badges.append("样本不足")
    if max_drawdown is not None and abs(max_drawdown) > MAX_DRAWDOWN_FOR_BADGE:
        badges.append("风险高")
    return {
        "badges": badges,
        "sample_adequate": "样本较充分" in badges,
        "high_drawdown_risk": "风险高" in badges,
        "eligible_for_display": True,
    }


__all__ = ["candidate_quality"]
