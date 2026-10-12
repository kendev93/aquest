"""Orchestration between candidate search and the local portfolio backtester."""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import math
import multiprocessing
import os
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from datetime import date, timedelta
from typing import Protocol, cast

from aquest.indicators.cache import IndicatorCache
from aquest.individual_research.search_policy import FULL_LIMIT, MEDIUM_LIMIT
from aquest.market_data.adapter import DuckDbMarketDataAdapter, MarketDataAdapter
from aquest.market_data.models import InstrumentBoard, InstrumentType
from aquest.market_data.store import DuckDbMarketDataStore
from aquest.portfolio_backtest.models import (
    PortfolioBacktestConfig,
    PortfolioBacktestReport,
)
from aquest.portfolio_backtest.service import (
    PortfolioBacktestService,
    SimulationCheckpoint,
)
from aquest.screening.jobs import TaskCancelledError
from aquest.web.schemas import PortfolioBacktestRequest

from .candidates import build_random_candidate
from .catalog import default_indicator_catalog
from .evaluation import run_validation_pipeline
from .evaluation_budget import EvaluationWorkerBudget
from .models import DiscoveryRequest, StrategyCandidate
from .refinement import (
    RefinementRadius,
    build_refined_candidate,
    candidate_from_payload,
    discovery_request_from_dict,
)
from .replay import ReplayLeaderboard
from .replay_service import ReplayProgressCallback, StrategyReplayService
from .resources import (
    resolve_discovery_workers,
    validate_discovery_workers,
)
from .search import BatchEvaluator, HybridSearchEngine
from .snapshot import (
    DiscoverySnapshot,
    DiscoverySnapshotCache,
    _request_refs,
    build_discovery_snapshot,
)

ProgressCallback = Callable[[int, int, str, Mapping[str, object]], None]
DISCOVERY_EVALUATION_WORKERS = resolve_discovery_workers()

_WORKER_DISCOVERY_SERVICE: StrategyDiscoveryService | None = None


def _init_discovery_worker(
    database_path: str,
    snapshot_path: str | None = None,
) -> None:
    global _WORKER_DISCOVERY_SERVICE
    worker_store = DuckDbMarketDataStore(database_path, read_only=True)
    market_adapter = DuckDbMarketDataAdapter(worker_store)
    worker_portfolio = PortfolioBacktestService(
        adapter=market_adapter,
        indicator_cache=IndicatorCache(),
    )
    _WORKER_DISCOVERY_SERVICE = StrategyDiscoveryService(
        portfolio_service=worker_portfolio,
        store=worker_store,
        market_data_adapter=market_adapter,
        discovery_snapshot=(DiscoverySnapshot(snapshot_path) if snapshot_path else None),
    )


def _evaluate_candidate_process(
    job: tuple[object, ...],
) -> Mapping[str, object]:
    # 旧版本在 database_path 之后还带一个外部共享行情库路径。那一整套共享
    # 行情集成已经移除，这里容忍历史元组形状并丢掉多余的字段。
    if len(job) == 7:
        job = (*job[:5], job[6])
    candidate, request, stage, fidelity, database_path, checkpoint = cast(
        tuple[
            StrategyCandidate,
            DiscoveryRequest,
            str,
            str | None,
            str,
            Mapping[str, object] | None,
        ],
        job,
    )
    if _WORKER_DISCOVERY_SERVICE is None:
        _init_discovery_worker(database_path)
    assert _WORKER_DISCOVERY_SERVICE is not None
    split = "search" if stage in {"coarse", "medium", "full"} else stage
    return _WORKER_DISCOVERY_SERVICE._evaluate_candidate(
        candidate,
        request,
        split,
        fidelity=fidelity,
        checkpoint=checkpoint,
    )


class PortfolioServiceLike(Protocol):
    def run(
        self, config: PortfolioBacktestConfig
    ) -> PortfolioBacktestReport | dict[str, object]: ...


class StrategyDiscoveryService:
    """Run bounded strategy discovery against local DuckDB market data."""

    def __init__(
        self,
        *,
        portfolio_service: PortfolioServiceLike | None = None,
        store: DuckDbMarketDataStore | None = None,
        market_data_adapter: MarketDataAdapter | None = None,
        discovery_snapshot: DiscoverySnapshot | None = None,
        evaluation_workers: int | None = None,
        persist_indicator_cache: bool = True,
        discovery_snapshot_cache: DiscoverySnapshotCache | None = None,
        evaluation_worker_budget: EvaluationWorkerBudget | None = None,
    ) -> None:
        selected_store = store or DuckDbMarketDataStore()
        selected_market_adapter = market_data_adapter or getattr(
            portfolio_service, "_adapter", None
        )
        if selected_market_adapter is None:
            selected_market_adapter = DuckDbMarketDataAdapter(selected_store)
        self._market_data_adapter = cast(MarketDataAdapter, selected_market_adapter)
        self._portfolio_service = portfolio_service or PortfolioBacktestService(
            adapter=self._market_data_adapter,
            indicator_cache=IndicatorCache(selected_store),
        )
        self._store = selected_store
        self._discovery_snapshot = discovery_snapshot
        self._persist_indicator_cache = persist_indicator_cache
        self._discovery_snapshot_cache = discovery_snapshot_cache
        self._evaluation_worker_budget = evaluation_worker_budget
        self._evaluation_workers = validate_discovery_workers(
            resolve_discovery_workers() if evaluation_workers is None else evaluation_workers
        )
        self._parallel_enabled = isinstance(
            self._portfolio_service, PortfolioBacktestService
        ) and all(
            callable(getattr(self._market_data_adapter, name, None))
            for name in ("read_many_stocks", "count_many_stocks")
        )
        self._replay_service = StrategyReplayService(
            portfolio_service=self._portfolio_service,
            store=self._store,
            market_data_adapter=self._market_data_adapter,
            evaluation_workers=self._evaluation_workers,
            persist_indicator_cache=persist_indicator_cache,
        )

    def capabilities(self) -> dict[str, object]:
        catalog = default_indicator_catalog()
        optuna_available = importlib.util.find_spec("optuna") is not None
        lightgbm_available = importlib.util.find_spec("lightgbm") is not None
        fallback_enabled = _fallback_enabled()
        ml_available = optuna_available and lightgbm_available
        return {
            "enabled": ml_available or fallback_enabled,
            "strategy_type": "portfolio",
            "frequency": "daily",
            "evaluation_mode": "process_pool" if self._parallel_enabled else "serial",
            "evaluation_workers": (self._evaluation_workers if self._parallel_enabled else 1),
            "data_snapshot": "shared_memmap" if self._parallel_enabled else "none",
            "indicator_count": len(catalog.values),
            "signal_count": len(catalog.signal_ids),
            "indicators": [
                {
                    "id": item.id,
                    "display_name": item.display_name,
                    "rankable": item.rankable,
                }
                for item in catalog.values
            ],
            "signals": [
                {
                    "id": item.id,
                    "display_name": item.display_name,
                    "description": item.description,
                }
                for item in catalog.signals
            ],
            "operators": ["gt", "gte", "lt", "lte", "eq", "neq", "cross_up", "cross_down"],
            "optuna_available": optuna_available,
            "lightgbm_available": lightgbm_available,
            "fallback_enabled": fallback_enabled,
            "algorithm": (
                "optuna_tpe_lightgbm_surrogate"
                if ml_available
                else "seeded_random_fallback"
                if fallback_enabled
                else "docker_dependencies_required"
            ),
            "versions": {
                "optuna": _package_version("optuna"),
                "lightgbm": _package_version("lightgbm"),
            },
        }

    def replay(
        self,
        experiment: Mapping[str, object],
        leaderboard: ReplayLeaderboard,
        *,
        progress_callback: ReplayProgressCallback | None = None,
        abort_check: Callable[[], bool] | None = None,
    ) -> dict[str, object]:
        """Replay one leaderboard against the latest completed local history."""

        if leaderboard not in {"sharpe", "return"}:
            raise ValueError("策略复核榜单类型无效")
        return self._replay_service.run(
            experiment,
            leaderboard,
            progress_callback=progress_callback,
            abort_check=abort_check,
        )

    def run(
        self,
        request: DiscoveryRequest,
        *,
        progress_callback: ProgressCallback | None = None,
        abort_check: Callable[[], bool] | None = None,
        candidate_builder: Callable[
            [DiscoveryRequest, object], StrategyCandidate
        ] = build_random_candidate,
        result_metadata: Mapping[str, object] | None = None,
        expected_data_fingerprint: str | None = None,
    ) -> dict[str, object]:
        started = time.monotonic()
        snapshot_metadata: Mapping[str, object] = {}
        if self._parallel_enabled:
            self._assert_local_data(request)
            report_snapshot_progress: Callable[[int, int], None] | None = None
            if progress_callback is not None:
                estimated_total = _progress_plan(request)[2]

                def report_snapshot_progress(current: int, total: int) -> None:
                    progress_callback(
                        current,
                        total,
                        "preparing_snapshot",
                        {
                            "overall_progress": 0.0,
                            "phase_current": current,
                            "phase_total": total,
                            "phase_label": "准备行情和指标快照",
                            "estimated_total_evaluations": estimated_total,
                            "completed_work_units": 0.0,
                            "elapsed_seconds": max(0.0, time.monotonic() - started),
                            "throughput_per_second": 0.0,
                            "estimated_seconds_remaining": None,
                        },
                    )

                report_snapshot_progress(0, 1)
            snapshot_started = time.monotonic()
            with build_discovery_snapshot(
                self._store,
                request,
                adapter=self._market_data_adapter,
                progress_callback=report_snapshot_progress,
                abort_check=abort_check,
                persist_indicator_cache=self._persist_indicator_cache,
                cache=self._discovery_snapshot_cache,
            ) as snapshot:
                snapshot_build_seconds = max(0.0, time.monotonic() - snapshot_started)
                snapshot_metadata = dict(snapshot.build_metadata)
                if progress_callback is not None:
                    progress_callback(
                        1,
                        1,
                        "snapshot_ready",
                        {
                            "overall_progress": 0.0,
                            "phase_current": 1,
                            "phase_total": 1,
                            "phase_label": (
                                "快照已准备，等待可用计算资源"
                                if self._evaluation_worker_budget is not None
                                else "快照已准备，开始寻优"
                            ),
                            "estimated_total_evaluations": _progress_plan(request)[2],
                            "completed_work_units": 0.0,
                            "elapsed_seconds": max(0.0, time.monotonic() - started),
                            "throughput_per_second": 0.0,
                            "estimated_seconds_remaining": None,
                            "snapshot_build_seconds": snapshot_build_seconds,
                            "snapshot_cache_hit": snapshot_metadata.get(
                                "snapshot_cache_hit", False
                            ),
                        },
                    )
                with self._evaluation_executor(
                    str(snapshot.root), abort_check=abort_check
                ) as evaluation_executor:
                    result = self._run_impl(
                        request,
                        progress_callback=progress_callback,
                        abort_check=abort_check,
                        evaluation_executor=evaluation_executor,
                        started=started,
                        snapshot_build_seconds=snapshot_build_seconds,
                        snapshot_metadata=snapshot_metadata,
                        candidate_builder=candidate_builder,
                        result_metadata=result_metadata,
                        expected_data_fingerprint=expected_data_fingerprint,
                    )
        else:
            with self._evaluation_executor() as evaluation_executor:
                result = self._run_impl(
                    request,
                    progress_callback=progress_callback,
                    abort_check=abort_check,
                    evaluation_executor=evaluation_executor,
                    started=started,
                    snapshot_metadata=snapshot_metadata,
                    candidate_builder=candidate_builder,
                    result_metadata=result_metadata,
                    expected_data_fingerprint=expected_data_fingerprint,
                )
        if request.candidate_mode == "risk_only":
            result["mode"] = "risk_optimization"
            result["configuration_fingerprint"] = hashlib.sha256(
                json.dumps(
                    dict(request.base_config),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            _expose_training_metrics(result)
        return result

    def refine(
        self,
        experiment: Mapping[str, object],
        parent_candidate_hash: str,
        *,
        max_trials: int | None = None,
        seed: int | None = None,
        radius: RefinementRadius = "medium",
        progress_callback: ProgressCallback | None = None,
        abort_check: Callable[[], bool] | None = None,
    ) -> dict[str, object]:
        """Search a bounded neighborhood around one stored parent candidate."""

        raw_request = experiment.get("request")
        if not isinstance(raw_request, Mapping):
            raise ValueError("父实验请求内容无效")
        discovery_request = discovery_request_from_dict(raw_request)
        parent = _find_parent_candidate(experiment, parent_candidate_hash)
        parent_candidate = candidate_from_payload(parent, base_config=discovery_request.base_config)
        resolved_request = replace(
            discovery_request,
            required_buy_conditions=(),
            required_sell_conditions=(),
            max_trials=max_trials or discovery_request.max_trials,
            seed=discovery_request.seed if seed is None else seed,
        )
        source_fingerprint = str(experiment.get("data_fingerprint") or "")

        def builder(request: DiscoveryRequest, rng: object) -> StrategyCandidate:
            return build_refined_candidate(parent_candidate, request, rng, radius=radius)

        result = self.run(
            resolved_request,
            progress_callback=progress_callback,
            abort_check=abort_check,
            candidate_builder=builder,
            result_metadata={
                "mode": "refinement",
                "refinement": {
                    "parent_history_id": str(
                        experiment.get("history_id") or experiment.get("experiment_id") or ""
                    ),
                    "parent_candidate_hash": parent_candidate_hash,
                    "refinement_radius": radius,
                    "parent_candidate": parent_candidate.to_dict(),
                    "parent_metrics": (
                        dict(parent_metrics)
                        if isinstance(parent_metrics := parent.get("metrics"), Mapping)
                        else {}
                    ),
                },
            },
        )
        request_payload = result.get("request")
        refinement_payload = result.get("refinement")
        if isinstance(request_payload, dict) and isinstance(refinement_payload, Mapping):
            request_payload["mode"] = "refinement"
            request_payload["refinement"] = {
                "parent_history_id": refinement_payload.get("parent_history_id"),
                "parent_candidate_hash": refinement_payload.get("parent_candidate_hash"),
                "refinement_radius": refinement_payload.get("refinement_radius"),
            }
        refinement = result.get("refinement")
        if isinstance(refinement, dict):
            refinement["parent_data_fingerprint"] = source_fingerprint
            current_fingerprint = str(result.get("data_fingerprint") or "")
            refinement["data_changed"] = bool(
                source_fingerprint
                and current_fingerprint
                and source_fingerprint != current_fingerprint
            )
        return result

    @contextmanager
    def _evaluation_executor(
        self, snapshot_path: str | None = None, *, abort_check: Callable[[], bool] | None = None
    ) -> Iterator[ProcessPoolExecutor | None]:
        if not self._parallel_enabled:
            yield None
            return
        requested = self._evaluation_workers
        lease = (
            self._evaluation_worker_budget.lease(requested, abort_check)
            if self._evaluation_worker_budget is not None
            else nullcontext(requested)
        )
        with lease as workers:
            self._evaluation_workers = workers
            try:
                database_path = str(self._store.database_path)
                executor = ProcessPoolExecutor(
                    max_workers=self._evaluation_workers,
                    mp_context=multiprocessing.get_context("spawn"),
                    initializer=_init_discovery_worker,
                    initargs=(database_path, snapshot_path),
                )
                try:
                    yield executor
                finally:
                    executor.shutdown(wait=True, cancel_futures=True)
            finally:
                self._evaluation_workers = requested

    def _assert_local_data(self, request: DiscoveryRequest) -> None:
        base = request.base_config
        universe = str(base.get("universe") or "all")
        universe_file_value = base.get("universe_file")
        universe_file = str(universe_file_value) if universe_file_value else None
        instrument_types = cast(
            tuple[InstrumentType, ...] | None,
            _string_tuple(base.get("instrument_types")),
        )
        boards = cast(
            tuple[InstrumentBoard, ...] | None,
            _string_tuple(base.get("boards")),
        )
        refs = _request_refs(
            self._market_data_adapter,
            base,
            universe=universe,
            universe_file=universe_file,
            instrument_types=instrument_types,
            boards=boards,
        )
        if not refs:
            raise ValueError("行情数据库没有可用日线，请先同步行情")

    def _run_impl(
        self,
        request: DiscoveryRequest,
        *,
        progress_callback: ProgressCallback | None = None,
        abort_check: Callable[[], bool] | None = None,
        evaluation_executor: ProcessPoolExecutor | None = None,
        started: float | None = None,
        snapshot_build_seconds: float = 0.0,
        snapshot_metadata: Mapping[str, object] | None = None,
        candidate_builder: Callable[
            [DiscoveryRequest, object], StrategyCandidate
        ] = build_random_candidate,
        result_metadata: Mapping[str, object] | None = None,
        expected_data_fingerprint: str | None = None,
    ) -> dict[str, object]:
        started = time.monotonic() if started is None else started
        fingerprint_holder = [""]
        best_metrics: dict[str, float | None] = {
            "sharpe": None,
            "annual_return": None,
        }
        progress_plan = _progress_plan(request)
        fidelity_checkpoints: dict[str, Mapping[str, object]] = {}
        checkpoint_metrics = {
            "captured": 0,
            "resume_attempts": 0,
            "fallbacks": 0,
        }

        def observe_metrics(metrics: Mapping[str, object], split: str) -> None:
            if split not in {"search", "validation", "holdout"}:
                return
            for key in best_metrics:
                try:
                    raw = metrics.get(key, float("nan"))
                    value = float(raw) if isinstance(raw, (int, float)) else float(str(raw))
                except (TypeError, ValueError):
                    continue
                if not value == value:
                    continue
                current = best_metrics[key]
                if current is None or value > current:
                    best_metrics[key] = value

        def report_progress(current: int, total: int, stage: str) -> None:
            if progress_callback is not None:
                metadata = _progress_metadata(
                    current,
                    total,
                    stage,
                    progress_plan,
                    started,
                )
                metadata.update(
                    {
                        "best_sharpe": best_metrics["sharpe"],
                        "best_return": best_metrics["annual_return"],
                    }
                )
                progress_callback(
                    current,
                    total,
                    stage,
                    metadata,
                )

        def evaluate(candidate: StrategyCandidate, split: str) -> Mapping[str, object]:
            metrics = self._evaluate_candidate(
                candidate,
                request,
                split,
                data_fingerprint_holder=fingerprint_holder,
            )
            _validate_expected_fingerprint(metrics, expected_data_fingerprint)
            observe_metrics(metrics, split)
            return metrics

        def _prepare_fidelity_metrics(
            candidate: StrategyCandidate,
            metrics: Mapping[str, object],
        ) -> dict[str, object]:
            sanitized = dict(metrics)
            raw_checkpoint = sanitized.pop("_simulation_checkpoint", None)
            if isinstance(raw_checkpoint, Mapping):
                fidelity_checkpoints[candidate.candidate_hash] = dict(raw_checkpoint)
                checkpoint_metrics["captured"] += 1
            if sanitized.pop("_checkpoint_resume_attempted", False) is True:
                checkpoint_metrics["resume_attempts"] += 1
            if sanitized.pop("_checkpoint_fallback", False) is True:
                checkpoint_metrics["fallbacks"] += 1
            return sanitized

        def evaluate_fidelity(candidate: StrategyCandidate, fidelity: str) -> Mapping[str, object]:
            metrics = self._evaluate_candidate(
                candidate,
                request,
                "search",
                fidelity=fidelity,
                checkpoint=(
                    fidelity_checkpoints.get(candidate.candidate_hash)
                    if fidelity in {"medium", "full"}
                    else None
                ),
                data_fingerprint_holder=fingerprint_holder,
            )
            metrics = _prepare_fidelity_metrics(candidate, metrics)
            _validate_expected_fingerprint(metrics, expected_data_fingerprint)
            observe_metrics(metrics, "search")
            return metrics

        def retain_candidate_checkpoints(candidates: Sequence[StrategyCandidate]) -> None:
            retained_hashes = {candidate.candidate_hash for candidate in candidates}
            for candidate_hash in tuple(fidelity_checkpoints):
                if candidate_hash not in retained_hashes:
                    fidelity_checkpoints.pop(candidate_hash, None)

        batch_evaluator: BatchEvaluator | None = None
        if evaluation_executor is not None:
            database_path = str(self._store.database_path)
            executor = evaluation_executor

            def evaluate_batch(
                candidates: Sequence[StrategyCandidate], stage: str
            ) -> Sequence[Mapping[str, object]]:
                fidelity = stage if stage in {"coarse", "medium", "full"} else None
                jobs = [
                    (
                        candidate,
                        request,
                        stage,
                        fidelity,
                        database_path,
                        fidelity_checkpoints.get(candidate.candidate_hash)
                        if fidelity in {"medium", "full"}
                        else None,
                    )
                    for candidate in candidates
                ]
                try:
                    metrics = list(executor.map(_evaluate_candidate_process, jobs, chunksize=1))
                except (BrokenProcessPool, OSError) as exc:
                    raise RuntimeError(
                        f"策略实验的 {self._evaluation_workers} 个评估进程启动失败，"
                        "请检查 Docker 运行时和行情数据库权限"
                    ) from exc
                sanitized_metrics: list[Mapping[str, object]] = []
                for candidate, item in zip(candidates, metrics, strict=True):
                    sanitized_metrics.append(_prepare_fidelity_metrics(candidate, item))
                metrics = sanitized_metrics
                observed_stage = "search" if fidelity is not None else stage
                for item in metrics:
                    _validate_expected_fingerprint(item, expected_data_fingerprint)
                    observe_metrics(item, observed_stage)
                    data_fingerprint = item.get("data_fingerprint")
                    if isinstance(data_fingerprint, str) and data_fingerprint:
                        fingerprint_holder[0] = data_fingerprint
                return metrics

            batch_evaluator = evaluate_batch

        search_result = HybridSearchEngine().search(
            request,
            candidate_builder=candidate_builder,
            evaluator=evaluate,
            batch_evaluator=batch_evaluator,
            fidelity_evaluator=evaluate_fidelity if request.multifidelity_enabled else None,
            checkpoint_retention_callback=(
                retain_candidate_checkpoints if request.multifidelity_enabled else None
            ),
            batch_size=self._evaluation_workers if batch_evaluator is not None else 1,
            evaluation_workers=(self._evaluation_workers if batch_evaluator is not None else 1),
            evaluation_mode="process_pool" if batch_evaluator is not None else "serial",
            max_trials=request.max_trials,
            seed=request.seed,
            validation_limit=request.validation_candidates,
            holdout_limit=request.holdout_candidates,
            convergence_enabled=request.convergence_enabled,
            progress_callback=report_progress,
            abort_check=abort_check,
        )
        # High-fidelity checkpoints are only needed while search promotes
        # candidates. Validation and holdout evaluate from their own date ranges.
        fidelity_checkpoints.clear()
        if abort_check is not None and abort_check():
            raise TaskCancelledError("策略实验已取消")
        if not search_result.results_by_sharpe and not search_result.results_by_return:
            if (
                search_result.search_metadata.get("stop_reason") == "timeout"
                and search_result.partial_by_sharpe
            ):
                partial_by_sharpe = _partial_candidates(search_result.partial_by_sharpe)
                partial_by_return = _partial_candidates(search_result.partial_by_return)
                partial_search_metadata = dict(search_result.search_metadata)
                partial_stage_durations = partial_search_metadata.get("stage_durations_seconds")
                stage_durations = (
                    dict(partial_stage_durations)
                    if isinstance(partial_stage_durations, Mapping)
                    else {}
                )
                stage_durations["snapshot"] = max(0.0, snapshot_build_seconds)
                stage_durations["indicators"] = _number(
                    (snapshot_metadata or {}).get("indicator_compute_seconds")
                )
                partial_algorithm = {
                    "algorithm": partial_search_metadata.get("algorithm"),
                    "algorithm_version": partial_search_metadata.get("algorithm_version", "1"),
                    "fallback_enabled": partial_search_metadata.get("fallback_enabled", False),
                    "surrogate_enabled": partial_search_metadata.get("surrogate_enabled", False),
                    "surrogate_fit_count": partial_search_metadata.get("surrogate_fit_count", 0),
                    "data_snapshot": "shared_memmap" if evaluation_executor is not None else "none",
                    "snapshot_build_seconds": snapshot_build_seconds,
                    "snapshot_metrics": dict(snapshot_metadata or {}),
                    "checkpoint_metrics": dict(checkpoint_metrics),
                    "seed": request.seed,
                    "optuna_version": _package_version("optuna"),
                    "lightgbm_version": _package_version("lightgbm"),
                }
                return {
                    "request": request.to_dict(),
                    "results_by_sharpe": [],
                    "results_by_return": [],
                    "partial": True,
                    "status": "partial",
                    "message": (
                        "搜索在完整精度复核完成前超时；以下候选仅供参考，尚未通过完整约束，"
                        "不会进入正式榜单或自动候选池。"
                    ),
                    "partial_candidate_count": len(search_result.partial_by_sharpe),
                    "partial_by_sharpe": partial_by_sharpe,
                    "partial_by_return": partial_by_return,
                    "search_metadata": partial_search_metadata,
                    "algorithm_metadata": partial_algorithm,
                    "stage_durations_seconds": stage_durations,
                    "data_fingerprint": fingerprint_holder[0],
                    "duration_seconds": max(0.0, time.monotonic() - started),
                    "validation_metadata": {
                        "status": "not_run",
                        "reason": "搜索候选尚未完成完整精度复核",
                    },
                }
            raise ValueError("没有找到满足约束的策略候选")

        def report_validation(current: int, total: int, stage: str) -> None:
            if progress_callback is not None:
                metadata = _progress_metadata(
                    current,
                    total,
                    stage,
                    progress_plan,
                    started,
                )
                metadata.update(
                    {
                        "best_sharpe": best_metrics["sharpe"],
                        "best_return": best_metrics["annual_return"],
                    }
                )
                progress_callback(
                    current,
                    total,
                    stage,
                    metadata,
                )

        discovery = run_validation_pipeline(
            search_result,
            request,
            evaluate,
            progress_callback=report_validation,
            abort_check=abort_check,
            batch_evaluator=batch_evaluator,
            batch_size=self._evaluation_workers if batch_evaluator is not None else 1,
        )
        result = discovery.to_dict()
        if search_result.search_metadata.get("stop_reason") == "timeout":
            result.update(
                partial=True,
                status="partial",
                message=(
                    "搜索超时；正式榜单仅包含通过完整精度约束的候选，另有候选尚未完成完整精度复核。"
                ),
                partial_candidate_count=len(search_result.partial_by_sharpe),
                partial_by_sharpe=_partial_candidates(search_result.partial_by_sharpe),
                partial_by_return=_partial_candidates(search_result.partial_by_return),
            )
        result["data_fingerprint"] = fingerprint_holder[0]
        search_metadata = result.get("search_metadata")
        validation_metadata = result.get("validation_metadata")
        snapshot_metadata_map = snapshot_metadata if isinstance(snapshot_metadata, Mapping) else {}
        search_metadata_map = search_metadata if isinstance(search_metadata, Mapping) else {}
        stage_durations = {
            "snapshot": max(0.0, snapshot_build_seconds),
            "indicators": _number(snapshot_metadata_map.get("indicator_compute_seconds")),
            "search": _stage_duration(search_metadata, "search"),
            "coarse": _number(search_metadata_map.get("coarse_seconds")),
            "medium": _stage_duration(search_metadata, "medium"),
            "full": _stage_duration(search_metadata, "full"),
            "validation": _stage_duration(validation_metadata, "validation"),
            "holdout": _stage_duration(validation_metadata, "holdout"),
            "candidate_generation": _number(
                search_metadata_map.get("candidate_generation_seconds")
            ),
            "surrogate": _number(search_metadata_map.get("surrogate_seconds")),
        }
        result["stage_durations_seconds"] = stage_durations
        result["algorithm_metadata"] = {
            "algorithm": search_result.search_metadata.get("algorithm"),
            "algorithm_version": search_result.search_metadata.get("algorithm_version", "1"),
            "fallback_enabled": search_result.search_metadata.get("fallback_enabled", False),
            "surrogate_enabled": search_result.search_metadata.get("surrogate_enabled", False),
            "surrogate_fit_count": search_result.search_metadata.get("surrogate_fit_count", 0),
            "data_snapshot": ("shared_memmap" if evaluation_executor is not None else "none"),
            "snapshot_build_seconds": snapshot_build_seconds,
            "snapshot_metrics": dict(snapshot_metadata or {}),
            "stage_durations_seconds": stage_durations,
            "checkpoint_metrics": dict(checkpoint_metrics),
            "seed": request.seed,
            "optuna_version": _package_version("optuna"),
            "lightgbm_version": _package_version("lightgbm"),
        }
        if result_metadata:
            result.update(dict(result_metadata))
        result["duration_seconds"] = max(0.0, time.monotonic() - started)
        return result

    def _evaluate_candidate(
        self,
        candidate: StrategyCandidate,
        request: DiscoveryRequest,
        split: str,
        *,
        fidelity: str | None = None,
        checkpoint: Mapping[str, object] | SimulationCheckpoint | None = None,
        data_fingerprint_holder: list[str] | None = None,
    ) -> Mapping[str, object]:
        split_config = request.splits.get(split)
        if not split_config:
            return {"error": f"缺少 {split} 时间区间", "annual_return": float("nan")}
        start_text = str(split_config.get("start_date") or "")
        end_text = str(split_config.get("end_date") or "")
        if split == "search" and fidelity in {"coarse", "medium"}:
            end_text = _fidelity_end(start_text, end_text, fidelity)
        payload = candidate.to_config()
        payload["start_date"] = start_text
        payload["end_date"] = end_text
        checkpoint_value: SimulationCheckpoint | None = None
        checkpoint_resume_attempted = checkpoint is not None
        checkpoint_fallback = False
        try:
            if isinstance(checkpoint, Mapping):
                checkpoint_value = SimulationCheckpoint.from_dict(checkpoint)
            elif isinstance(checkpoint, SimulationCheckpoint):
                checkpoint_value = checkpoint
        except Exception:
            checkpoint_fallback = checkpoint_resume_attempted
        captured_checkpoint: SimulationCheckpoint | None = None
        try:
            config = PortfolioBacktestRequest.model_validate(payload).to_config()
            report: PortfolioBacktestReport | dict[str, object]
            if self._discovery_snapshot is not None and isinstance(
                self._portfolio_service, PortfolioBacktestService
            ):
                reuse_candidate_contexts = request.candidate_mode == "risk_only"
                contexts = self._discovery_snapshot.contexts_for_config(
                    config,
                    reuse_candidate_contexts=reuse_candidate_contexts,
                )

                def capture_checkpoint(value: SimulationCheckpoint) -> None:
                    nonlocal captured_checkpoint
                    captured_checkpoint = value

                try:
                    report = self._portfolio_service.run_prepared_metrics(
                        config,
                        contexts,
                        total_candidates=self._discovery_snapshot.total_candidates,
                        checkpoint=checkpoint_value,
                        precompute_ranked_candidates=reuse_candidate_contexts,
                        checkpoint_callback=(
                            capture_checkpoint if fidelity in {"coarse", "medium", "full"} else None
                        ),
                    )
                except Exception:
                    if checkpoint_value is None:
                        raise
                    checkpoint_fallback = True
                    captured_checkpoint = None
                    report = self._portfolio_service.run_prepared_metrics(
                        config,
                        contexts,
                        total_candidates=self._discovery_snapshot.total_candidates,
                        checkpoint=None,
                        precompute_ranked_candidates=reuse_candidate_contexts,
                        checkpoint_callback=(
                            capture_checkpoint if fidelity in {"coarse", "medium", "full"} else None
                        ),
                    )
            else:
                report = self._portfolio_service.run(config)
        except Exception as exc:  # noqa: BLE001 - isolate one candidate
            return {
                "error": str(exc),
                "annual_return": float("nan"),
                "total_return": float("nan"),
                "sharpe": float("nan"),
                "total_trades": 0,
            }
        if isinstance(report, dict):
            performance_value = report.get("performance")
            performance: Mapping[str, object] = (
                performance_value if isinstance(performance_value, Mapping) else report
            )
            data_fingerprint = str(report.get("data_fingerprint") or "")
        elif isinstance(report, PortfolioBacktestReport):
            performance = report.performance
            data_fingerprint = report.data_fingerprint
        else:
            return {"error": "组合回测返回结果无效", "annual_return": float("nan")}
        if data_fingerprint and data_fingerprint_holder is not None:
            data_fingerprint_holder[0] = data_fingerprint
        metrics: dict[str, object] = {
            key: performance.get(key)
            for key in (
                "total_return",
                "annual_return",
                "sharpe",
                "sortino",
                "calmar",
                "max_drawdown",
                "max_dd_duration",
                "total_trades",
                "win_rate",
                "win_trades",
                "lose_trades",
                "rejected_trades",
                "payoff_ratio",
                "profit_factor",
                "avg_win",
                "avg_loss",
                "max_win",
                "max_loss",
                "avg_holding_days",
                "volatility",
                "executed_buy_orders",
                "executed_sell_orders",
                "rejected_buy_orders",
                "rejected_sell_orders",
                "buy_turnover",
                "sell_turnover",
                "buy_transaction_costs",
                "sell_transaction_costs",
            )
        }
        metrics["turnover"] = (
            _turnover(report, config.initial_cash)
            if isinstance(report, PortfolioBacktestReport)
            else _number(report.get("turnover"))
        )
        trades = (
            report.trades if isinstance(report, PortfolioBacktestReport) else report.get("trades")
        )
        diagnostic = (
            report.diagnostic
            if isinstance(report, PortfolioBacktestReport)
            else report.get("diagnostic")
        )
        metrics["trade_fingerprint"] = _trade_fingerprint(trades)
        metrics["preflight_skipped"] = diagnostic == "预检无可交易候选"
        if captured_checkpoint is not None:
            metrics["_simulation_checkpoint"] = captured_checkpoint.to_dict()
        if checkpoint_resume_attempted:
            metrics["_checkpoint_resume_attempted"] = True
        if checkpoint_fallback:
            metrics["_checkpoint_fallback"] = True
        metrics["data_fingerprint"] = data_fingerprint
        return metrics


def _string_tuple(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, (list, tuple)):
        return None
    result = tuple(str(item).strip() for item in value if str(item).strip())
    return result or None


def _expose_training_metrics(result: dict[str, object]) -> None:
    """Publish each risk candidate's highest-fidelity search metrics as train."""

    for board_key in ("results_by_sharpe", "results_by_return"):
        raw_candidates = result.get(board_key)
        if not isinstance(raw_candidates, list):
            continue
        for candidate in raw_candidates:
            if not isinstance(candidate, dict):
                continue
            raw_metrics = candidate.get("metrics")
            if not isinstance(raw_metrics, dict):
                continue
            training = next(
                (
                    raw_metrics[key]
                    for key in ("full", "medium", "search")
                    if isinstance(raw_metrics.get(key), Mapping)
                ),
                None,
            )
            if isinstance(training, Mapping):
                raw_metrics["train"] = dict(training)


def _progress_plan(request: DiscoveryRequest) -> tuple[tuple[str, ...], dict[str, int], int]:
    units = {
        "search": max(1, request.max_trials),
        # Validation is evaluated independently for the Sharpe and return
        # shortlists.  The union can therefore be up to twice the per-board
        # budget when the two leaderboards contain different candidates.
        "validation": max(1, request.validation_candidates * 2),
        "holdout": max(1, request.holdout_candidates),
    }
    stages = ["search", "validation", "holdout"]
    if request.multifidelity_enabled:
        units["refining_medium"] = max(1, math.ceil(request.max_trials * 0.35))
        units["refining_full"] = max(1, math.ceil(request.max_trials * 0.15))
        if request.research_search_policy == "adaptive-v1":
            units["refining_medium"] = min(units["refining_medium"], MEDIUM_LIMIT)
            units["refining_full"] = min(units["refining_full"], FULL_LIMIT)
        stages = ["search", "refining_medium", "refining_full", "validation", "holdout"]
    return tuple(stages), units, sum(units.values())


def _progress_metadata(
    current: int,
    total: int,
    stage: str,
    plan: tuple[tuple[str, ...], dict[str, int], int],
    started: float,
) -> dict[str, object]:
    stages, units, estimated_total = plan
    stage_key = {
        "searching": "search",
        "refining_medium": "refining_medium",
        "refining_full": "refining_full",
    }.get(stage, stage)
    if stage_key == "completed":
        overall = 1.0
        offset = estimated_total
        stage_units = 0
    else:
        if stage_key not in units:
            stage_key = "search"
        stage_index = stages.index(stage_key)
        offset = sum(units[name] for name in stages[:stage_index])
        stage_units = units[stage_key]
        phase_ratio = current / total if total > 0 else 0.0
        phase_ratio = min(1.0, max(0.0, phase_ratio))
        overall = (offset + phase_ratio * stage_units) / estimated_total
    elapsed = max(0.0, time.monotonic() - started)
    completed_units = overall * estimated_total
    throughput = completed_units / elapsed if elapsed > 0 and completed_units > 0 else 0.0
    remaining = max(0.0, estimated_total - completed_units) / throughput if throughput > 0 else None
    phase_labels = {
        "search": "寻优候选",
        "refining_medium": "中等精度筛选",
        "refining_full": "完整精度筛选",
        "validation": "验证区间",
        "holdout": "最终留出",
    }
    return {
        "overall_progress": min(1.0, max(0.0, overall)),
        "phase_current": current,
        "phase_total": total,
        "phase_label": phase_labels.get(stage_key, stage),
        "estimated_total_evaluations": estimated_total,
        "completed_work_units": completed_units,
        "elapsed_seconds": elapsed,
        "throughput_per_second": throughput,
        "estimated_seconds_remaining": remaining,
    }


def _partial_candidates(
    candidates: Sequence[StrategyCandidate],
) -> list[dict[str, object]]:
    """Serialize timeout previews with their achieved fidelity and no qualification."""

    result: list[dict[str, object]] = []
    for candidate in candidates:
        metrics = candidate.metrics
        achieved = next(
            (stage for stage in ("full", "medium", "coarse", "search") if stage in metrics),
            "search",
        )
        result.append(
            {
                **candidate.to_dict(),
                "achieved_fidelity": achieved,
                "qualified": False,
            }
        )
    return result


def _fidelity_end(start_text: str, end_text: str, fidelity: str) -> str:
    try:
        start = date.fromisoformat(start_text)
        end = date.fromisoformat(end_text)
    except ValueError:
        return end_text
    fraction = {"coarse": 0.25, "medium": 0.5}.get(fidelity, 1.0)
    total_days = max(1, (end - start).days + 1)
    return (start + timedelta(days=max(1, int(total_days * fraction)) - 1)).isoformat()


def _turnover(report: PortfolioBacktestReport, initial_cash: float) -> float:
    if initial_cash <= 0:
        return 0.0
    total_notional = sum(
        _number(trade.get("size")) * _number(trade.get("price")) for trade in report.trades
    )
    return total_notional / initial_cash


def _number(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _stage_duration(metadata: object, stage: str) -> float:
    if not isinstance(metadata, Mapping):
        return 0.0
    durations = metadata.get("stage_durations_seconds")
    if not isinstance(durations, Mapping):
        return 0.0
    return max(0.0, _number(durations.get(stage)))


def _trade_fingerprint(trades: object) -> str:
    if not isinstance(trades, (list, tuple)):
        return ""
    fields = (
        "board",
        "code",
        "commission",
        "cooldown_days",
        "cost_basis",
        "date",
        "direction",
        "instrument_type",
        "market",
        "pnl",
        "price",
        "reason",
        "risk_trigger",
        "signal_date",
        "size",
        "slippage",
        "stamp_tax",
    )
    rows: list[dict[str, object]] = []
    for trade in trades:
        if not isinstance(trade, Mapping):
            continue
        rows.append({field: _fingerprint_value(trade.get(field)) for field in fields})
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _fingerprint_value(value: object) -> object:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return format(value, ".15g")
    return str(value)


def _find_parent_candidate(
    experiment: Mapping[str, object], candidate_hash: str
) -> Mapping[str, object]:
    for key in ("results_by_sharpe", "results_by_return"):
        candidates = experiment.get(key)
        if not isinstance(candidates, (list, tuple)):
            continue
        for candidate in candidates:
            if (
                isinstance(candidate, Mapping)
                and str(candidate.get("candidate_hash")) == candidate_hash
            ):
                return candidate
    raise ValueError("父实验中不存在指定候选")


def _validate_expected_fingerprint(metrics: Mapping[str, object], expected: str | None) -> None:
    if not expected:
        return
    actual = metrics.get("data_fingerprint")
    if isinstance(actual, str) and actual and actual != expected:
        raise ValueError("本地行情已变化，无法基于旧候选执行二次优化，请重新运行策略实验")


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _fallback_enabled() -> bool:
    value = os.getenv("AQUEST_DISCOVERY_ALLOW_FALLBACK", "0")
    return value.strip().lower() in {"1", "true", "yes", "on"}
