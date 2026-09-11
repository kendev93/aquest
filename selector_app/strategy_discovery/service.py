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
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Protocol, cast

from selector_app.indicators.cache import IndicatorCache
from selector_app.market_data.adapter import DuckDbMarketDataAdapter
from selector_app.market_data.models import InstrumentBoard, InstrumentType
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.portfolio_backtest.models import PortfolioBacktestConfig, PortfolioBacktestReport
from selector_app.portfolio_backtest.service import PortfolioBacktestService
from selector_app.web.schemas import PortfolioBacktestRequest

from .candidates import build_random_candidate
from .catalog import default_indicator_catalog
from .evaluation import run_validation_pipeline
from .models import DiscoveryRequest, StrategyCandidate
from .replay import ReplayLeaderboard
from .replay_service import ReplayProgressCallback, StrategyReplayService
from .search import BatchEvaluator, HybridSearchEngine
from .snapshot import DiscoverySnapshot, build_discovery_snapshot

ProgressCallback = Callable[[int, int, str, Mapping[str, object]], None]
DISCOVERY_EVALUATION_WORKERS = 8

_WORKER_DISCOVERY_SERVICE: StrategyDiscoveryService | None = None


def _init_discovery_worker(database_path: str, snapshot_path: str | None = None) -> None:
    global _WORKER_DISCOVERY_SERVICE
    worker_store = DuckDbMarketDataStore(database_path, read_only=True)
    worker_portfolio = PortfolioBacktestService(
        adapter=DuckDbMarketDataAdapter(worker_store),
        indicator_cache=IndicatorCache(),
    )
    _WORKER_DISCOVERY_SERVICE = StrategyDiscoveryService(
        portfolio_service=worker_portfolio,
        store=worker_store,
        discovery_snapshot=(DiscoverySnapshot(snapshot_path) if snapshot_path else None),
    )


def _evaluate_candidate_process(
    job: tuple[StrategyCandidate, DiscoveryRequest, str, str | None, str],
) -> Mapping[str, object]:
    candidate, request, stage, fidelity, database_path = job
    if _WORKER_DISCOVERY_SERVICE is None:
        _init_discovery_worker(database_path)
    assert _WORKER_DISCOVERY_SERVICE is not None
    split = "search" if stage in {"coarse", "medium", "full"} else stage
    return _WORKER_DISCOVERY_SERVICE._evaluate_candidate(
        candidate,
        request,
        split,
        fidelity=fidelity,
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
        discovery_snapshot: DiscoverySnapshot | None = None,
    ) -> None:
        selected_store = store or DuckDbMarketDataStore()
        self._portfolio_service = portfolio_service or PortfolioBacktestService(
            adapter=DuckDbMarketDataAdapter(selected_store),
            indicator_cache=IndicatorCache(selected_store),
        )
        self._store = selected_store
        self._discovery_snapshot = discovery_snapshot
        self._parallel_enabled = isinstance(
            self._portfolio_service, PortfolioBacktestService
        ) and isinstance(
            getattr(self._portfolio_service, "_adapter", None), DuckDbMarketDataAdapter
        )
        self._replay_service = StrategyReplayService(
            portfolio_service=self._portfolio_service,
            store=self._store,
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
            "evaluation_workers": (DISCOVERY_EVALUATION_WORKERS if self._parallel_enabled else 1),
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
    ) -> dict[str, object]:
        started = time.monotonic()
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
                progress_callback=report_snapshot_progress,
                abort_check=abort_check,
            ) as snapshot:
                snapshot_build_seconds = max(0.0, time.monotonic() - snapshot_started)
                if progress_callback is not None:
                    progress_callback(
                        1,
                        1,
                        "snapshot_ready",
                        {
                            "overall_progress": 0.0,
                            "phase_current": 1,
                            "phase_total": 1,
                            "phase_label": "快照已准备，开始寻优",
                            "estimated_total_evaluations": _progress_plan(request)[2],
                            "completed_work_units": 0.0,
                            "elapsed_seconds": max(0.0, time.monotonic() - started),
                            "throughput_per_second": 0.0,
                            "estimated_seconds_remaining": None,
                            "snapshot_build_seconds": snapshot_build_seconds,
                        },
                    )
                with self._evaluation_executor(str(snapshot.root)) as evaluation_executor:
                    return self._run_impl(
                        request,
                        progress_callback=progress_callback,
                        abort_check=abort_check,
                        evaluation_executor=evaluation_executor,
                        started=started,
                        snapshot_build_seconds=snapshot_build_seconds,
                    )
        with self._evaluation_executor() as evaluation_executor:
            return self._run_impl(
                request,
                progress_callback=progress_callback,
                abort_check=abort_check,
                evaluation_executor=evaluation_executor,
                started=started,
            )

    @contextmanager
    def _evaluation_executor(
        self, snapshot_path: str | None = None
    ) -> Iterator[ProcessPoolExecutor | None]:
        if not self._parallel_enabled:
            yield None
            return
        database_path = str(self._store.database_path)
        executor = ProcessPoolExecutor(
            max_workers=DISCOVERY_EVALUATION_WORKERS,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=_init_discovery_worker,
            initargs=(database_path, snapshot_path),
        )
        try:
            yield executor
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

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
        refs = DuckDbMarketDataAdapter(self._store).list_stock_refs(
            str(base.get("vipdoc_path") or ""),
            universe,
            universe_file,
            instrument_types=instrument_types,
            boards=boards,
        )
        if not refs:
            raise ValueError("本地 DuckDB 没有可用行情，请先在“本地行情”页面导入日线数据")

    def _run_impl(
        self,
        request: DiscoveryRequest,
        *,
        progress_callback: ProgressCallback | None = None,
        abort_check: Callable[[], bool] | None = None,
        evaluation_executor: ProcessPoolExecutor | None = None,
        started: float | None = None,
        snapshot_build_seconds: float = 0.0,
    ) -> dict[str, object]:
        started = time.monotonic() if started is None else started
        fingerprint_holder = [""]
        best_metrics: dict[str, float | None] = {
            "sharpe": None,
            "annual_return": None,
        }
        progress_plan = _progress_plan(request)

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
            observe_metrics(metrics, split)
            return metrics

        def evaluate_fidelity(candidate: StrategyCandidate, fidelity: str) -> Mapping[str, object]:
            metrics = self._evaluate_candidate(
                candidate,
                request,
                "search",
                fidelity=fidelity,
                data_fingerprint_holder=fingerprint_holder,
            )
            observe_metrics(metrics, "search")
            return metrics

        batch_evaluator: BatchEvaluator | None = None
        if evaluation_executor is not None:
            database_path = str(self._store.database_path)
            executor = evaluation_executor

            def evaluate_batch(
                candidates: Sequence[StrategyCandidate], stage: str
            ) -> Sequence[Mapping[str, object]]:
                fidelity = stage if stage in {"coarse", "medium", "full"} else None
                jobs = [
                    (candidate, request, stage, fidelity, database_path) for candidate in candidates
                ]
                try:
                    metrics = list(executor.map(_evaluate_candidate_process, jobs, chunksize=1))
                except (BrokenProcessPool, OSError) as exc:
                    raise RuntimeError(
                        "策略实验的 8 个评估进程启动失败，请检查 Docker 运行时和行情数据库权限"
                    ) from exc
                observed_stage = "search" if fidelity is not None else stage
                for item in metrics:
                    observe_metrics(item, observed_stage)
                    data_fingerprint = item.get("data_fingerprint")
                    if isinstance(data_fingerprint, str) and data_fingerprint:
                        fingerprint_holder[0] = data_fingerprint
                return metrics

            batch_evaluator = evaluate_batch

        search_result = HybridSearchEngine().search(
            request,
            candidate_builder=build_random_candidate,
            evaluator=evaluate,
            batch_evaluator=batch_evaluator,
            fidelity_evaluator=evaluate_fidelity if request.multifidelity_enabled else None,
            batch_size=DISCOVERY_EVALUATION_WORKERS if batch_evaluator is not None else 1,
            evaluation_workers=(DISCOVERY_EVALUATION_WORKERS if batch_evaluator is not None else 1),
            evaluation_mode="process_pool" if batch_evaluator is not None else "serial",
            max_trials=request.max_trials,
            seed=request.seed,
            validation_limit=request.validation_candidates,
            holdout_limit=request.holdout_candidates,
            progress_callback=report_progress,
            abort_check=abort_check,
        )
        if not search_result.results_by_sharpe and not search_result.results_by_return:
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
            batch_size=DISCOVERY_EVALUATION_WORKERS if batch_evaluator is not None else 1,
        )
        result = discovery.to_dict()
        result["data_fingerprint"] = fingerprint_holder[0]
        result["algorithm_metadata"] = {
            "algorithm": search_result.search_metadata.get("algorithm"),
            "algorithm_version": search_result.search_metadata.get("algorithm_version", "1"),
            "fallback_enabled": search_result.search_metadata.get("fallback_enabled", False),
            "surrogate_enabled": search_result.search_metadata.get("surrogate_enabled", False),
            "surrogate_fit_count": search_result.search_metadata.get("surrogate_fit_count", 0),
            "data_snapshot": ("shared_memmap" if evaluation_executor is not None else "none"),
            "snapshot_build_seconds": snapshot_build_seconds,
            "seed": request.seed,
            "optuna_version": _package_version("optuna"),
            "lightgbm_version": _package_version("lightgbm"),
        }
        result["duration_seconds"] = max(0.0, time.monotonic() - started)
        return result

    def _evaluate_candidate(
        self,
        candidate: StrategyCandidate,
        request: DiscoveryRequest,
        split: str,
        *,
        fidelity: str | None = None,
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
        try:
            config = PortfolioBacktestRequest.model_validate(payload).to_config()
            report: PortfolioBacktestReport | dict[str, object]
            if self._discovery_snapshot is not None and isinstance(
                self._portfolio_service, PortfolioBacktestService
            ):
                report = self._portfolio_service.run_prepared_contexts(
                    config,
                    self._discovery_snapshot.contexts_for_config(config),
                    total_candidates=self._discovery_snapshot.total_candidates,
                    include_details=False,
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
                "max_drawdown",
                "total_trades",
                "win_rate",
                "volatility",
            )
        }
        metrics["turnover"] = (
            _turnover(report, config.initial_cash)
            if isinstance(report, PortfolioBacktestReport)
            else 0.0
        )
        if isinstance(report, PortfolioBacktestReport):
            metrics["trade_fingerprint"] = _trade_fingerprint(report.trades)
        metrics["data_fingerprint"] = data_fingerprint
        return metrics


def _string_tuple(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, (list, tuple)):
        return None
    result = tuple(str(item).strip() for item in value if str(item).strip())
    return result or None


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


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _fallback_enabled() -> bool:
    value = os.getenv("INDICATOR_LAB_DISCOVERY_ALLOW_FALLBACK", "0")
    return value.strip().lower() in {"1", "true", "yes", "on"}
