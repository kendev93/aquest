"""TPE and optional LightGBM-assisted search over valid strategy candidates."""

from __future__ import annotations

import hashlib
import math
import os
import random
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .candidates import build_random_candidate
from .models import DiscoveryRequest, StrategyCandidate

BatchEvaluator = Callable[[Sequence[StrategyCandidate], str], Sequence[Mapping[str, object]]]


def _metric(metrics: Mapping[str, object], key: str, default: float = float("-inf")) -> float:
    try:
        raw = metrics.get(key, default)
        value = float(raw) if isinstance(raw, (int, float)) else float(str(raw))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _flatten(value: object, prefix: str = "") -> dict[str, float]:
    if isinstance(value, Mapping):
        result: dict[str, float] = {}
        for key in sorted(value, key=str):
            child = f"{prefix}.{key}" if prefix else str(key)
            result.update(_flatten(value[key], child))
        return result
    if isinstance(value, (list, tuple)):
        result = {f"{prefix}.__len__": float(len(value))}
        for index, item in enumerate(value):
            result.update(_flatten(item, f"{prefix}[{index}]"))
        return result
    if isinstance(value, bool):
        return {prefix: 1.0 if value else 0.0}
    try:
        number = float(value) if isinstance(value, (int, float)) else float(str(value))
    except (TypeError, ValueError):
        number = float("nan")
    if math.isfinite(number):
        return {prefix: number}
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]
    return {prefix: int(digest, 16) / float(16**12)}


class _TrialAdapter:
    def __init__(self, trial: Any) -> None:
        self._trial = trial
        self._counter = 0

    def _name(self) -> str:
        name = f"candidate_choice_{self._counter}"
        self._counter += 1
        return name

    def choice(self, values: Sequence[object]) -> object:
        options = list(values)
        if not options:
            return None
        index = self._trial.suggest_int(self._name(), 0, len(options) - 1)
        return options[int(index)]


class _LightGBMSurrogate:
    """Predict search-period scores only; never replaces real backtests."""

    _REFIT_INTERVAL = 25

    def __init__(self, *, strict: bool = False) -> None:
        self._model: Any = None
        self._feature_names: list[str] = []
        self._models: dict[str, Any] = {}
        self._feature_names_by_objective: dict[str, tuple[str, ...]] = {}
        self._fitted_counts: dict[str, int] = {}
        self._feature_cache: dict[str, dict[str, float]] = {}
        self._fit_count = 0
        self._strict = strict
        self.status = "unavailable"
        self.reason = ""

    @property
    def fit_count(self) -> int:
        return self._fit_count

    @property
    def refit_interval(self) -> int:
        return self._REFIT_INTERVAL

    def _features(self, candidate: StrategyCandidate) -> dict[str, float]:
        cached = self._feature_cache.get(candidate.candidate_hash)
        if cached is not None:
            return cached
        features = _flatten(candidate.to_config())
        self._feature_cache[candidate.candidate_hash] = features
        return features

    def fit(self, candidates: Sequence[StrategyCandidate], objective: str) -> bool:
        if len(candidates) < 10:
            self.status = "warming_up"
            self.reason = "有效样本不足"
            return False
        try:
            import lightgbm as lgb
            import numpy as np
        except Exception as exc:
            if self._strict:
                raise RuntimeError(
                    "策略实验室需要 LightGBM；请重新构建 Docker 镜像安装完整依赖"
                ) from exc
            self.status = "fallback"
            self.reason = f"LightGBM 不可用: {exc}"
            return False
        objective = str(objective)
        rows = [self._features(candidate) for candidate in candidates]
        feature_names = tuple(sorted({key for row in rows for key in row}))
        previous_model = self._models.get(objective)
        previous_names = self._feature_names_by_objective.get(objective, ())
        previous_count = self._fitted_counts.get(objective, 0)
        if (
            previous_model is not None
            and feature_names == previous_names
            and len(candidates) - previous_count < self._REFIT_INTERVAL
        ):
            self._model = previous_model
            self._feature_names = list(previous_names)
            self.status = "enabled"
            self.reason = "复用最近 surrogate 模型"
            return True
        self._feature_names = list(feature_names)
        targets = np.asarray(
            [
                _metric(candidate.metrics.get("search", {}), objective, 0.0)
                for candidate in candidates
            ],
            dtype=float,
        )
        if not self._feature_names or float(np.nanstd(targets)) <= 1e-12:
            self.status = "fallback"
            self.reason = "评分没有足够变化"
            return False
        matrix = np.asarray(
            [[row.get(key, 0.0) for key in self._feature_names] for row in rows], dtype=float
        )
        try:
            dataset = lgb.Dataset(
                matrix,
                label=targets,
                feature_name=[f"feature_{index}" for index in range(len(self._feature_names))],
                free_raw_data=True,
            )
            model = lgb.train(
                {
                    "objective": "regression",
                    "verbosity": -1,
                    "num_leaves": 15,
                    "learning_rate": 0.05,
                    "feature_fraction": 0.9,
                    "bagging_fraction": 0.9,
                    "bagging_freq": 1,
                    "num_threads": 1,
                },
                dataset,
                num_boost_round=60,
            )
        except Exception as exc:
            self._model = None
            if self._strict:
                raise RuntimeError("LightGBM surrogate 训练失败") from exc
            self.status = "fallback"
            self.reason = f"LightGBM 训练失败: {exc}"
            return False
        self._model = model
        self._models[objective] = model
        self._feature_names_by_objective[objective] = feature_names
        self._fitted_counts[objective] = len(candidates)
        self._fit_count += 1
        self.status = "enabled"
        self.reason = ""
        return True

    def predict(self, candidates: Sequence[StrategyCandidate]) -> list[float]:
        if self._model is None or not self._feature_names:
            return [float("-inf") for _ in candidates]
        try:
            import numpy as np

            matrix = np.asarray(
                [
                    [self._features(candidate).get(key, 0.0) for key in self._feature_names]
                    for candidate in candidates
                ],
                dtype=float,
            )
            return [float(value) for value in self._model.predict(matrix)]
        except Exception:
            return [float("-inf") for _ in candidates]


@dataclass(frozen=True)
class SearchResult:
    results_by_sharpe: tuple[StrategyCandidate, ...]
    results_by_return: tuple[StrategyCandidate, ...]
    search_metadata: Mapping[str, object]
    shortlist_by_sharpe: tuple[StrategyCandidate, ...] = ()
    shortlist_by_return: tuple[StrategyCandidate, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "results_by_sharpe": [candidate.to_dict() for candidate in self.results_by_sharpe],
            "results_by_return": [candidate.to_dict() for candidate in self.results_by_return],
            "shortlist_by_sharpe": [candidate.to_dict() for candidate in self.shortlist_by_sharpe],
            "shortlist_by_return": [candidate.to_dict() for candidate in self.shortlist_by_return],
            "search_metadata": dict(self.search_metadata),
        }


class HybridSearchEngine:
    """Bounded, cancellable, reproducible strategy candidate search."""

    def search(
        self,
        request: DiscoveryRequest,
        *,
        candidate_builder: Callable[
            [DiscoveryRequest, object], StrategyCandidate
        ] = build_random_candidate,
        evaluator: Callable[[StrategyCandidate, str], Mapping[str, object]],
        batch_evaluator: BatchEvaluator | None = None,
        fidelity_evaluator: Callable[[StrategyCandidate, str], Mapping[str, object]] | None = None,
        batch_size: int = 1,
        evaluation_workers: int = 1,
        evaluation_mode: str | None = None,
        convergence_min_trials: int | None = None,
        convergence_patience: int | None = None,
        convergence_enabled: bool | None = None,
        max_trials: int | None = None,
        seed: int | None = None,
        validation_limit: int = 50,
        holdout_limit: int = 20,
        progress_callback: Callable[[int, int, str], None] | None = None,
        abort_check: Callable[[], bool] | None = None,
    ) -> SearchResult:
        trial_limit = max(1, min(5_000, int(max_trials or request.max_trials)))
        resolved_batch_size = max(1, min(8, int(batch_size)))
        resolved_evaluation_workers = max(1, min(8, int(evaluation_workers)))
        resolved_seed = request.seed if seed is None else int(seed)
        if convergence_enabled is None:
            convergence_enabled = request.convergence_enabled or (
                convergence_min_trials is not None or convergence_patience is not None
            )
        if not isinstance(convergence_enabled, bool):
            raise ValueError("提前收敛开关必须是布尔值")
        resolved_convergence_min_trials = (
            _resolve_convergence_parameter(
                convergence_min_trials,
                trial_limit,
                default_ratio=0.25,
                default_floor=200,
            )
            if convergence_enabled
            else None
        )
        resolved_convergence_patience = (
            _resolve_convergence_parameter(
                convergence_patience,
                trial_limit,
                default_ratio=0.2,
                default_floor=100,
            )
            if convergence_enabled
            else None
        )
        rng = random.Random(resolved_seed)
        observed: list[StrategyCandidate] = []
        seen: set[str] = set()
        allow_fallback = _fallback_enabled()
        if not allow_fallback:
            _assert_ml_dependencies()
        surrogate = _LightGBMSurrogate(strict=not allow_fallback)
        optuna_module: Any = None
        studies: dict[str, Any] = {}
        if allow_fallback:
            surrogate.status = "fallback"
            surrogate.reason = "显式启用本地确定性回退"
        else:
            try:
                import optuna as optuna_module

                optuna_module.logging.set_verbosity(optuna_module.logging.WARNING)
                studies = {
                    objective: optuna_module.create_study(
                        direction="maximize",
                        sampler=optuna_module.samplers.TPESampler(seed=resolved_seed + index),
                    )
                    for index, objective in enumerate(("sharpe", "annual_return"))
                }
            except Exception as exc:
                raise RuntimeError(
                    "策略实验室需要 Optuna；请重新构建 Docker 镜像安装完整依赖"
                ) from exc

        started = time.monotonic()
        warmup = min(50, max(10, trial_limit))
        attempt_limit = max(20, math.ceil(trial_limit / resolved_batch_size) * 2)
        evaluated = 0
        attempts = 0
        duplicates = 0
        direct_trials = 0
        surrogate_trials = 0
        stop_reason = "completed"
        best_scores = {"sharpe": float("-inf"), "annual_return": float("-inf")}
        last_improvement = 0
        fidelity_counts: dict[str, int] = {"coarse": 0, "medium": 0, "full": 0}
        stage_durations: dict[str, float] = {
            "search": 0.0,
            "coarse": 0.0,
            "medium": 0.0,
            "full": 0.0,
        }
        evaluation_seconds: dict[str, float] = {"search": 0.0, "medium": 0.0, "full": 0.0}
        candidate_generation_seconds = 0.0
        surrogate_seconds = 0.0
        proposal_attempts = 0
        complexity_rejections = 0
        preflight_skipped = 0
        max_proposal_multiplier = 1

        def proposal_multiplier() -> int:
            """Adapt proposal oversampling to the observed duplicate rate."""

            if proposal_attempts < 20:
                return 4
            duplicate_rate = duplicates / proposal_attempts
            if duplicate_rate < 0.25:
                return 4
            if duplicate_rate < 0.5:
                return 5
            if duplicate_rate < 0.75:
                return 6
            if duplicate_rate < 0.9:
                return 6
            return 8

        def proposal_attempt_budget(target: int) -> int:
            nonlocal max_proposal_multiplier
            multiplier = proposal_multiplier()
            max_proposal_multiplier = max(max_proposal_multiplier, multiplier)
            return max(target + 8, target * multiplier)

        def build_candidate(generator: object) -> StrategyCandidate:
            nonlocal candidate_generation_seconds
            candidate_started = time.perf_counter()
            try:
                return candidate_builder(request, generator)
            finally:
                candidate_generation_seconds += time.perf_counter() - candidate_started

        def propose_optuna_candidates(
            objective_name: str,
            target: int,
        ) -> list[tuple[Any, StrategyCandidate]]:
            nonlocal proposal_attempts, duplicates, complexity_rejections
            proposals: list[tuple[Any, StrategyCandidate]] = []
            proposal_hashes: set[str] = set()
            max_attempts = proposal_attempt_budget(target)
            local_attempts = 0
            while len(proposals) < target and local_attempts < max_attempts:
                trial = studies[objective_name].ask()
                local_attempts += 1
                proposal_attempts += 1
                try:
                    candidate = build_candidate(_TrialAdapter(trial))
                except Exception:
                    studies[objective_name].tell(
                        trial,
                        state=optuna_module.trial.TrialState.PRUNED,
                    )
                    continue
                digest = candidate.candidate_hash
                if digest in seen or digest in proposal_hashes:
                    duplicates += 1
                    studies[objective_name].tell(
                        trial,
                        state=optuna_module.trial.TrialState.PRUNED,
                    )
                    continue
                if candidate.complexity > request.max_complexity:
                    complexity_rejections += 1
                    studies[objective_name].tell(
                        trial,
                        state=optuna_module.trial.TrialState.PRUNED,
                    )
                    continue
                proposal_hashes.add(digest)
                proposals.append((trial, candidate))
            return proposals

        def propose_random_candidates(target: int) -> list[tuple[Any, StrategyCandidate]]:
            nonlocal proposal_attempts, duplicates, complexity_rejections
            proposals: list[tuple[Any, StrategyCandidate]] = []
            proposal_hashes: set[str] = set()
            local_attempts = 0
            max_attempts = proposal_attempt_budget(target)
            while len(proposals) < target and local_attempts < max_attempts:
                local_attempts += 1
                proposal_attempts += 1
                candidate = build_candidate(rng)
                digest = candidate.candidate_hash
                if digest in seen or digest in proposal_hashes:
                    duplicates += 1
                    continue
                if candidate.complexity > request.max_complexity:
                    complexity_rejections += 1
                    continue
                proposal_hashes.add(digest)
                proposals.append((None, candidate))
            return proposals

        def evaluate_stage_batch(
            candidates: Sequence[StrategyCandidate], stage: str
        ) -> list[Mapping[str, object]]:
            evaluation_started = time.perf_counter()
            try:
                if batch_evaluator is not None:
                    metrics = list(batch_evaluator(candidates, stage))
                elif stage in {"coarse", "medium", "full"} and fidelity_evaluator is not None:
                    metrics = [fidelity_evaluator(candidate, stage) for candidate in candidates]
                else:
                    metrics = [evaluator(candidate, stage) for candidate in candidates]
            finally:
                stage_key = stage if stage in evaluation_seconds else "search"
                evaluation_seconds[stage_key] += time.perf_counter() - evaluation_started
            if len(metrics) != len(candidates):
                raise RuntimeError("批量候选评估返回数量不一致")
            return [dict(item) for item in metrics]

        search_started = time.perf_counter()
        while evaluated < trial_limit and attempts < attempt_limit:
            attempts += 1
            if abort_check is not None and abort_check():
                stop_reason = "cancelled"
                break
            if time.monotonic() - started >= request.timeout_seconds:
                stop_reason = "timeout"
                break
            objective = "sharpe" if evaluated % 2 == 0 else "annual_return"
            desired = min(resolved_batch_size, trial_limit - evaluated)
            proposals: list[tuple[Any, StrategyCandidate]] = []
            if optuna_module is not None and evaluated >= warmup:
                surrogate_started = time.perf_counter()
                surrogate.fit(observed, objective)
                surrogate_seconds += time.perf_counter() - surrogate_started
                proposal_count = max(8, desired * proposal_multiplier())
                proposals = propose_optuna_candidates(objective, proposal_count)
                if proposals:
                    predictions = surrogate.predict([candidate for _, candidate in proposals])
                    ranked_indexes = sorted(
                        range(len(proposals)),
                        key=lambda index: (predictions[index], -index),
                        reverse=True,
                    )
                    selected_indexes = ranked_indexes[:desired]
                    if len(ranked_indexes) > desired and rng.random() < 0.2:
                        selected_indexes[-1] = rng.choice(ranked_indexes[desired:])
                    selected_set = set(selected_indexes)
                    for index, (trial, _candidate) in enumerate(proposals):
                        if index not in selected_set:
                            studies[objective].tell(
                                trial, state=optuna_module.trial.TrialState.PRUNED
                            )
                    proposals = [proposals[index] for index in selected_indexes]
                    surrogate_trials += len(proposals)
            if not proposals:
                direct_trials += desired
                proposals = (
                    propose_optuna_candidates(objective, desired)
                    if optuna_module is not None
                    else propose_random_candidates(desired)
                )

            accepted: list[tuple[Any, StrategyCandidate]] = []
            for trial, candidate in proposals:
                digest = candidate.candidate_hash
                if digest in seen:
                    duplicates += 1
                    if trial is not None and optuna_module is not None:
                        studies[objective].tell(trial, state=optuna_module.trial.TrialState.PRUNED)
                    continue
                seen.add(digest)
                if candidate.complexity > request.max_complexity:
                    if trial is not None and optuna_module is not None:
                        studies[objective].tell(trial, state=optuna_module.trial.TrialState.PRUNED)
                    continue
                accepted.append((trial, candidate))
            if not accepted:
                continue
            evaluation_stage = (
                "coarse"
                if fidelity_evaluator is not None and request.multifidelity_enabled
                else "search"
            )
            metrics_batch = evaluate_stage_batch(
                [candidate for _, candidate in accepted], evaluation_stage
            )
            if evaluation_stage == "coarse":
                fidelity_counts["coarse"] += len(accepted)
            for (trial, candidate), raw_metrics in zip(accepted, metrics_batch, strict=True):
                candidate = candidate.with_metrics("search", raw_metrics)
                if evaluation_stage == "coarse":
                    candidate = candidate.with_metrics("coarse", raw_metrics)
                evaluated += 1
                if raw_metrics.get("preflight_skipped") is True:
                    preflight_skipped += 1
                if trial is not None and optuna_module is not None:
                    studies[objective].tell(
                        trial,
                        _metric(raw_metrics, objective, -1e9),
                    )
                if _passes_constraints(
                    raw_metrics,
                    request,
                    min_trades=_fidelity_min_trades(request, "coarse")
                    if fidelity_evaluator is not None and request.multifidelity_enabled
                    else None,
                ):
                    observed.append(candidate)
                improved = False
                for metric_name in best_scores:
                    score = _metric(raw_metrics, metric_name)
                    if score > best_scores[metric_name] + 1e-9:
                        best_scores[metric_name] = score
                        improved = True
                if improved:
                    last_improvement = evaluated
                if progress_callback is not None:
                    progress_callback(evaluated, trial_limit, "searching")
            if (
                resolved_convergence_min_trials is not None
                and resolved_convergence_patience is not None
                and evaluated >= resolved_convergence_min_trials
                and evaluated - last_improvement >= resolved_convergence_patience
            ):
                stop_reason = "converged"
                break
        stage_durations["search"] = time.perf_counter() - search_started
        if fidelity_evaluator is not None and request.multifidelity_enabled:
            stage_durations["coarse"] = stage_durations["search"]
        if evaluated < trial_limit and stop_reason == "completed":
            stop_reason = "attempt_limit"

        final_observed = observed
        if fidelity_evaluator is not None and request.multifidelity_enabled and observed:
            medium_limit = max(1, min(len(observed), math.ceil(len(observed) * 0.35)))
            medium_source = _rank_candidates(observed, "search")[:medium_limit]
            medium_observed: list[StrategyCandidate] = []
            medium_started = time.perf_counter()
            for offset in range(0, len(medium_source), resolved_batch_size):
                chunk = medium_source[offset : offset + resolved_batch_size]
                if abort_check is not None and abort_check():
                    stop_reason = "cancelled"
                    break
                if time.monotonic() - started >= request.timeout_seconds:
                    stop_reason = "timeout"
                    break
                metrics_batch = evaluate_stage_batch(chunk, "medium")
                fidelity_counts["medium"] += len(chunk)
                for index, (candidate, metrics) in enumerate(
                    zip(chunk, metrics_batch, strict=True), start=offset + 1
                ):
                    enriched = candidate.with_metrics("medium", metrics)
                    if _passes_constraints(
                        metrics,
                        request,
                        min_trades=_fidelity_min_trades(request, "medium"),
                    ):
                        medium_observed.append(enriched)
                    if progress_callback is not None:
                        progress_callback(index, len(medium_source), "refining_medium")
            stage_durations["medium"] = time.perf_counter() - medium_started
            full_source = medium_observed or medium_source
            full_limit = max(1, min(len(full_source), math.ceil(len(observed) * 0.15)))
            full_stage = "medium" if medium_observed else "search"
            full_candidates = _rank_candidates(full_source, full_stage)[:full_limit]
            full_observed: list[StrategyCandidate] = []
            full_started = time.perf_counter()
            for offset in range(0, len(full_candidates), resolved_batch_size):
                chunk = full_candidates[offset : offset + resolved_batch_size]
                if abort_check is not None and abort_check():
                    stop_reason = "cancelled"
                    break
                if time.monotonic() - started >= request.timeout_seconds:
                    stop_reason = "timeout"
                    break
                metrics_batch = evaluate_stage_batch(chunk, "full")
                fidelity_counts["full"] += len(chunk)
                for index, (candidate, metrics) in enumerate(
                    zip(chunk, metrics_batch, strict=True), start=offset + 1
                ):
                    enriched = candidate.with_metrics("full", metrics)
                    if _passes_constraints(metrics, request):
                        full_observed.append(enriched)
                    if progress_callback is not None:
                        progress_callback(index, len(full_candidates), "refining_full")
            stage_durations["full"] = time.perf_counter() - full_started
            final_observed = full_observed

        def ranking_metrics(candidate: StrategyCandidate) -> Mapping[str, object]:
            return (
                candidate.metrics.get("full")
                or candidate.metrics.get("medium")
                or candidate.metrics.get("search", {})
            )

        by_sharpe = tuple(
            sorted(
                final_observed,
                key=lambda item: (
                    _metric(ranking_metrics(item), "sharpe"),
                    _metric(ranking_metrics(item), "annual_return"),
                    -item.complexity,
                ),
                reverse=True,
            )
        )
        by_return = tuple(
            sorted(
                final_observed,
                key=lambda item: (
                    _metric(ranking_metrics(item), "annual_return"),
                    _metric(ranking_metrics(item), "sharpe"),
                    -item.complexity,
                ),
                reverse=True,
            )
        )
        metadata = {
            "algorithm": "optuna_tpe_lightgbm_surrogate"
            if optuna_module is not None
            else "seeded_random_fallback",
            "algorithm_version": "1",
            "seed": resolved_seed,
            "fallback_enabled": allow_fallback,
            "evaluation_workers": resolved_evaluation_workers,
            "evaluation_batch_size": resolved_batch_size,
            "evaluation_mode": evaluation_mode
            or ("batch" if batch_evaluator is not None else "serial"),
            "max_trials": trial_limit,
            "attempts": attempts,
            "attempt_limit": attempt_limit,
            "requested_trials": trial_limit,
            "evaluated_trials": evaluated,
            "unique_evaluated_trials": evaluated,
            "valid_candidates": len(observed),
            "duplicate_trials": duplicates,
            "direct_trials": direct_trials,
            "surrogate_trials": surrogate_trials,
            "surrogate_fit_count": surrogate.fit_count,
            "surrogate_status": surrogate.status,
            "surrogate_enabled": surrogate.status == "enabled",
            "surrogate_reason": surrogate.reason,
            "validation_candidates": validation_limit,
            "holdout_candidates": holdout_limit,
            "convergence_enabled": bool(convergence_enabled),
            "convergence_min_trials": resolved_convergence_min_trials,
            "convergence_patience": resolved_convergence_patience,
            "multifidelity_plan": [
                {"name": "coarse", "data_fraction": 0.25, "keep_ratio": None},
                {"name": "medium", "data_fraction": 0.5, "keep_ratio": 0.35},
                {"name": "full", "data_fraction": 1.0, "keep_ratio": 0.15},
            ],
            "stop_reason": stop_reason,
            "fidelity_evaluations": fidelity_counts,
            "promoted_to_medium": fidelity_counts["medium"],
            "promoted_to_full": fidelity_counts["full"],
            "final_valid_candidates": len(final_observed),
            "stage_durations_seconds": stage_durations,
            "coarse_seconds": stage_durations["coarse"],
            "candidate_generation_seconds": candidate_generation_seconds,
            "proposal_attempts": proposal_attempts,
            "unique_proposal_rate": (
                evaluated / proposal_attempts if proposal_attempts > 0 else 0.0
            ),
            "complexity_rejections": complexity_rejections,
            "max_proposal_multiplier": max_proposal_multiplier,
            "preflight_skipped": preflight_skipped,
            "surrogate_refit_interval": surrogate.refit_interval,
            "surrogate_seconds": surrogate_seconds,
            "evaluation_durations_seconds": evaluation_seconds,
            "stage_throughput_per_second": {
                key: (
                    float(
                        evaluated
                        if key == "search" and not request.multifidelity_enabled
                        else fidelity_counts["coarse"]
                        if key == "search" and request.multifidelity_enabled
                        else fidelity_counts.get(key, 0)
                    )
                    / duration
                    if duration > 0
                    else 0.0
                )
                for key, duration in stage_durations.items()
            },
        }
        return SearchResult(
            results_by_sharpe=by_sharpe[:10],
            results_by_return=by_return[:10],
            search_metadata=metadata,
            shortlist_by_sharpe=by_sharpe[: max(10, validation_limit)],
            shortlist_by_return=by_return[: max(10, validation_limit)],
        )


def _resolve_convergence_parameter(
    value: int | None,
    trial_limit: int,
    *,
    default_ratio: float,
    default_floor: int,
) -> int | None:
    if value is None and trial_limit < 400:
        return None
    if value is None:
        resolved = max(default_floor, math.ceil(trial_limit * default_ratio))
    else:
        resolved = int(value)
    if resolved <= 0:
        return None
    return min(trial_limit, resolved)


def _passes_constraints(
    metrics: Mapping[str, object],
    request: DiscoveryRequest,
    *,
    min_trades: int | None = None,
) -> bool:
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
    if trades < (request.min_trades if min_trades is None else min_trades):
        return False
    if request.max_drawdown is not None:
        drawdown = abs(_metric(metrics, "max_drawdown", float("inf")))
        if not math.isfinite(drawdown) or drawdown > request.max_drawdown:
            return False
    if (
        request.max_turnover is not None
        and _metric(metrics, "turnover", float("inf")) > request.max_turnover
    ):
        return False
    return True


def _fallback_enabled() -> bool:
    value = os.getenv("INDICATOR_LAB_DISCOVERY_ALLOW_FALLBACK", "0")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _fidelity_min_trades(request: DiscoveryRequest, fidelity: str) -> int:
    fraction = {"coarse": 0.25, "medium": 0.5}.get(fidelity, 1.0)
    return min(request.min_trades, max(1, math.ceil(request.min_trades * fraction)))


def _assert_ml_dependencies() -> None:
    try:
        import lightgbm  # noqa: F401
        import optuna  # noqa: F401
    except Exception as exc:
        raise RuntimeError(
            "策略实验室需要 Optuna 和 LightGBM；请重新构建 Docker 镜像安装完整依赖"
        ) from exc


def _rank_candidates(
    candidates: Sequence[StrategyCandidate], stage: str
) -> list[StrategyCandidate]:
    return sorted(
        candidates,
        key=lambda item: (
            _metric(item.metrics.get(stage, {}), "sharpe"),
            _metric(item.metrics.get(stage, {}), "annual_return"),
            -item.complexity,
        ),
        reverse=True,
    )
