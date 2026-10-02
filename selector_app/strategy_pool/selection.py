"""Shared date splitting and per-objective candidate selection for the market pool."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date

import numpy as np

from selector_app.individual_research.metrics import candidate_quality

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
) -> dict[str, ObjectiveCandidate | None]:
    """Select each objective from validation metrics only and retain compact replay data."""

    boards = {
        "annual_return": result.get("results_by_return"),
        "sharpe": result.get("results_by_sharpe"),
    }
    winners: dict[str, ObjectiveCandidate | None] = {}
    for objective, raw_candidates in boards.items():
        candidates = _mapping_sequence(raw_candidates)
        winner: Mapping[str, object] | None = None
        winning_score: float | None = None
        for candidate in candidates:
            metrics = _mapping(candidate.get("metrics"))
            validation = _mapping(metrics.get("validation")) or _mapping(
                candidate.get("validation")
            )
            score = _finite_number(validation.get(objective))
            if score is None:
                continue
            if winning_score is None or score > winning_score:
                winner = candidate
                winning_score = score
        winners[objective] = (
            _compact_candidate(winner, objective, result.get("data_fingerprint"))
            if winner is not None
            else None
        )
    return winners


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
