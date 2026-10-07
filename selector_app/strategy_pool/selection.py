"""Shared date splitting and per-objective candidate selection for the market pool."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date

import numpy as np

from selector_app.individual_research.metrics import MIN_VALIDATION_TRADES, candidate_quality
from selector_app.strategy_discovery.candidates import DEFAULT_MAX_HOLD_DAYS

ObjectiveCandidate = dict[str, object]


def common_calendar_windows(
    dates: Sequence[date], *, purge_days: int
) -> dict[str, tuple[date, date]]:
    """Create common 60/20/20 windows and purge forward-label overlap at each edge."""

    if purge_days < 0:
        raise ValueError("前视清除交易日不能小于 0")
    ordered = sorted(set(dates))
    if len(ordered) < max(3, purge_days * 3 + 3):
        raise ValueError("共同市场日历不足以进行训练、验证、留出三段切分")

    day_count = len(ordered)
    train_cut = max(1, min(day_count - 2, int(day_count * 0.6)))
    validation_cut = max(train_cut + 1, min(day_count - 1, int(day_count * 0.8)))
    train_stop = max(1, train_cut - purge_days)
    validation_stop = max(train_cut + 1, validation_cut - purge_days)
    holdout_stop = max(validation_cut + 1, day_count - purge_days)
    if train_stop <= 0 or validation_stop <= train_cut or holdout_stop <= validation_cut:
        raise ValueError("共同市场日历在清除前视窗口后没有足够样本")

    return {
        "train": (ordered[0], ordered[train_stop - 1]),
        "validation": (ordered[train_cut], ordered[validation_stop - 1]),
        "holdout": (ordered[validation_cut], ordered[holdout_stop - 1]),
    }


def slices_for_common_windows(
    dates: Sequence[date] | np.ndarray,
    windows: Mapping[str, tuple[date, date]],
) -> dict[str, slice]:
    """Project shared calendar windows onto a symbol's own observed trading dates."""

    observed = [
        value if isinstance(value, date) else date.fromisoformat(str(value)[:10]) for value in dates
    ]
    result: dict[str, slice] = {}
    for phase in ("train", "validation", "holdout"):
        bounds = windows.get(phase)
        if bounds is None:
            result[phase] = slice(0, 0)
            continue
        start_date, end_date = bounds
        start = next(
            (index for index, value in enumerate(observed) if value >= start_date), len(observed)
        )
        stop = next(
            (index for index, value in enumerate(observed) if value > end_date), len(observed)
        )
        result[phase] = slice(start, stop)
    return result


def summarize_objective_winners(
    result: Mapping[str, object],
    *,
    max_hold_days: int | None = DEFAULT_MAX_HOLD_DAYS,
) -> dict[str, ObjectiveCandidate | None]:
    """Select each objective from validation metrics only and retain compact replay data.

    样本不够的候选很容易靠一两笔交易把年化 / 夏普顶到榜首：持有两年不动的一笔
    交易就足以让「平均持仓 700 天」的策略看起来比交易了几十笔的还好。所以只要
    榜上存在样本足够的候选，胜者就只在它们之间产生；全部候选都样本不足时才退回
    按分数选，免得样本稀薄的市场直接得到一个空池子。

    ``max_hold_days``（交易日）是**硬门槛**，和样本门槛不同：它没有"全都不合格
    就退回"的豁免——持仓过长正是这次要治的病，退回等于白设。候选验证段的
    ``avg_holding_days`` 是自然日口径（回测绩效报告如此），要按年化常数折算成
    交易日再比；个别候选缺这个指标时不误杀，只跳过这项检查。
    """

    boards = {
        "annual_return": result.get("results_by_return"),
        "sharpe": result.get("results_by_sharpe"),
    }
    winners: dict[str, ObjectiveCandidate | None] = {}
    for objective, raw_candidates in boards.items():
        candidates = _mapping_sequence(raw_candidates)
        winner = _best_candidate(
            candidates, objective, require_adequate_sample=True, max_hold_days=max_hold_days
        )
        if winner is None:
            winner = _best_candidate(
                candidates, objective, require_adequate_sample=False, max_hold_days=max_hold_days
            )
        winners[objective] = (
            _compact_candidate(winner, objective, result.get("data_fingerprint"))
            if winner is not None
            else None
        )
    return winners


def _best_candidate(
    candidates: Sequence[Mapping[str, object]],
    objective: str,
    *,
    require_adequate_sample: bool,
    max_hold_days: int | None,
) -> Mapping[str, object] | None:
    """Pick the highest validation score, optionally skipping thin samples."""

    winner: Mapping[str, object] | None = None
    winning_score: float | None = None
    for candidate in candidates:
        metrics = _mapping(candidate.get("metrics"))
        validation = _mapping(metrics.get("validation")) or _mapping(candidate.get("validation"))
        score = _finite_number(validation.get(objective))
        if score is None:
            continue
        if _exceeds_hold_cap(validation, max_hold_days):
            continue
        if require_adequate_sample and not _sample_adequate(validation):
            continue
        if winning_score is None or score > winning_score:
            winner = candidate
            winning_score = score
    return winner


def _sample_adequate(
    validation: Mapping[str, object],
) -> bool:
    """Use validation sample size for eligibility; holdout remains descriptive only."""

    return _trade_count(validation) >= MIN_VALIDATION_TRADES


#: 与 ``backtest.performance.PerformanceAnalyzer.ANNUAL_DAYS`` 同源：年化用 252 个
#: 交易日，"n 个交易日 ≈ n×365/252 个自然日"就由它决定。绩效报告的
#: ``avg_holding_days`` 是自然日，准入上限是交易日，两边必须换算到同一把尺才能比。
_NATURAL_TO_TRADING = 252 / 365


def _exceeds_hold_cap(validation: Mapping[str, object], max_hold_days: int | None) -> bool:
    """验证段平均持仓是否超出上限；``max_hold_days=None`` 表示不设限。"""

    if max_hold_days is None:
        return False
    natural_days = _finite_number(validation.get("avg_holding_days"))
    if natural_days is None:
        # 指标缺失时不误杀：门槛拦的是"明知持仓过长"，不是"缺数据"。
        return False
    return natural_days * _NATURAL_TO_TRADING > max_hold_days


def _compact_candidate(
    candidate: Mapping[str, object], objective: str, default_fingerprint: object
) -> ObjectiveCandidate:
    metrics = _mapping(candidate.get("metrics"))
    validation = _mapping(metrics.get("validation")) or _mapping(candidate.get("validation"))
    train = _mapping(metrics.get("train")) or _mapping(candidate.get("train"))
    holdout = _mapping(metrics.get("holdout"))
    conditions = _mapping(candidate.get("condition_rules"))
    buy_rule = candidate.get("buy_rule") or conditions.get("buy")
    sell_rule = candidate.get("sell_rule") or conditions.get("sell")
    raw_config = _mapping(candidate.get("config"))
    config: dict[str, object] = dict(raw_config)
    if buy_rule is not None:
        config["buy_rule"] = buy_rule
    if sell_rule is not None:
        config["sell_rule"] = sell_rule
    if not isinstance(buy_rule, Mapping) or not isinstance(sell_rule, Mapping):
        # A board entry without a complete entry and exit rule cannot be replayed.
        return {
            "candidate_id": str(
                candidate.get("candidate_id") or candidate.get("candidate_hash") or ""
            ),
            "objective": objective,
            "metrics": _metrics_summary(train, validation, holdout),
            "config": config,
            "quality": _quality(validation, holdout, candidate),
            "replayable": False,
        }
    return {
        "candidate_id": str(candidate.get("candidate_id") or candidate.get("candidate_hash") or ""),
        "candidate_hash": str(candidate.get("candidate_hash") or ""),
        "objective": objective,
        "buy_rule": dict(buy_rule),
        "sell_rule": dict(sell_rule),
        "config": config,
        "metrics": _metrics_summary(train, validation, holdout),
        "quality": _quality(validation, holdout, candidate),
        "data_fingerprint": candidate.get("data_fingerprint") or default_fingerprint,
        "replayable": True,
    }


def _metrics_summary(
    train: Mapping[str, object],
    validation: Mapping[str, object],
    holdout: Mapping[str, object],
) -> dict[str, object]:
    keys = (
        "total_return",
        "annual_return",
        "sharpe",
        "win_rate",
        "payoff_ratio",
        "max_drawdown",
        "closed_trades",
        "total_trades",
    )
    return {
        phase: {
            key: _finite_number(metrics.get(key)) for key in keys if metrics.get(key) is not None
        }
        for phase, metrics in (("train", train), ("validation", validation), ("holdout", holdout))
    }


def _quality(
    validation: Mapping[str, object],
    holdout: Mapping[str, object],
    candidate: Mapping[str, object],
) -> dict[str, object]:
    existing = _mapping(candidate.get("quality"))
    if existing:
        return dict(existing)
    validation_trades = _trade_count(validation)
    holdout_trades = _trade_count(holdout)
    drawdown = _finite_number(validation.get("max_drawdown"))
    return dict(candidate_quality(validation_trades, holdout_trades, drawdown))


def _trade_count(metrics: Mapping[str, object]) -> int:
    value = metrics.get("closed_trades", metrics.get("total_trades", 0))
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    if isinstance(value, str):
        try:
            return max(0, int(value))
        except ValueError:
            return 0
    return 0


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _mapping_sequence(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


__all__ = [
    "common_calendar_windows",
    "slices_for_common_windows",
    "summarize_objective_winners",
]
