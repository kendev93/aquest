"""Validation and final holdout pipeline for discovered candidates."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping, Sequence
from typing import cast

from .models import DiscoveryRequest, DiscoveryRun, StrategyCandidate


def _metric(metrics: Mapping[str, object], key: str) -> float:
    try:
        raw = metrics.get(key, float("-inf"))
        value = float(raw) if isinstance(raw, (int, float)) else float(str(raw))
    except (TypeError, ValueError):
        return float("-inf")
    return value if math.isfinite(value) else float("-inf")


def _passes(metrics: Mapping[str, object], request: DiscoveryRequest) -> bool:
    if metrics.get("error"):
        return False
    if not math.isfinite(_metric(metrics, "annual_return")):
        return False
    try:
        raw_trades = metrics.get("total_trades", 0)
        trades = (
            int(raw_trades) if isinstance(raw_trades, (int, float)) else int(str(raw_trades or 0))
        )
    except (TypeError, ValueError):
        trades = 0
    if trades < request.min_trades:
        return False
    if (
        request.max_drawdown is not None
        and abs(_metric(metrics, "max_drawdown")) > request.max_drawdown
    ):
        return False
    if request.max_turnover is not None and _metric(metrics, "turnover") > request.max_turnover:
        return False
    return True


def _unique(candidates: Sequence[StrategyCandidate]) -> list[StrategyCandidate]:
    result: list[StrategyCandidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.candidate_hash in seen:
            continue
        seen.add(candidate.candidate_hash)
        result.append(candidate)
    return result


def _behavior_fingerprint(candidate: StrategyCandidate, split: str) -> str | None:
    metrics = candidate.metrics.get(split)
    if not isinstance(metrics, Mapping):
        return None
    fingerprint = metrics.get("trade_fingerprint")
    if not isinstance(fingerprint, str):
        return None
    normalized = fingerprint.strip()
    return normalized or None


def _unique_behaviors(
    candidates: Sequence[StrategyCandidate], split: str
) -> list[StrategyCandidate]:
    """Keep one candidate for each observed trading behavior when available."""

    result: list[StrategyCandidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        fingerprint = _behavior_fingerprint(candidate, split)
        if fingerprint is not None:
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
        result.append(candidate)
    return result


def run_validation_pipeline(
    search_result: Mapping[str, object] | object,
    request: DiscoveryRequest,
    evaluator: Callable[[StrategyCandidate, str], Mapping[str, object]],
    *,
    progress_callback: Callable[[int, int, str], None] | None = None,
    abort_check: Callable[[], bool] | None = None,
    batch_evaluator: Callable[[Sequence[StrategyCandidate], str], Sequence[Mapping[str, object]]]
    | None = None,
    batch_size: int = 1,
) -> DiscoveryRun:
    """Evaluate search candidates on validation, then a small final holdout."""

    if hasattr(search_result, "results_by_sharpe"):
        sharpe = _candidate_list(
            getattr(search_result, "shortlist_by_sharpe", ())
            or getattr(search_result, "results_by_sharpe")
        )
        returns = _candidate_list(
            getattr(search_result, "shortlist_by_return", ())
            or getattr(search_result, "results_by_return")
        )
        search_metadata = dict(
            cast(Mapping[str, object], getattr(search_result, "search_metadata", {}))
        )
    else:
        raw = search_result if isinstance(search_result, Mapping) else {}
        sharpe = _candidate_list(raw.get("shortlist_by_sharpe") or raw.get("results_by_sharpe", []))
        returns = _candidate_list(
            raw.get("shortlist_by_return") or raw.get("results_by_return", [])
        )
        search_metadata = dict(cast(Mapping[str, object], raw.get("search_metadata", {})))
    # Each leaderboard owns its validation budget.  Do not truncate the union
    # after placing the Sharpe shortlist first, otherwise the return leaderboard
    # is evaluated from a biased subset and often becomes a reordered Sharpe list.
    sharpe_shortlist = sharpe[: request.validation_candidates]
    return_shortlist = returns[: request.validation_candidates]
    candidates = _unique([*sharpe_shortlist, *return_shortlist])
    if not candidates:
        raise ValueError("没有可供验证的策略候选")

    validated: list[StrategyCandidate] = []
    total = len(candidates)
    validation_started = time.perf_counter()
    for offset in range(0, total, max(1, batch_size)):
        chunk = candidates[offset : offset + max(1, batch_size)]
        if abort_check is not None and abort_check():
            raise RuntimeError("策略实验已取消")
        metrics_batch = (
            list(batch_evaluator(chunk, "validation"))
            if batch_evaluator is not None
            else [evaluator(candidate, "validation") for candidate in chunk]
        )
        if len(metrics_batch) != len(chunk):
            raise RuntimeError("批量候选评估返回数量不一致")
        for index, (candidate, metrics) in enumerate(
            zip(chunk, metrics_batch, strict=True), start=offset + 1
        ):
            enriched = candidate.with_metrics("validation", dict(metrics))
            if _passes(metrics, request):
                validated.append(enriched)
            if progress_callback is not None:
                progress_callback(index, total, "validation")
    validation_seconds = time.perf_counter() - validation_started
    if not validated:
        raise ValueError("验证区间没有满足约束的候选策略")

    ranked_by_sharpe = sorted(
        validated,
        key=lambda item: (
            _metric(item.metrics.get("validation", {}), "sharpe"),
            _metric(item.metrics.get("validation", {}), "annual_return"),
            -item.complexity,
        ),
        reverse=True,
    )
    ranked_by_return = sorted(
        validated,
        key=lambda item: (
            _metric(item.metrics.get("validation", {}), "annual_return"),
            _metric(item.metrics.get("validation", {}), "sharpe"),
            -item.complexity,
        ),
        reverse=True,
    )
    valid_by_sharpe = _unique_behaviors(ranked_by_sharpe, "validation")
    valid_by_return = _unique_behaviors(ranked_by_return, "validation")
    holdout_candidates = _unique(
        [
            *valid_by_sharpe[: max(1, (request.holdout_candidates + 1) // 2)],
            *valid_by_return[: max(1, request.holdout_candidates // 2)],
        ]
    )[: request.holdout_candidates]
    holdout_by_hash: dict[str, Mapping[str, object]] = {}
    holdout_started = time.perf_counter()
    for offset in range(0, len(holdout_candidates), max(1, batch_size)):
        chunk = holdout_candidates[offset : offset + max(1, batch_size)]
        if abort_check is not None and abort_check():
            raise RuntimeError("策略实验已取消")
        metrics_batch = (
            list(batch_evaluator(chunk, "holdout"))
            if batch_evaluator is not None
            else [evaluator(candidate, "holdout") for candidate in chunk]
        )
        if len(metrics_batch) != len(chunk):
            raise RuntimeError("批量候选评估返回数量不一致")
        for index, (candidate, raw_metrics) in enumerate(
            zip(chunk, metrics_batch, strict=True), start=offset + 1
        ):
            metrics = dict(raw_metrics)
            metrics["constraints_passed"] = _passes(metrics, request)
            holdout_by_hash[candidate.candidate_hash] = metrics
            if progress_callback is not None:
                progress_callback(index, len(holdout_candidates), "holdout")
    holdout_seconds = time.perf_counter() - holdout_started

    def finalize(items: Sequence[StrategyCandidate]) -> tuple[StrategyCandidate, ...]:
        result: list[StrategyCandidate] = []
        for candidate in items[:10]:
            metrics = holdout_by_hash.get(candidate.candidate_hash, {"error": "未执行最终留出"})
            result.append(candidate.with_metrics("holdout", metrics))
        return tuple(result)

    return DiscoveryRun(
        request=request,
        results_by_sharpe=finalize(valid_by_sharpe),
        results_by_return=finalize(valid_by_return),
        search_metadata=search_metadata,
        validation_metadata={
            "search_candidate_count": len(candidates),
            "validation_candidate_count": len(candidates),
            "validated_candidate_count": len(validated),
            "validation_candidate_limit_per_leaderboard": request.validation_candidates,
            "validation_sharpe_shortlist_count": len(sharpe_shortlist),
            "validation_return_shortlist_count": len(return_shortlist),
            "behavioral_deduplication": {
                "validation_sharpe_removed": len(ranked_by_sharpe) - len(valid_by_sharpe),
                "validation_return_removed": len(ranked_by_return) - len(valid_by_return),
            },
            "holdout_candidate_count": len(holdout_candidates),
            "leaderboard_period": "validation",
            "stage_durations_seconds": {
                "validation": validation_seconds,
                "holdout": holdout_seconds,
            },
            "stage_throughput_per_second": {
                "validation": total / validation_seconds if validation_seconds > 0 else 0.0,
                "holdout": (
                    len(holdout_candidates) / holdout_seconds if holdout_seconds > 0 else 0.0
                ),
            },
        },
    )


def _candidate_list(value: object) -> list[StrategyCandidate]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, StrategyCandidate)]
