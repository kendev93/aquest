"""Incremental full-universe research using the existing individual-stock pipeline."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import nullcontext
from datetime import date
from threading import Lock, local
from typing import Literal, cast

import numpy as np
import pandas as pd

from selector_app.backtest.models import BacktestConfig
from selector_app.backtest.service import BacktestService
from selector_app.concurrency import visible_cpu_count
from selector_app.formulas.registry import FORMULA_REGISTRY
from selector_app.indicators.cache import frame_fingerprint
from selector_app.individual_research.models import IndividualResearchConfig
from selector_app.individual_research.search_policy import CANDIDATE_LIMIT
from selector_app.individual_research.service import (
    IndividualStockResearchService,
    InsufficientResearchHistoryError,
)
from selector_app.market_data.adapter import BatchMarketDataAdapter
from selector_app.market_data.adjustment import CorporateAction, mark_adjusted
from selector_app.market_data.models import StockRef
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.market_data.store_base import RESEARCH_START
from selector_app.portfolio_backtest.benchmark import curve_performance
from selector_app.portfolio_backtest.models import PortfolioBacktestConfig
from selector_app.portfolio_backtest.service import PortfolioBacktestService
from selector_app.screening.engine import evaluate_rule_group
from selector_app.screening.jobs import TaskCancelledError
from selector_app.screening.rules import RuleGroup
from selector_app.strategy_discovery.candidates import DEFAULT_MAX_HOLD_DAYS
from selector_app.strategy_discovery.catalog import default_indicator_catalog
from selector_app.strategy_discovery.evaluation_budget import EvaluationWorkerBudget
from selector_app.web.schemas.screening import RuleGroupRequest

from .resources import (
    EVALUATION_WORKER_MEMORY_BYTES,
    POOL_RAMP_INTERVAL_SECONDS,
    PoolConcurrencyRamp,
    cpu_bounded_worker_count,
    estimate_ticker_working_set_bytes,
    get_cpu_load_average_1m,
    memory_bounded_worker_count,
    memory_status,
    pool_evaluation_budget_capacity,
    pool_evaluation_worker_count,
    pool_worker_cap,
)
from .selection import common_calendar_windows, summarize_objective_winners
from .store import DuckDbStrategyPoolStore

PoolObjective = Literal["annual_return", "sharpe"]
PoolProgressCallback = Callable[[int, int, str, Mapping[str, object] | None], None]
PoolEntryWriter = Callable[[Mapping[str, object]], None]
# v6：选优加了两道硬门槛（样本不足直接出局、平均持仓超上限出局，见
# strategy_pool.selection）；增量复用判定不再看行情指纹，改为按"研究是否有
# 结论"跳过（ready/partial/no_strategy 复用，insufficient_history/failed 重查）；
# 研究窗口起点从 2015-01-05 裁到 RESEARCH_START（2016-01-01），2015 全年只作
# 指标预热。门槛、复用语义与研究窗口都会改变存量条目的有效性，v5 语义下的
# 条目在下一次全市场重选时逐股重研究。
MARKET_POOL_ALGORITHM_VERSION = "market-pool-v6"
POOL_PROGRESS_EMIT_INTERVAL_SECONDS = 1.5
STRATEGY_HOLDING_BATCH_SIZE = 100
STRATEGY_HOLDING_VIRTUAL_CASH = 1_000_000_000_000.0
logger = logging.getLogger(__name__)


class _TickerProgress:
    def __init__(
        self,
        *,
        index: int,
        total: int,
        symbol: str,
        name: str = "",
        callback: PoolProgressCallback | None,
        coordinator: _PoolProgressCoordinator | None = None,
    ) -> None:
        self._index = index
        self._total = total
        self._symbol = symbol
        self._name = name
        self._callback = callback
        self._coordinator = coordinator
        self._phase = "研究单只股票"
        self._phase_label = "准备开始"
        self._started_at: float | None = None
        self._work_details: dict[str, object] = {}

    def start(self) -> None:
        """Start the ticker clock when its worker begins, rather than while queued."""

        if self._started_at is not None:
            return
        self._started_at = time.time()
        self(0, 100)

    def __call__(self, current: int, total: int) -> None:
        if self._started_at is None:
            self._started_at = time.time()
        fraction = min(1.0, max(0.0, current / total if total else 1.0))
        if self._coordinator is not None:
            self._coordinator.ticker_progress(
                self._index,
                self._symbol,
                fraction,
                self._phase,
                self._phase_label,
                name=self._name,
                started_at=self._started_at,
                work_details=self._work_details,
            )
            return
        overall = (self._index - 1 + fraction) / max(1, self._total)
        if self._callback is not None:
            self._callback(
                min(self._total, self._index - 1 + (1 if fraction >= 1 else 0)),
                self._total,
                self._phase,
                {
                    "overall_progress": overall,
                    "symbol": self._symbol,
                    "ticker_progress": fraction,
                    "ticker_started_at": self._started_at,
                    "ticker_name": self._name,
                    "phase_label": self._phase_label,
                    "work_details": self._work_details,
                },
            )

    def report_detail(self, details: Mapping[str, object]) -> None:
        work = details.get("work_details")
        self._work_details = dict(work) if isinstance(work, Mapping) else {}
        phase = details.get("phase")
        label = details.get("phase_label")
        if isinstance(phase, str):
            self._phase = phase
        if isinstance(label, str):
            self._phase_label = label


class _PoolProgressCoordinator:
    """Serialize worker progress and aggregate independent ticker fractions."""

    def __init__(
        self,
        *,
        total_symbols: int,
        total_work: int,
        callback: PoolProgressCallback | None,
        maximum_workers: int,
    ) -> None:
        self._total_symbols = total_symbols
        self._total_work = total_work
        self._callback = callback
        self._maximum_workers = maximum_workers
        self._memory_worker_limit = maximum_workers
        self._available_memory_mb: int | None = None
        self._cpu_worker_limit = maximum_workers
        self._cpu_load_average_1m: float | None = None
        self._visible_cpus = maximum_workers
        self._target_workers = 1
        self._estimated_worker_bytes = 0
        self._completed = 0
        self._finished = 0
        self._skipped = 0
        self._failed = 0
        self._fractions: dict[int, float] = {}
        self._active_symbols: dict[int, dict[str, object]] = {}
        self._last_emit_at = 0.0
        self._lock = Lock()

    def set_resource_budget(
        self,
        *,
        memory_worker_limit: int,
        available_bytes: int | None,
        cpu_worker_limit: int,
        cpu_load_average_1m: float | None,
        visible_cpus: int,
        target_workers: int = 1,
        estimated_worker_bytes: int = 0,
    ) -> None:
        with self._lock:
            self._memory_worker_limit = memory_worker_limit
            self._available_memory_mb = (
                max(0, available_bytes // (1024**2)) if available_bytes is not None else None
            )
            self._cpu_worker_limit = cpu_worker_limit
            self._cpu_load_average_1m = cpu_load_average_1m
            self._visible_cpus = visible_cpus
            self._target_workers = target_workers
            self._estimated_worker_bytes = estimated_worker_bytes

    def ticker_progress(
        self,
        index: int,
        symbol: str,
        fraction: float,
        phase: str,
        phase_label: str,
        *,
        name: str = "",
        started_at: float | None = None,
        work_details: Mapping[str, object] | None = None,
    ) -> None:
        with self._lock:
            if started_at is None:
                previous = self._active_symbols.get(index, {})
                started_at = cast(float, previous.get("started_at") or time.time())
            self._fractions[index] = fraction
            self._active_symbols[index] = {
                "symbol": symbol,
                "name": name,
                "started_at": started_at,
                "progress": fraction,
                "phase": phase,
                "phase_label": phase_label,
                "work_details": dict(work_details or {}),
            }
            now = time.monotonic()
            if now - self._last_emit_at < POOL_PROGRESS_EMIT_INTERVAL_SECONDS:
                return
            fractional_work = min(
                self._total_symbols,
                self._finished + sum(self._fractions.values()),
            )
            self._emit(
                int(fractional_work),
                phase,
                {
                    "overall_progress": fractional_work / max(1, self._total_work),
                    "current_symbol": symbol,
                    "phase_label": phase_label,
                    "completed_symbols": self._completed,
                    "failed_symbols": self._failed,
                    "skipped_symbols": self._skipped,
                    "finished_symbols": self._finished,
                    "total_symbols": self._total_symbols,
                    "current_parallelism": len(self._fractions),
                    "active_symbols": [
                        dict(progress) for _, progress in sorted(self._active_symbols.items())
                    ],
                    "maximum_parallelism": self._maximum_workers,
                    "memory_worker_limit": self._memory_worker_limit,
                    "available_memory_mb": self._available_memory_mb,
                    "cpu_worker_limit": self._cpu_worker_limit,
                    "cpu_load_average_1m": self._cpu_load_average_1m,
                    "visible_cpus": self._visible_cpus,
                    "target_parallelism": self._target_workers,
                    "estimated_ticker_memory_mb": self._estimated_worker_bytes // (1024**2),
                    "ramp_interval_seconds": POOL_RAMP_INTERVAL_SECONDS,
                },
            )
            self._last_emit_at = now

    def finish(
        self,
        index: int,
        symbol: str,
        phase: str,
        *,
        skipped: bool = False,
        failed: bool = False,
    ) -> None:
        with self._lock:
            self._fractions.pop(index, None)
            self._active_symbols.pop(index, None)
            self._finished += 1
            if failed:
                self._failed += 1
            else:
                self._completed += 1
                if skipped:
                    self._skipped += 1
            self._emit(
                self._finished,
                phase,
                {
                    "overall_progress": self._finished / max(1, self._total_work),
                    "current_symbol": symbol,
                    "completed_symbols": self._completed,
                    "total_symbols": self._total_symbols,
                    "skipped_symbols": self._skipped,
                    "failed_symbols": self._failed,
                    "finished_symbols": self._finished,
                    "current_parallelism": len(self._fractions),
                    "active_symbols": [
                        dict(progress) for _, progress in sorted(self._active_symbols.items())
                    ],
                    "maximum_parallelism": self._maximum_workers,
                    "memory_worker_limit": self._memory_worker_limit,
                    "available_memory_mb": self._available_memory_mb,
                    "cpu_worker_limit": self._cpu_worker_limit,
                    "cpu_load_average_1m": self._cpu_load_average_1m,
                    "visible_cpus": self._visible_cpus,
                    "target_parallelism": self._target_workers,
                    "estimated_ticker_memory_mb": self._estimated_worker_bytes // (1024**2),
                    "ramp_interval_seconds": POOL_RAMP_INTERVAL_SECONDS,
                },
            )
            self._last_emit_at = time.monotonic()

    def _emit(self, current: int, phase: str, metadata: Mapping[str, object]) -> None:
        if self._callback is not None:
            self._callback(current, self._total_work, phase, metadata)


class MarketStrategyPoolService:
    """Research local A shares with memory-bounded concurrency and checkpoints."""

    def __init__(
        self,
        *,
        store: DuckDbMarketDataStore,
        adapter: BatchMarketDataAdapter,
        individual_service: IndividualStockResearchService,
        backtest_service: BacktestService | None = None,
        pool_store: DuckDbStrategyPoolStore | None = None,
        portfolio_service: PortfolioBacktestService | None = None,
        individual_service_factory: Callable[[], IndividualStockResearchService] | None = None,
        max_pool_workers: int | None = None,
        evaluation_worker_budget: EvaluationWorkerBudget | None = None,
    ) -> None:
        self._store = store
        self._adapter = adapter
        self._individual_service = individual_service
        self._backtest_service = backtest_service
        self._pool_store = pool_store
        self._portfolio_service = portfolio_service
        self._individual_service_factory = individual_service_factory
        self._research_local = local()
        self._evaluation_worker_budget = evaluation_worker_budget
        requested_pool_workers = (
            1
            if individual_service_factory is None
            else pool_worker_cap()
            if max_pool_workers is None
            else max_pool_workers
        )
        self._max_pool_workers = requested_pool_workers
        if self._max_pool_workers < 1:
            raise ValueError("全市场策略池最大并发数必须大于 0")

    def _individual_service_for_thread(self) -> IndividualStockResearchService:
        if self._individual_service_factory is None:
            return self._individual_service
        service = getattr(self._research_local, "service", None)
        if service is None:
            service = self._individual_service_factory()
            self._research_local.service = service
        return cast(IndividualStockResearchService, service)

    def _research_symbol(
        self,
        ref: StockRef,
        config: IndividualResearchConfig,
        *,
        job_id: str,
        mode: Literal["full", "single"],
        config_fingerprint: str,
        previous_market_fingerprint: str,
        progress: _TickerProgress,
        abort_check: Callable[[], bool] | None,
    ) -> tuple[dict[str, object], dict[str, object] | None]:
        registration = (
            self._evaluation_worker_budget.stock()
            if self._evaluation_worker_budget is not None
            else nullcontext()
        )
        with registration:
            progress.start()
            clear_stock_cache = getattr(self._adapter, "clear_thread_stock_cache", None)
            if callable(clear_stock_cache):
                clear_stock_cache()
            individual_service = self._individual_service_for_thread()
            result = individual_service.run(config, progress, abort_check)
            result_market_fingerprint = result.get("market_fingerprint")
            if not isinstance(result_market_fingerprint, str) or not result_market_fingerprint:
                result_market_fingerprint = previous_market_fingerprint or _market_fingerprint(
                    self._adapter, ref
                )
            if (
                previous_market_fingerprint
                and result_market_fingerprint != previous_market_fingerprint
            ):
                raise ValueError("研究期间本地行情发生变化，请重新运行该股票")
            benchmark = _symbol_benchmark(
                self._adapter,
                ref,
                config,
                _mapping(result.get("split_metadata")),
            )
            winners = summarize_objective_winners(result, max_hold_days=config.max_hold_days)
            annual_candidate = winners["annual_return"]
            sharpe_candidate = winners["sharpe"]
            split_metadata = result.get("split_metadata")
            if isinstance(split_metadata, Mapping):
                for candidate in (annual_candidate, sharpe_candidate):
                    if isinstance(candidate, dict):
                        candidate["split_metadata"] = dict(split_metadata)
            if mode == "full" and _same_candidate(annual_candidate, sharpe_candidate):
                sharpe_candidate = {
                    "candidate_ref": "annual_return",
                    "candidate_id": annual_candidate.get("candidate_id")
                    if annual_candidate is not None
                    else None,
                }
            full_period_metrics = getattr(individual_service, "full_period_metrics", None)
            if callable(full_period_metrics):
                calculated: set[str] = set()
                for candidate in (annual_candidate, sharpe_candidate):
                    if candidate is None or isinstance(candidate.get("candidate_ref"), str):
                        continue
                    candidate_key = str(
                        candidate.get("candidate_id")
                        or candidate.get("candidate_hash")
                        or id(candidate)
                    )
                    if candidate_key in calculated:
                        continue
                    calculated.add(candidate_key)
                    try:
                        full_metrics = full_period_metrics(
                            candidate,
                            config.start_date,
                            config.end_date,
                        )
                    except Exception:  # noqa: BLE001 - full-period display metrics do not discard a valid strategy
                        logger.exception("全区间策略收益回测失败: %s", ref.symbol)
                        continue
                    current_metrics = _mapping(candidate.get("metrics"))
                    candidate["metrics"] = {**current_metrics, "full_period": full_metrics}
            record: dict[str, object] = {
                "market": ref.market,
                "code": ref.code,
                "name": ref.name or "",
                "annual_candidate": annual_candidate,
                "sharpe_candidate": sharpe_candidate,
                "benchmark": benchmark,
                "data_fingerprint": str(result.get("data_fingerprint") or ""),
                "market_fingerprint": result_market_fingerprint,
                "config_fingerprint": config_fingerprint,
                "source_job_id": job_id,
                "split_metadata": dict(split_metadata)
                if isinstance(split_metadata, Mapping)
                else None,
            }
            single_result = None
            if mode == "single":
                single_result = {
                    **record,
                    "split_metadata": result.get("split_metadata"),
                    "date_range": result.get("date_range"),
                    "condition_candidates": result.get("condition_candidates"),
                    "risk_trials_per_rule_set": result.get("risk_trials_per_rule_set"),
                    "risk_search_runs": result.get("risk_search_runs"),
                }
            return record, single_result

    def _run_full_research(
        self,
        refs: list[StockRef],
        *,
        calendar: Sequence[date],
        settings: Mapping[str, object],
        value_ids: Sequence[str],
        signal_ids: Sequence[str],
        config_fingerprint: str,
        prior_entries: Mapping[tuple[str, str], tuple[str, str, str, bool]],
        job_id: str,
        total_work: int,
        progress_callback: PoolProgressCallback | None,
        abort_check: Callable[[], bool] | None,
        entry_writer: PoolEntryWriter | None,
    ) -> tuple[int, int, int, list[dict[str, str]]]:
        total = len(refs)
        coordinator = _PoolProgressCoordinator(
            total_symbols=total,
            total_work=total_work,
            callback=progress_callback,
            maximum_workers=self._max_pool_workers,
        )
        errors: list[dict[str, str]] = []
        completed = 0
        skipped = 0
        failed = 0
        next_index = 0
        ramp = PoolConcurrencyRamp(interval_seconds=POOL_RAMP_INTERVAL_SECONDS)
        active: dict[
            Future[tuple[dict[str, object], dict[str, object] | None]],
            tuple[int, StockRef],
        ] = {}
        with ThreadPoolExecutor(
            max_workers=self._max_pool_workers,
            thread_name_prefix="strategy-pool-symbol",
        ) as executor:
            while next_index < total or active:
                if self._evaluation_worker_budget is not None:
                    self._evaluation_worker_budget.set_capacity(
                        pool_evaluation_budget_capacity(
                            in_use_workers=self._evaluation_worker_budget.in_use_workers
                        )
                    )
                if abort_check is not None and abort_check():
                    for future in active:
                        future.cancel()
                    raise TaskCancelledError("全市场策略池研究已取消")

                while next_index < total:
                    ref = refs[next_index]
                    index = next_index + 1
                    symbol = f"{ref.market}:{ref.code}"
                    prior = prior_entries.get((ref.market, ref.code))
                    if self._can_reuse(prior, config_fingerprint):
                        reused_entry = (
                            self._pool_store.get(ref.market, ref.code)
                            if self._pool_store is not None
                            else None
                        )
                        metrics_backfilled = False
                        if reused_entry is not None and self._needs_full_period_backfill(
                            reused_entry
                        ):
                            if active:
                                break
                            _report(
                                progress_callback,
                                completed,
                                total_work,
                                "补算全区间策略收益",
                                symbol,
                                skipped,
                            )
                            updated = self._backfill_full_period_metrics(
                                ref,
                                reused_entry,
                                start_date=calendar[0],
                                end_date=calendar[-1],
                                source_job_id=job_id,
                            )
                            if updated is not None:
                                if entry_writer is not None:
                                    entry_writer(updated)
                                elif self._pool_store is not None:
                                    self._pool_store.upsert_automatic(
                                        market=ref.market,
                                        code=ref.code,
                                        name=str(updated["name"]),
                                        annual_candidate=cast(
                                            dict[str, object] | None,
                                            updated["annual_candidate"],
                                        ),
                                        sharpe_candidate=cast(
                                            dict[str, object] | None,
                                            updated["sharpe_candidate"],
                                        ),
                                        data_fingerprint=str(updated["data_fingerprint"]),
                                        market_fingerprint=str(updated["market_fingerprint"]),
                                        config_fingerprint=str(updated["config_fingerprint"]),
                                        source_job_id=job_id,
                                        benchmark=cast(
                                            Mapping[str, object] | None,
                                            updated.get("benchmark"),
                                        ),
                                    )
                                metrics_backfilled = True
                        if (
                            prior is not None
                            and prior[2] != "insufficient_history"
                            and not prior[3]
                            and self._pool_store is not None
                            and reused_entry is not None
                        ):
                            try:
                                automatic = _mapping(reused_entry.get("automatic_candidates"))
                                reference_candidate = _mapping(
                                    automatic.get("annual_return") or automatic.get("sharpe")
                                )
                                benchmark = _symbol_benchmark(
                                    self._adapter,
                                    ref,
                                    _individual_config(
                                        ref,
                                        calendar[0],
                                        calendar[-1],
                                        value_ids,
                                        signal_ids,
                                        settings,
                                    ),
                                    _mapping(reference_candidate.get("split_metadata")),
                                )
                                if benchmark is not None:
                                    checkpoint = {
                                        "market": ref.market,
                                        "code": ref.code,
                                        "name": ref.name or "",
                                        "benchmark": benchmark,
                                        "benchmark_only": True,
                                    }
                                    if entry_writer is not None:
                                        entry_writer(checkpoint)
                                    else:
                                        self._pool_store.update_benchmark(
                                            ref.market,
                                            ref.code,
                                            benchmark,
                                        )
                            except Exception:  # noqa: BLE001 - benchmark backfill must not fail strategy reuse
                                logger.exception("全市场策略池基准回填失败: %s", symbol)
                        skipped += 1
                        completed += 1
                        coordinator.finish(
                            index,
                            symbol,
                            "补算全区间策略收益" if metrics_backfilled else "复用已完成标的",
                            skipped=True,
                        )
                        next_index += 1
                        continue

                    lookahead = refs[next_index : next_index + self._max_pool_workers]
                    estimated_bytes = max(
                        estimate_ticker_working_set_bytes(
                            max(0, int(candidate.bar_count or len(calendar))),
                            value_count=len(value_ids),
                            signal_count=len(signal_ids),
                        )
                        for candidate in lookahead
                    )
                    memory = memory_status()
                    ramp.observe_memory(
                        memory,
                        len(active),
                        shared_worker_bytes=(
                            self._evaluation_worker_budget.in_use_workers
                            * EVALUATION_WORKER_MEMORY_BYTES
                            if self._evaluation_worker_budget is not None
                            else 0
                        ),
                    )
                    estimated_bytes = max(estimated_bytes, ramp.observed_worker_bytes)
                    reserved_bytes = (
                        max(
                            0,
                            min(
                                self._evaluation_worker_budget.capacity,
                                (len(active) + 1) * pool_evaluation_worker_count(),
                            )
                            - self._evaluation_worker_budget.in_use_workers,
                        )
                        * EVALUATION_WORKER_MEMORY_BYTES
                        if self._evaluation_worker_budget is not None
                        else 0
                    )
                    memory_worker_limit = memory_bounded_worker_count(
                        status=memory,
                        estimated_worker_bytes=estimated_bytes,
                        reserved_bytes=reserved_bytes,
                        maximum_workers=self._max_pool_workers,
                        active_workers=len(active),
                    )
                    visible_cpus = visible_cpu_count()
                    cpu_load_average = get_cpu_load_average_1m()
                    cpu_worker_limit = cpu_bounded_worker_count(
                        load_average_1m=cpu_load_average,
                        visible_cpus=visible_cpus,
                        maximum_workers=self._max_pool_workers,
                        active_workers=len(active),
                    )
                    target_workers = ramp.target(
                        resource_limit=min(memory_worker_limit, cpu_worker_limit),
                        active_workers=len(active),
                        now=time.monotonic(),
                    )
                    coordinator.set_resource_budget(
                        memory_worker_limit=memory_worker_limit,
                        available_bytes=memory.available_bytes,
                        cpu_worker_limit=cpu_worker_limit,
                        cpu_load_average_1m=cpu_load_average,
                        visible_cpus=visible_cpus,
                        target_workers=target_workers,
                        estimated_worker_bytes=estimated_bytes,
                    )
                    if len(active) >= target_workers:
                        break

                    config = _individual_config(
                        ref,
                        calendar[0],
                        calendar[-1],
                        value_ids,
                        signal_ids,
                        settings,
                    )
                    ticker_progress = _TickerProgress(
                        index=index,
                        total=total_work,
                        symbol=symbol,
                        name=ref.name or "",
                        callback=None,
                        coordinator=coordinator,
                    )
                    # previous_market_fingerprint 刻意传空：守卫要求"提交时的指纹"
                    # 与"研究加载的指纹"同口径，而研究内部的指纹是**复权指标口径**
                    # （backtest/service._metric_frame，随事件补录自动失效），提交时
                    # 复算原始价口径必然对不上——有分红送转历史的股票会全部误报
                    # 「研究期间行情变化」（2026-10-07 实测）。研究加载的数据自身
                    # 就是一份一致快照，指纹随结果落库即可；中途同步的影响由跟踪
                    # 持续检验兜底。
                    future = executor.submit(
                        self._research_symbol,
                        ref,
                        config,
                        job_id=job_id,
                        mode="full",
                        config_fingerprint=config_fingerprint,
                        previous_market_fingerprint="",
                        progress=ticker_progress,
                        abort_check=abort_check,
                    )
                    active[future] = (index, ref)
                    next_index += 1

                if not active:
                    continue
                finished, _pending = wait(
                    active,
                    timeout=0.5,
                    return_when=FIRST_COMPLETED,
                )
                for future in finished:
                    index, ref = active.pop(future)
                    symbol = f"{ref.market}:{ref.code}"
                    try:
                        record, _single_result = future.result()
                        if entry_writer is not None:
                            entry_writer(record)
                    except TaskCancelledError:
                        raise
                    except InsufficientResearchHistoryError as exc:
                        try:
                            market_fingerprint = _market_fingerprint(self._adapter, ref)
                        except Exception:  # noqa: BLE001 - do not cache without a stable fingerprint
                            logger.exception("读取历史样本不足标的指纹失败: %s", symbol)
                            market_fingerprint = ""
                        if market_fingerprint:
                            message = f"历史样本不足：{exc}"
                            checkpoint = {
                                "market": ref.market,
                                "code": ref.code,
                                "name": ref.name or "",
                                "error": message,
                                "status": "insufficient_history",
                                "data_fingerprint": exc.data_fingerprint,
                                "market_fingerprint": market_fingerprint,
                                "config_fingerprint": config_fingerprint,
                                "source_job_id": job_id,
                            }
                            if entry_writer is not None:
                                entry_writer(checkpoint)
                            elif self._pool_store is not None:
                                self._pool_store.persist_worker_entry(
                                    checkpoint,
                                    source_job_id=job_id,
                                )
                            skipped += 1
                            completed += 1
                            coordinator.finish(index, symbol, "历史样本不足，已记录", skipped=True)
                        else:
                            failed += 1
                            errors.append({"symbol": symbol, "error": str(exc)[:500]})
                            checkpoint = {
                                "market": ref.market,
                                "code": ref.code,
                                "name": ref.name or "",
                                "error": str(exc)[:500],
                                "config_fingerprint": config_fingerprint,
                                "source_job_id": job_id,
                            }
                            if entry_writer is not None:
                                entry_writer(checkpoint)
                            elif self._pool_store is not None:
                                self._pool_store.persist_worker_entry(
                                    checkpoint,
                                    source_job_id=job_id,
                                )
                            coordinator.finish(index, symbol, "读取行情指纹失败", failed=True)
                        continue
                    except Exception as exc:  # noqa: BLE001 - isolate stock failures
                        failed += 1
                        message = str(exc).strip()[:500] or type(exc).__name__
                        if exc.__cause__ is not None:
                            cause = str(exc.__cause__).strip()
                            if cause and cause not in message:
                                message = f"{message}；底层错误：{cause}"[:500]
                        logger.exception("全市场策略池单只股票研究失败: %s", symbol)
                        errors.append({"symbol": symbol, "error": message})
                        if entry_writer is not None:
                            entry_writer(
                                {
                                    "market": ref.market,
                                    "code": ref.code,
                                    "name": ref.name or "",
                                    "error": message,
                                    "config_fingerprint": config_fingerprint,
                                    "source_job_id": job_id,
                                }
                            )
                        coordinator.finish(index, symbol, "单只股票失败，继续", failed=True)
                    else:
                        completed += 1
                        coordinator.finish(index, symbol, "保存标的策略")
        return completed, skipped, failed, errors

    def run(
        self,
        payload: Mapping[str, object],
        *,
        job_id: str,
        progress_callback: PoolProgressCallback | None = None,
        abort_check: Callable[[], bool] | None = None,
        entry_writer: PoolEntryWriter | None = None,
    ) -> dict[str, object]:
        mode = str(payload.get("mode") or "full")
        if mode not in {"full", "single", "signals", "portfolio"}:
            raise ValueError("策略池任务范围无效")
        if mode == "signals":
            signal_symbol = (
                _symbol_from_payload(payload)
                if payload.get("market") is not None or payload.get("code") is not None
                else None
            )
            return self._scan_signals(
                payload,
                job_id=job_id,
                single_symbol=signal_symbol,
                progress_callback=progress_callback,
                abort_check=abort_check,
            )
        single_symbol = _symbol_from_payload(payload) if mode == "single" else None
        risk_trials = _trial_count(payload.get("risk_trials", 3_000))
        refs = self._list_refs(single_symbol)
        if not refs:
            raise ValueError("本地行情没有可研究的 A 股日线")
        calendar = self._common_calendar()
        if not calendar:
            raise ValueError("本地行情没有已完成的 A 股日线")
        # 2015 全年只作指标预热（引擎读取 start_date 之前的 warmup_bars 覆盖它），
        # 研究与回测窗口从 RESEARCH_START 起跑；基准与全区间展示窗口随之对齐。
        calendar = [day for day in calendar if day >= RESEARCH_START]
        if not calendar:
            raise ValueError("本地行情没有研究窗口内（2016 年起）的已完成日线")
        horizon = _positive_int(payload.get("horizon"), 10, "波段预测窗口")
        if mode == "portfolio":
            windows = common_calendar_windows(calendar, purge_days=horizon)
            metadata = _calendar_metadata(calendar, windows, horizon)
            objective = str(payload.get("objective") or "")
            if objective not in {"annual_return", "sharpe"}:
                raise ValueError("组合回测必须选择年化收益或夏普目标")
            summary = self._portfolio_summary(
                cast(PoolObjective, objective),
                windows,
                calendar[-1],
                progress_callback=progress_callback,
                progress_index=0,
                progress_total=1,
                abort_check=abort_check,
            )
            return {"mode": "portfolio", "common_calendar": metadata, "portfolio": summary}
        catalog = default_indicator_catalog()
        settings = _research_settings(payload, risk_trials)
        settings["value_ids"] = list(catalog.value_ids)
        settings["signal_ids"] = list(catalog.signal_ids)
        formula_ids = {
            value_id.split(".", 1)[0] for value_id in (*catalog.value_ids, *catalog.signal_ids)
        }
        settings["formula_versions"] = {
            formula_id: FORMULA_REGISTRY.formula(formula_id).cache_version
            for formula_id in sorted(formula_ids)
        }
        from selector_app.individual_research.search_policy import search_policy_settings

        settings["search_policy"] = search_policy_settings()
        settings["algorithm_version"] = MARKET_POOL_ALGORITHM_VERSION
        config_fingerprint = _fingerprint(settings)
        rebuild = payload.get("rebuild") is True
        if rebuild:
            if mode != "full":
                raise ValueError("只有全市场任务支持清空重建")
            if abort_check is not None and abort_check():
                raise TaskCancelledError("全市场策略池研究已取消")
            if entry_writer is not None:
                entry_writer({"reset": True})
            elif self._pool_store is not None:
                self._pool_store.persist_worker_entry({"reset": True}, source_job_id=job_id)
            else:
                raise ValueError("清空重建需要可写策略池")
        prior_entries = self._prior_entries() if not rebuild and mode in {"full", "single"} else {}
        started = date.today()
        if mode == "single" and self._pool_store is not None:
            ref = refs[0]
            prior = prior_entries.get((ref.market, ref.code))
            if (
                prior is not None
                and prior[1] == config_fingerprint
                and prior[2] == "insufficient_history"
            ):
                try:
                    market_fingerprint = _market_fingerprint(self._adapter, ref)
                except Exception:  # noqa: BLE001 - fingerprint errors should allow a fresh attempt
                    logger.exception("读取单股历史样本不足指纹失败: %s", ref.symbol)
                    market_fingerprint = ""
                else:
                    # 显式单股重跑与全市场增量的语义不同：这只股票上次连研究都没法
                    # 开始（历史不足），行情没变时重试注定以同样失败收场，跳过省
                    # 一次必败寻优；数据长了的当天指纹就不同，自然落到重新研究。
                    if market_fingerprint and market_fingerprint == prior[0]:
                        saved_entry = self._pool_store.get(ref.market, ref.code) or {}
                        _report(
                            progress_callback,
                            1,
                            1,
                            "复用历史样本不足记录",
                            ref.symbol,
                            1,
                        )
                        return {
                            "mode": "single",
                            "total_symbols": 1,
                            "completed_symbols": 1,
                            "skipped_symbols": 1,
                            "failed_symbols": 0,
                            "errors": [],
                            "split_policy": {
                                "method": "按每只股票自身的已完成交易日顺序切分训练、验证、留出",
                                "train_ratio": 0.6,
                                "validation_ratio": 0.2,
                                "holdout_ratio": 0.2,
                                "purge_trading_days": horizon,
                            },
                            "single_candidate": {
                                "market": ref.market,
                                "code": ref.code,
                                "name": ref.name or "",
                                "status": "insufficient_history",
                                "error": saved_entry.get("last_error")
                                or "历史样本不足：当前数据和研究配置未变化",
                                "annual_candidate": None,
                                "sharpe_candidate": None,
                                "saved_to_pool": True,
                            },
                            "risk_trials_per_rule_set": risk_trials,
                            "started_date": started.isoformat(),
                        }
        single_result: dict[str, object] | None = None
        completed = 0
        skipped = 0
        failed = 0
        errors: list[dict[str, str]] = []
        total = len(refs)
        total_work = total
        if mode == "full":
            completed, skipped, failed, errors = self._run_full_research(
                refs,
                calendar=calendar,
                settings=settings,
                value_ids=catalog.value_ids,
                signal_ids=catalog.signal_ids,
                config_fingerprint=config_fingerprint,
                prior_entries=prior_entries,
                job_id=job_id,
                total_work=total_work,
                progress_callback=progress_callback,
                abort_check=abort_check,
                entry_writer=entry_writer,
            )
        else:
            ref = refs[0]
            symbol = f"{ref.market}:{ref.code}"
            config = _individual_config(
                ref,
                calendar[0],
                calendar[-1],
                catalog.value_ids,
                catalog.signal_ids,
                settings,
            )
            ticker_progress = _TickerProgress(
                index=1,
                total=total_work,
                symbol=symbol,
                name=ref.name or "",
                callback=progress_callback,
            )
            try:
                _record, single_result = self._research_symbol(
                    ref,
                    config,
                    job_id=job_id,
                    mode="single",
                    config_fingerprint=config_fingerprint,
                    previous_market_fingerprint="",
                    progress=ticker_progress,
                    abort_check=abort_check,
                )
                saved_to_pool = False
                if entry_writer is not None:
                    entry_writer(_record)
                    saved_to_pool = True
                elif self._pool_store is not None:
                    annual_candidate = _record.get("annual_candidate")
                    sharpe_candidate = _record.get("sharpe_candidate")
                    benchmark = _record.get("benchmark")
                    self._pool_store.upsert_automatic(
                        market=ref.market,
                        code=ref.code,
                        name=ref.name or "",
                        annual_candidate=(
                            dict(annual_candidate)
                            if isinstance(annual_candidate, Mapping)
                            else None
                        ),
                        sharpe_candidate=(
                            dict(sharpe_candidate)
                            if isinstance(sharpe_candidate, Mapping)
                            else None
                        ),
                        data_fingerprint=str(_record.get("data_fingerprint") or ""),
                        market_fingerprint=str(_record.get("market_fingerprint") or ""),
                        config_fingerprint=str(_record.get("config_fingerprint") or ""),
                        source_job_id=job_id,
                        benchmark=(dict(benchmark) if isinstance(benchmark, Mapping) else None),
                    )
                    saved_to_pool = True
                if single_result is not None:
                    single_result["saved_to_pool"] = saved_to_pool
                completed = 1
                _report(progress_callback, completed, total_work, "保存标的候选", symbol, 0)
            except TaskCancelledError:
                raise
            except InsufficientResearchHistoryError as exc:
                try:
                    market_fingerprint = _market_fingerprint(self._adapter, ref)
                except Exception as fingerprint_error:  # noqa: BLE001 - missing fingerprint retries
                    message = f"{symbol} 研究样本不足，且无法记录行情指纹"
                    raise ValueError(message) from fingerprint_error
                message = f"历史样本不足：{exc}"
                checkpoint = {
                    "market": ref.market,
                    "code": ref.code,
                    "name": ref.name or "",
                    "error": message,
                    "status": "insufficient_history",
                    "data_fingerprint": exc.data_fingerprint,
                    "market_fingerprint": market_fingerprint,
                    "config_fingerprint": config_fingerprint,
                    "source_job_id": job_id,
                }
                if entry_writer is not None:
                    entry_writer(checkpoint)
                    saved_to_pool = True
                elif self._pool_store is not None:
                    self._pool_store.persist_worker_entry(checkpoint, source_job_id=job_id)
                    saved_to_pool = True
                else:
                    saved_to_pool = False
                single_result = {
                    "market": ref.market,
                    "code": ref.code,
                    "name": ref.name or "",
                    "status": "insufficient_history",
                    "error": message,
                    "split_metadata": exc.split_metadata,
                    "annual_candidate": None,
                    "sharpe_candidate": None,
                    "saved_to_pool": saved_to_pool,
                }
                skipped = 1
                completed = 1
                _report(progress_callback, completed, total_work, "历史样本不足", symbol, skipped)
            except Exception as exc:  # noqa: BLE001 - expose a safe single-symbol failure
                raise ValueError(f"{symbol} 个股研究失败：{str(exc)[:500]}") from exc

        return {
            "mode": mode,
            "total_symbols": total,
            "completed_symbols": completed,
            "skipped_symbols": skipped,
            "failed_symbols": failed,
            "errors": errors[:200],
            "split_policy": {
                "method": "按每只股票自身的已完成交易日顺序切分训练、验证、留出",
                "train_ratio": 0.6,
                "validation_ratio": 0.2,
                "holdout_ratio": 0.2,
                "purge_trading_days": horizon,
            },
            "single_candidate": single_result,
            "risk_trials_per_rule_set": risk_trials,
            "started_date": started.isoformat(),
        }

    def _portfolio_summary(
        self,
        objective: PoolObjective,
        windows: Mapping[str, tuple[date, date]],
        latest_date: date,
        *,
        progress_callback: PoolProgressCallback | None,
        progress_index: int,
        progress_total: int,
        abort_check: Callable[[], bool] | None,
    ) -> dict[str, object]:
        if self._portfolio_service is None or self._pool_store is None:
            return {"objective": objective, "available": False, "reason": "组合回测服务未配置"}
        refs = self._list_refs(None)
        strategies, priorities = _portfolio_strategies(
            self._pool_store.list_entries(),
            refs,
            objective,
        )
        if not strategies:
            return {"objective": objective, "available": False, "reason": "没有可用策略"}
        config = _pool_portfolio_config(
            refs,
            strategies,
            priorities,
            start_date=windows["holdout"][0],
            end_date=windows["holdout"][1],
        )

        def report_progress(current: int, total: int) -> None:
            if abort_check is not None and abort_check():
                raise TaskCancelledError("组合留出段回测已取消")
            if progress_callback is not None:
                ratio = current / total if total else 1.0
                progress_callback(
                    progress_index + (1 if ratio >= 1.0 else 0),
                    progress_total,
                    "组合留出段回测",
                    {
                        "overall_progress": (progress_index + ratio) / max(1, progress_total),
                        "objective": objective,
                        "completed_symbols": current,
                        "total_symbols": total,
                    },
                )

        try:
            report = self._portfolio_service.run(
                config,
                report_progress,
                abort_check=abort_check,
            )
        except TaskCancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - preserve per-symbol pool results if portfolio fails
            return {
                "objective": objective,
                "available": False,
                "reason": str(exc).strip()[:500] or "组合留出段回测失败",
            }
        return {
            "objective": objective,
            "available": True,
            "start_date": report.start_date,
            "end_date": report.end_date,
            "strategy_count": len(strategies),
            "performance": dict(report.performance),
            "benchmark": dict(report.benchmark) if report.benchmark is not None else None,
            "risk_controls": dict(report.risk_controls),
            "transactions": len(report.trades),
            "closed_trades": sum(1 for trade in report.trades if trade.get("direction") == "SELL"),
            "data_fingerprint": report.data_fingerprint,
        }

    def _current_strategy_snapshot(
        self,
        refs: Sequence[StockRef],
        strategies: Mapping[str, Mapping[str, object]],
        priorities: Mapping[str, float],
        *,
        start_date: date,
        latest_date: date,
        progress_callback: PoolProgressCallback | None,
        progress_index: int,
        progress_total: int,
        abort_check: Callable[[], bool] | None,
    ) -> dict[str, object]:
        if self._portfolio_service is None:
            return {"available": False, "reason": "逐股持仓回放服务未配置"}
        selected_refs = [ref for ref in refs if f"{ref.market}{ref.code}" in strategies]
        if not selected_refs:
            if progress_callback is not None:
                _report(
                    progress_callback,
                    progress_index + 1,
                    progress_total,
                    "没有可用的逐股持仓策略",
                    "",
                    0,
                )
            return {
                "available": True,
                "holding_scope": "per_stock_strategy",
                "as_of_date": latest_date.isoformat(),
                "current_state": {"as_of": latest_date.isoformat(), "holdings": []},
                "holdings": [],
                "risk_exit_signals": [],
            }

        holdings: list[dict[str, object]] = []
        risk_exits: list[dict[str, object]] = []
        fingerprints: list[str] = []
        failures: list[str] = []
        batches = [
            selected_refs[offset : offset + STRATEGY_HOLDING_BATCH_SIZE]
            for offset in range(0, len(selected_refs), STRATEGY_HOLDING_BATCH_SIZE)
        ]
        for batch_index, batch_refs in enumerate(batches):
            if abort_check is not None and abort_check():
                raise TaskCancelledError("逐股持仓状态回放已取消")
            batch_strategies = {
                f"{ref.market}{ref.code}": strategies[f"{ref.market}{ref.code}"]
                for ref in batch_refs
            }
            batch_priorities = {symbol: priorities.get(symbol, 1.0) for symbol in batch_strategies}

            def report_progress(current: int, total: int) -> None:
                if abort_check is not None and abort_check():
                    raise TaskCancelledError("逐股持仓状态回放已取消")
                if progress_callback is None:
                    return
                ratio = current / total if total else 1.0
                progress_callback(
                    progress_index + batch_index + (1 if ratio >= 1.0 else 0),
                    progress_total,
                    "回放各股策略持仓",
                    {
                        "overall_progress": (progress_index + batch_index + ratio)
                        / max(1, progress_total),
                        "completed_symbols": min(
                            len(selected_refs),
                            batch_index * STRATEGY_HOLDING_BATCH_SIZE + current,
                        ),
                        "total_symbols": len(selected_refs),
                        "current_parallelism": 1,
                        "maximum_parallelism": 1,
                    },
                )

            config = _pool_portfolio_config(
                batch_refs,
                batch_strategies,
                batch_priorities,
                start_date=start_date,
                end_date=latest_date,
                max_positions=len(batch_refs),
                initial_cash=STRATEGY_HOLDING_VIRTUAL_CASH,
            )
            try:
                report = self._portfolio_service.run(
                    config,
                    report_progress,
                    abort_check=abort_check,
                )
            except TaskCancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - isolate one group of ticker failures
                failures.append(str(exc).strip()[:200] or type(exc).__name__)
                continue

            fingerprints.append(report.data_fingerprint)
            current_state = report.current_state or {}
            batch_as_of = str(current_state.get("as_of") or latest_date.isoformat())
            raw_holdings = current_state.get("holdings")
            if isinstance(raw_holdings, (list, tuple)):
                for item in raw_holdings:
                    if not isinstance(item, Mapping):
                        continue
                    holding: dict[str, object] = {
                        "market": str(item.get("market") or ""),
                        "code": str(item.get("code") or ""),
                        "name": str(item.get("name") or ""),
                        "status": "held",
                    }
                    holding_return = _finite_metric(item.get("unrealized_pnl_pct"))
                    if holding_return is not None:
                        holding["holding_return_pct"] = holding_return
                    holdings.append(holding)
            for trade in report.trades:
                if trade.get("date") != batch_as_of or trade.get("risk_trigger") is None:
                    continue
                risk_exit = dict(trade)
                pnl = _finite_metric(trade.get("pnl"))
                cost_basis = _finite_metric(trade.get("cost_basis"))
                if pnl is not None and cost_basis is not None and cost_basis > 0:
                    risk_exit["trade_return_pct"] = pnl / cost_basis
                risk_exits.append(risk_exit)

        if progress_callback is not None:
            progress_callback(
                progress_index + len(batches),
                progress_total,
                "完成逐股持仓回放",
                {
                    "overall_progress": min(
                        1.0,
                        (progress_index + len(batches)) / max(1, progress_total),
                    ),
                    "completed_symbols": len(selected_refs),
                    "total_symbols": len(selected_refs),
                },
            )
        data_fingerprint = hashlib.sha256(
            "|".join(sorted(fingerprints)).encode("utf-8")
        ).hexdigest()
        return {
            "available": bool(fingerprints),
            "as_of_date": latest_date.isoformat(),
            "current_state": {
                "as_of": latest_date.isoformat(),
                "positions_count": len(holdings),
                "holdings": holdings,
            },
            "holdings": holdings,
            "risk_exit_signals": risk_exits,
            "data_fingerprint": data_fingerprint,
            "failed_batches": len(failures),
            "reason": "；".join(failures[:3]) if failures else None,
        }

    def _list_refs(self, single_symbol: tuple[str, str] | None) -> list[StockRef]:
        refs = self._adapter.list_stock_refs(
            "",
            "all",
            instrument_types=("stock",),
        )
        with self._store.read_session() as connection:
            completed_symbols = {
                (str(row[0]), str(row[1]))
                for row in connection.execute(
                    """
                    SELECT DISTINCT market, code
                    FROM daily_bars
                    WHERE instrument_type = 'stock' AND bar_status = 'completed'
                    """
                ).fetchall()
            }
        refs = [ref for ref in refs if (ref.market, ref.code) in completed_symbols]
        if single_symbol is not None:
            refs = [ref for ref in refs if (ref.market, ref.code) == single_symbol]
            if not refs:
                raise ValueError("本地行情找不到这只 A 股")
        return sorted(refs, key=lambda ref: (ref.market, ref.code))

    def _scan_signals(
        self,
        payload: Mapping[str, object],
        *,
        job_id: str,
        single_symbol: tuple[str, str] | None,
        progress_callback: PoolProgressCallback | None,
        abort_check: Callable[[], bool] | None,
    ) -> dict[str, object]:
        objective = str(payload.get("objective") or "annual_return")
        if objective not in {"annual_return", "sharpe"}:
            raise ValueError("策略池目标必须是 annual_return 或 sharpe")
        if self._backtest_service is None or self._pool_store is None:
            raise ValueError("策略池信号扫描服务未配置")
        refs = self._list_refs(single_symbol)
        raw_entries = self._pool_store.list_entries()
        entries = {(str(item.get("market")), str(item.get("code"))): item for item in raw_entries}
        position_strategies, position_priorities = _portfolio_strategies(
            raw_entries,
            refs,
            cast(PoolObjective, objective),
            require_validation_metric=False,
        )
        position_refs = [ref for ref in refs if f"{ref.market}{ref.code}" in position_strategies]
        strategy_snapshot = []
        for ref in position_refs:
            active = position_strategies.get(f"{ref.market}{ref.code}")
            if active is None:
                continue
            candidate_version = active.get("candidate_id") or active.get("candidate_hash")
            strategy_snapshot.append(
                {
                    "market": ref.market,
                    "code": ref.code,
                    "candidate_id": str(candidate_version or ""),
                }
            )
        strategy_snapshot.sort(key=lambda item: (item["market"], item["code"]))
        market_status = self._store.status()
        buy_signals: list[dict[str, object]] = []
        sell_signals: list[dict[str, object]] = []
        states: list[dict[str, object]] = []
        failed = 0
        position_batches = (
            math.ceil(len(position_refs) / STRATEGY_HOLDING_BATCH_SIZE)
            if self._portfolio_service is not None
            else 0
        )
        total_work = len(position_refs) + max(1, position_batches)
        latest_date: date | None = None
        if market_status.data_end:
            try:
                latest_date = date.fromisoformat(market_status.data_end[:10])
            except ValueError:
                latest_date = None
        for index, ref in enumerate(position_refs, start=1):
            if abort_check is not None and abort_check():
                raise TaskCancelledError("策略池信号扫描已取消")
            symbol = f"{ref.market}{ref.code}"
            entry = entries.get((ref.market, ref.code))
            active_candidate = _active_candidate(entry, objective)
            if active_candidate is None:
                continue
            try:
                buy_rule = _parse_rule(active_candidate.get("buy_rule"))
                sell_rule = _parse_rule(active_candidate.get("sell_rule"))
                if buy_rule is None or sell_rule is None:
                    raise ValueError("策略缺少买入或卖出规则")
                union_rule = _union_rule(buy_rule, sell_rule)
                feature_ids = _feature_ids(buy_rule, sell_rule)
                backtest_config = BacktestConfig(
                    market=ref.market,
                    code=ref.code,
                    vipdoc_path="",
                    buy_rule=union_rule,
                    sell_rule=union_rule,
                    always_in_market=False,
                    close_at_end=False,
                    instrument_type="stock",
                    board=ref.board,
                )
                prepared = self._backtest_service.prepare_for_ref(
                    backtest_config,
                    ref,
                    feature_ids=feature_ids,
                )
                observed_date = prepared.frame.iloc[-1]["date"]
                if hasattr(observed_date, "date"):
                    observed_date = observed_date.date()
                if isinstance(observed_date, date):
                    latest_date = max(latest_date, observed_date) if latest_date else observed_date
                buy = bool(
                    evaluate_rule_group(buy_rule, prepared.formula_results, FORMULA_REGISTRY)[-1]
                )
                sell = bool(
                    evaluate_rule_group(sell_rule, prepared.formula_results, FORMULA_REGISTRY)[-1]
                )
                as_of = str(prepared.frame.iloc[-1]["date"])[:10]
                if buy:
                    buy_signals.append(_signal_row(ref, active_candidate, objective, as_of, "buy"))
                if sell:
                    sell_signals.append(
                        _signal_row(ref, active_candidate, objective, as_of, "sell")
                    )
                state = "buy_sell" if buy and sell else "buy" if buy else "sell" if sell else "flat"
                states.append(_signal_state(ref, state, active_candidate, objective))
            except Exception as exc:  # noqa: BLE001 - one unavailable formula cannot hide other signals
                failed += 1
                states.append(
                    _signal_state(ref, "error", active_candidate, objective, str(exc)[:200])
                )
            _report(progress_callback, index, total_work, "扫描当天买卖信号", symbol, 0)
        buy_signals.sort(key=lambda row: _signal_sort_key(row, objective))
        sell_signals.sort(key=lambda row: _signal_sort_key(row, objective))
        model_portfolio: dict[str, object] = {
            "available": False,
            "holding_scope": "per_stock_strategy",
            "holdings": [],
            "risk_exit_signals": [],
            "reason": "没有可用于逐股回放的策略",
        }
        if (
            latest_date is not None
            and self._portfolio_service is not None
            and self._pool_store is not None
        ):
            try:
                start_date = (
                    date.fromisoformat(market_status.data_start[:10])
                    if market_status.data_start
                    else latest_date
                )
                model_portfolio = self._current_strategy_snapshot(
                    position_refs,
                    position_strategies,
                    position_priorities,
                    start_date=start_date,
                    latest_date=latest_date,
                    progress_callback=progress_callback,
                    progress_index=len(position_refs),
                    progress_total=total_work,
                    abort_check=abort_check,
                )
            except ValueError as exc:
                model_portfolio = {
                    "available": False,
                    "holding_scope": "per_stock_strategy",
                    "holdings": [],
                    "risk_exit_signals": [],
                    "reason": str(exc),
                }
            except TaskCancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - direct rule signals are still useful
                model_portfolio = {
                    "available": False,
                    "holding_scope": "per_stock_strategy",
                    "holdings": [],
                    "risk_exit_signals": [],
                    "reason": str(exc).strip()[:500] or "模拟组合状态计算失败",
                }
        elif progress_callback is not None:
            _report(
                progress_callback,
                len(position_refs) + 1,
                total_work,
                "逐股持仓回放服务不可用",
                "",
                0,
            )
        model_portfolio["holding_scope"] = "per_stock_strategy"
        current_state = _mapping(model_portfolio.get("current_state"))
        current_holdings = current_state.get("holdings")
        risk_exit_signals = model_portfolio.get("risk_exit_signals")
        model_portfolio["holdings"] = (
            list(current_holdings) if isinstance(current_holdings, (list, tuple)) else []
        )
        model_portfolio["risk_exit_signals"] = (
            list(risk_exit_signals) if isinstance(risk_exit_signals, (list, tuple)) else []
        )
        states.sort(
            key=lambda row: (
                _status_order(str(row["status"])),
                *_signal_sort_key(row, objective),
            )
        )
        return {
            "mode": "signals",
            "objective": objective,
            "holding_scope": "per_stock_strategy",
            "holding_snapshot_complete": (
                single_symbol is None
                and model_portfolio.get("available") is True
                and not model_portfolio.get("failed_batches")
            ),
            "as_of_date": latest_date.isoformat() if latest_date else None,
            "signal_target": (
                {"market": single_symbol[0], "code": single_symbol[1]}
                if single_symbol is not None
                else None
            ),
            "market_snapshot": {
                "data_end": market_status.data_end,
                "bar_count": market_status.bar_count,
                "last_online_sync_at": market_status.last_online_sync_at,
            },
            "strategy_snapshot": strategy_snapshot,
            "buy_signals": buy_signals,
            "sell_signals": sell_signals,
            "states": states,
            "failed_symbols": failed,
            "completed_symbols": len(position_refs) - failed,
            "skipped_symbols": 0,
            "total_symbols": len(position_refs),
            "model_portfolio": model_portfolio,
            "signal_job_id": job_id,
        }

    def _common_calendar(self) -> list[date]:
        with self._store.read_session() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT trade_date
                FROM daily_bars
                WHERE instrument_type = 'stock' AND bar_status = 'completed'
                ORDER BY trade_date
                """
            ).fetchall()
        return [row[0] for row in rows if row and isinstance(row[0], date)]

    def _prior_entries(self) -> dict[tuple[str, str], tuple[str, str, str, bool]]:
        with self._store.read_session() as connection:
            # 「已有基准」的要求版本也得对得上：口径改过之后（例如从不复权换成
            # 分红再投资），老行必须重新补算，否则页面上会一直是改口径前的数字。
            rows = connection.execute(
                """
                SELECT market, code, market_fingerprint, config_fingerprint, status,
                       benchmark_json IS NOT NULL AND benchmark_json LIKE ?
                FROM strategy_pool_entries
                """,
                [_BENCHMARK_VERSION_LIKE],
            ).fetchall()
        return {
            (str(row[0]), str(row[1])): (
                str(row[2] or ""),
                str(row[3] or ""),
                str(row[4] or ""),
                bool(row[5]),
            )
            for row in rows
        }

    @staticmethod
    def _can_reuse(
        prior: tuple[str, str, str, bool] | None,
        config_fingerprint: str,
    ) -> bool:
        """增量是否跳过这只股票：只看配置与研究是否有结论，刻意不看行情。

        全市场逐股寻优要跑十几天、每天同步都会新增日线；行情指纹一变全部作废，
        增量就永远等不到复用的那一天（实测：每天点增量都从第一只重新跑）。
        策略选出之后的持续检验是**跟踪**（tracking）的职责——天天在重叠的数据
        上重选胜者，恰恰是多重检验校正要惩罚的那种研究。

        可复用的是研究**有了结论**的状态：ready / partial（选出策略）与
        no_strategy（研究跑完、没有通过门槛的候选）。最后这个同样是一次结论：
        薄样本的股票明天多半还是薄样本，每天重跑一小时寻优换不来新信息。要
        重新打开它们：改配置（指纹变化）、单股重跑、或清空重建。

        刻意**不可**复用的：insufficient_history（历史还没攒够，而数据每天都在
        长，攒够当天就该补研究）与 failed（错误应当重试）。
        """

        return bool(
            prior
            and prior[1] == config_fingerprint
            and prior[2] in {"ready", "partial", "no_strategy"}
        )

    @staticmethod
    def _needs_full_period_backfill(entry: Mapping[str, object]) -> bool:
        automatic_value = entry.get("automatic_candidates")
        automatic = automatic_value if isinstance(automatic_value, Mapping) else {}
        for objective in ("annual_return", "sharpe"):
            candidate = automatic.get(objective)
            if (
                not isinstance(candidate, Mapping)
                or isinstance(candidate.get("candidate_ref"), str)
                or candidate.get("replayable") is False
            ):
                continue
            metrics = _mapping(candidate.get("metrics"))
            full_period = _mapping(metrics.get("full_period"))
            if _finite_metric(full_period.get("total_return")) is None:
                return True
            # 平均持仓天数为新增指标：老候选缺失时同样触发快速回放补算
            if _finite_metric(full_period.get("avg_holding_days")) is None:
                return True
        return False

    def _backfill_full_period_metrics(
        self,
        ref: StockRef,
        entry: Mapping[str, object],
        *,
        start_date: date,
        end_date: date,
        source_job_id: str,
    ) -> dict[str, object] | None:
        if self._pool_store is None:
            return None
        automatic_value = entry.get("automatic_candidates")
        automatic_raw = automatic_value if isinstance(automatic_value, Mapping) else {}
        automatic: dict[str, object] = dict(automatic_raw)
        calculator = getattr(self._individual_service_for_thread(), "full_period_metrics", None)
        if not callable(calculator):
            return None
        changed = False
        seen: set[str] = set()
        for objective in ("annual_return", "sharpe"):
            candidate_value = automatic.get(objective)
            if (
                not isinstance(candidate_value, Mapping)
                or isinstance(candidate_value.get("candidate_ref"), str)
                or candidate_value.get("replayable") is False
            ):
                continue
            candidate_id = str(
                candidate_value.get("candidate_id")
                or candidate_value.get("candidate_hash")
                or objective
            )
            if candidate_id in seen:
                continue
            seen.add(candidate_id)
            if not self._needs_full_period_backfill(
                {"automatic_candidates": {objective: candidate_value}}
            ):
                continue
            try:
                full_metrics = calculator(candidate_value, start_date, end_date)
            except Exception:  # noqa: BLE001 - display metric backfill must not discard cached winners
                logger.exception("全区间策略收益补算失败: %s %s", ref.symbol, objective)
                continue
            if not isinstance(full_metrics, Mapping):
                continue
            updated = dict(candidate_value)
            current_metrics = _mapping(updated.get("metrics"))
            updated["metrics"] = {**current_metrics, "full_period": dict(full_metrics)}
            automatic[objective] = updated
            changed = True
        if not changed:
            return None
        return {
            "market": ref.market,
            "code": ref.code,
            "name": str(entry.get("name") or ref.name or ""),
            "annual_candidate": automatic.get("annual_return"),
            "sharpe_candidate": automatic.get("sharpe"),
            "benchmark": entry.get("benchmark"),
            "data_fingerprint": str(entry.get("data_fingerprint") or ""),
            "market_fingerprint": str(entry.get("market_fingerprint") or ""),
            "config_fingerprint": str(entry.get("config_fingerprint") or ""),
            "source_job_id": source_job_id,
        }


def _individual_config(
    ref: StockRef,
    start_date: date,
    end_date: date,
    value_ids: Sequence[str],
    signal_ids: Sequence[str],
    settings: Mapping[str, object],
) -> IndividualResearchConfig:
    return IndividualResearchConfig(
        market=ref.market,
        code=ref.code,
        name=ref.name or "",
        # 研究起点取「股票自身数据起点」与「统一研究起点」的较晚者：2016 年后才
        # 上市的票从上市起跑；老票被 RESEARCH_START 裁剪，2015 全年只作引擎的
        # 指标预热。注意这里必须是两者取较晚，不能让 first_date 单方面优先——
        # 那会把老票的研究窗口又拉回 2015，预热段就混进研究样本了。
        start_date=max(ref.first_date, start_date) if ref.first_date else start_date,
        end_date=min(ref.last_date or end_date, end_date),
        value_ids=tuple(value_ids),
        signal_ids=tuple(signal_ids),
        reversal_multiple=cast(float, settings["reversal_multiple"]),
        max_hold_days=cast("int | None", settings["max_hold_days"]),
        atr_window=cast(int, settings["atr_window"]),
        min_bars=cast(int, settings["min_bars"]),
        horizon=cast(int, settings["horizon"]),
        candidate_limit=cast(int, settings["candidate_limit"]),
        risk_trials=cast(Literal[1000, 2000, 3000], settings["risk_trials"]),
        initial_cash=cast(float, settings["initial_cash"]),
        commission=cast(float, settings["commission"]),
        min_commission=cast(float, settings["min_commission"]),
        stamp_tax=cast(float, settings["stamp_tax"]),
        slippage=cast(float, settings["slippage"]),
        execution=cast(Literal["next_open", "next_close"], settings["execution"]),
        position_mode="full",
        split_windows=None,
        common_split_metadata=None,
    )


def _research_settings(
    payload: Mapping[str, object],
    risk_trials: int,
) -> dict[str, object]:
    # 显式 None = 不限制（对照实验用）；键缺席 = 按默认 20 个交易日收口。
    raw_max_hold = payload.get("max_hold_days", DEFAULT_MAX_HOLD_DAYS)
    settings: dict[str, object] = {
        "max_hold_days": (
            None
            if raw_max_hold is None
            else _positive_int(raw_max_hold, DEFAULT_MAX_HOLD_DAYS, "最长持仓交易日")
        ),
        "reversal_multiple": _positive_float(payload.get("reversal_multiple"), 2.0, "反转阈值"),
        "atr_window": _positive_int(payload.get("atr_window"), 14, "ATR 周期"),
        "min_bars": _positive_int(payload.get("min_bars"), 5, "最短波段交易日"),
        "horizon": _positive_int(payload.get("horizon"), 10, "波段预测窗口"),
        "candidate_limit": CANDIDATE_LIMIT,
        "risk_trials": risk_trials,
        "initial_cash": 1_000_000.0,
        "commission": 0.0001,
        "min_commission": 0.1,
        "stamp_tax": 0.001,
        "slippage": 0.0,
        "execution": "next_open",
        "split_policy": {
            "method": "per_stock_trading_days",
            "train_ratio": 0.6,
            "validation_ratio": 0.2,
            "holdout_ratio": 0.2,
            "purge_trading_days": _positive_int(payload.get("horizon"), 10, "波段预测窗口"),
        },
    }
    return settings


def _market_fingerprint(adapter: BatchMarketDataAdapter, ref: StockRef) -> str:
    return frame_fingerprint(adapter.read_stock(ref))


#: 个股基准的口径版本。改动一字就意味着存量 ``benchmark_json`` 全部作废：
#: 早期版本用不复权收盘价算买入持有，漏掉全部分红，基准被系统性低估。版本号写进
#: 存下来的 benchmark 里，:meth:`MarketStrategyPoolService._prior_entries` 只把版本
#: 对得上的行算作"已有基准"，其余的在下次全市场重选时补算——不然旧的错数字会一直
#: 躺在那儿，改了代码页面上却看不出变化。
BENCHMARK_CALIBER_VERSION = "buy-hold-dividend-adjusted-1"

#: SQL 里判断 benchmark 是否属于当前口径的 LIKE 片段。``strategy_pool/store.py``
#: 的 ``_dump_json`` 用紧凑分隔符落盘，键名格式固定，靠字符串匹配可以避免把整个
#: JSON 列读回来（每张表几千行）。
_BENCHMARK_VERSION_LIKE = f'%"caliber_version":"{BENCHMARK_CALIBER_VERSION}"%'


def _symbol_corporate_actions(
    adapter: BatchMarketDataAdapter,
    ref: StockRef,
) -> tuple[CorporateAction, ...]:
    """该标的的分红送转事件；适配器不支持时返回空，退化成不复权口径。

    取不到事件就用不复权价：基准算不出来只是少一个参考数，不该把策略研究整个拖垮。
    ``getattr`` 探测是因为 ``BatchMarketDataAdapter`` 协议没声明这个方法——生产注入
    的 ``DuckDbMarketDataAdapter`` 有，测试替身没有。
    """

    reader = getattr(adapter, "read_corporate_actions", None)
    if not callable(reader):
        return ()
    return tuple(reader(ref))


def _symbol_benchmark(
    adapter: BatchMarketDataAdapter,
    ref: StockRef,
    config: IndividualResearchConfig,
    split_metadata: Mapping[str, object],
) -> dict[str, object] | None:
    """Summarize close-to-close buy-and-hold returns for the same research windows.

    价格先按分红送转事件校正：基准回答的是"一直拿着会怎样"，而持有者会收到现金
    分红。不复权口径会把这部分收益全部漏掉——浦发银行 2015~2026 名义 -41%，按
    持有者现金流算实际 +31%，用它当基准，跑输的策略会显示成跑赢。
    """

    try:
        frame = adapter.read_stock(ref)
        actions = _symbol_corporate_actions(adapter, ref)
        if actions:
            frame = mark_adjusted(frame, actions)
        return _summarize_symbol_benchmark(frame, config, split_metadata)
    except Exception:  # noqa: BLE001 - a missing benchmark must not discard a valid strategy
        logger.debug("计算个股基准失败: %s", ref.symbol, exc_info=True)
        return None


def _summarize_symbol_benchmark(
    frame: pd.DataFrame,
    config: IndividualResearchConfig,
    split_metadata: Mapping[str, object],
) -> dict[str, object] | None:
    if frame.empty or "date" not in frame or "close" not in frame:
        return None

    dates = pd.to_datetime(frame["date"], errors="coerce")
    closes = pd.to_numeric(frame["close"], errors="coerce").to_numpy(dtype=float)
    valid = dates.notna().to_numpy() & np.isfinite(closes) & (closes > 0)
    if int(valid.sum()) < 2:
        return None
    bars = pd.DataFrame(
        {
            "date": dates[valid].dt.date.to_numpy(),
            "close": closes[valid],
        }
    ).sort_values("date")

    windows = dict(config.split_windows or {})
    observed_windows = _mapping(split_metadata.get("observed_windows"))
    for phase in ("train", "validation", "holdout"):
        observed = _mapping(observed_windows.get(phase))
        start = _parse_date(observed.get("start_date"))
        end = _parse_date(observed.get("end_date"))
        if start is not None and end is not None:
            windows[phase] = (start, end)

    periods: dict[str, tuple[date, date]] = {
        "full_period": (config.start_date, config.end_date),
        **windows,
    }
    benchmark: dict[str, object] = {
        "label": "个股买入持有",
        "method": "同区间首尾收盘价持有，价格按分红送转校正（分红再投资口径），未计交易费用",
        "caliber_version": BENCHMARK_CALIBER_VERSION,
    }
    for phase, (start, end) in periods.items():
        selected = bars.loc[bars["date"].between(start, end)]
        if len(selected) < 2:
            benchmark[phase] = None
            continue
        prices = selected["close"].to_numpy(dtype=float)
        performance = curve_performance(prices / prices[0] * config.initial_cash)
        benchmark[phase] = {
            "start_date": selected["date"].iloc[0].isoformat(),
            "end_date": selected["date"].iloc[-1].isoformat(),
            "bars": len(selected),
            **{
                key: performance.get(key)
                for key in ("total_return", "annual_return", "sharpe", "max_drawdown")
            },
        }
    return benchmark


def _parse_date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _same_candidate(
    annual: Mapping[str, object] | None,
    sharpe: Mapping[str, object] | None,
) -> bool:
    return bool(
        annual
        and sharpe
        and annual.get("candidate_hash")
        and annual.get("candidate_hash") == sharpe.get("candidate_hash")
    )


def _fingerprint(settings: Mapping[str, object]) -> str:
    raw = json.dumps(dict(settings), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _symbol_from_payload(payload: Mapping[str, object]) -> tuple[str, str]:
    market = str(payload.get("market") or "").upper()
    code = str(payload.get("code") or "")
    if market not in {"SH", "SZ"} or len(code) != 6 or not code.isdigit():
        raise ValueError("单股重跑证券代码无效")
    return market, code


def _trial_count(value: object) -> int:
    if isinstance(value, bool) or value not in {1000, 2000, 3000}:
        raise ValueError("退出风控寻优组数只支持 1000、2000 或 3000")
    return int(value)


def _positive_int(value: object, default: int, label: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) <= 0:
        raise ValueError(f"{label}必须大于 0")
    return int(value)


def _positive_float(value: object, default: float, label: str) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) <= 0:
        raise ValueError(f"{label}必须大于 0")
    return float(value)


def _calendar_metadata(
    calendar: Sequence[date], windows: Mapping[str, tuple[date, date]], horizon: int
) -> dict[str, object]:
    return {
        "method": "所有证券共用 A 股已完成日线交易日历，按时间顺序切分 60/20/20",
        "train_ratio": 0.6,
        "validation_ratio": 0.2,
        "holdout_ratio": 0.2,
        "purge_trading_days": horizon,
        "calendar_days": len(calendar),
        "calendar_start": calendar[0].isoformat(),
        "calendar_end": calendar[-1].isoformat(),
        "holdout_policy": "留出段只复核最终策略，不参与选策和排序",
        **{
            phase: {
                "start": bounds[0].isoformat(),
                "end": bounds[1].isoformat(),
                "days": sum(bounds[0] <= day <= bounds[1] for day in calendar),
            }
            for phase, bounds in windows.items()
        },
    }


def _active_candidate(
    entry: Mapping[str, object] | None,
    objective: str,
) -> Mapping[str, object] | None:
    if entry is None:
        return None
    automatic = entry.get("automatic_candidates")
    overrides = entry.get("manual_overrides")
    automatic_map = automatic if isinstance(automatic, Mapping) else {}
    override_map = overrides if isinstance(overrides, Mapping) else {}
    candidate = override_map.get(objective) or automatic_map.get(objective)
    if isinstance(candidate, Mapping) and isinstance(candidate.get("candidate_ref"), str):
        candidate = automatic_map.get(candidate["candidate_ref"])
    return candidate if isinstance(candidate, Mapping) else None


def _parse_rule(raw: object) -> RuleGroup | None:
    if not isinstance(raw, Mapping):
        return None
    try:
        return RuleGroupRequest.model_validate(raw).to_model()
    except ValueError as exc:
        raise ValueError("策略规则格式无效") from exc


def _union_rule(buy: RuleGroup, sell: RuleGroup) -> RuleGroup:
    signals = tuple(dict.fromkeys((*buy.signals, *sell.signals)))
    conditions = tuple(dict.fromkeys((*buy.conditions, *sell.conditions)))
    required = tuple(dict.fromkeys((*buy.required_conditions, *sell.required_conditions)))
    if not signals and not conditions and not required:
        raise ValueError("策略规则为空")
    return RuleGroup(
        signals=signals,
        conditions=conditions,
        required_conditions=required,
        combine_mode="any",
    )


def _feature_ids(*rules: RuleGroup) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            value
            for rule in rules
            for condition in (*rule.conditions, *rule.required_conditions)
            for value in (condition.left_value, condition.right_value)
            if isinstance(value, str)
        )
    )


def _signal_row(
    ref: StockRef,
    candidate: Mapping[str, object],
    objective: str,
    as_of: str,
    signal_type: str,
) -> dict[str, object]:
    candidate_metrics = _mapping(candidate.get("metrics"))
    metrics = _mapping(candidate_metrics.get("validation"))
    holdout_metrics = _mapping(candidate_metrics.get("holdout"))
    full_period_metrics = _mapping(candidate_metrics.get("full_period"))
    return {
        "market": ref.market,
        "code": ref.code,
        "name": ref.name or "",
        "symbol": f"{ref.market}{ref.code}",
        "as_of_date": as_of,
        "signal_type": signal_type,
        "objective": objective,
        "total_return": _finite_metric(metrics.get("total_return")),
        "annual_return": _finite_metric(metrics.get("annual_return")),
        "full_period_return": _finite_metric(full_period_metrics.get("total_return")),
        "holdout_return": _finite_metric(holdout_metrics.get("total_return")),
        "sharpe": _finite_metric(metrics.get("sharpe")),
        "win_rate": _finite_metric(metrics.get("win_rate")),
        "payoff_ratio": _finite_metric(metrics.get("payoff_ratio")),
        "max_drawdown": _finite_metric(metrics.get("max_drawdown")),
        "validation_trades": _candidate_trade_count(metrics),
        "holdout_trades": _candidate_trade_count(holdout_metrics),
        "candidate_id": candidate.get("candidate_id"),
        "quality": candidate.get("quality"),
    }


def _candidate_trade_count(metrics: Mapping[str, object]) -> float | None:
    for key in ("closed_trades", "total_trades"):
        count = _finite_metric(metrics.get(key))
        if count is not None:
            return count
    return None


def _signal_state(
    ref: StockRef,
    state: str,
    candidate: Mapping[str, object] | None,
    objective: str,
    error: str | None = None,
) -> dict[str, object]:
    row = _signal_row(ref, candidate, objective, "", "") if candidate is not None else {}
    row.update({"status": state, "error": error})
    row.setdefault("market", ref.market)
    row.setdefault("code", ref.code)
    row.setdefault("name", ref.name or "")
    row.setdefault("symbol", f"{ref.market}{ref.code}")
    return row


def _signal_sort_key(row: Mapping[str, object], objective: str) -> tuple[float, float, str]:
    primary = _finite_metric(row.get(objective))
    secondary = _finite_metric(
        row.get("sharpe" if objective == "annual_return" else "annual_return")
    )
    return (
        -primary if primary is not None else float("inf"),
        -secondary if secondary is not None else float("inf"),
        str(row.get("symbol") or ""),
    )


def _status_order(state: str) -> int:
    return {"buy": 0, "buy_sell": 0, "sell": 1, "flat": 2, "no_strategy": 3}.get(state, 4)


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _finite_metric(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _portfolio_strategies(
    entries: Sequence[Mapping[str, object]],
    refs: Sequence[StockRef],
    objective: PoolObjective,
    *,
    require_validation_metric: bool = True,
) -> tuple[dict[str, dict[str, object]], dict[str, float]]:
    risk_fields = (
        "stop_loss_pct",
        "stop_loss_mode",
        "stop_loss_atr_multiplier",
        "stop_loss_cooldown_days",
        "take_profit_pct",
        "take_profit_mode",
        "take_profit_atr_multiplier",
        "take_profit_cooldown_days",
        "trailing_stop_pct",
        "trailing_stop_mode",
        "trailing_stop_atr_multiplier",
        "trailing_stop_cooldown_days",
        "atr_period",
    )
    entry_map = {(str(entry.get("market")), str(entry.get("code"))): entry for entry in entries}
    ranked: list[tuple[float, float, float, str, dict[str, object]]] = []
    for ref in refs:
        entry = entry_map.get((ref.market, ref.code))
        candidate = _active_candidate(entry, objective)
        if candidate is None or candidate.get("replayable") is False:
            continue
        buy_rule = _parse_rule(candidate.get("buy_rule"))
        sell_rule = _parse_rule(candidate.get("sell_rule"))
        if buy_rule is None or sell_rule is None:
            continue
        metrics = _mapping(_mapping(candidate.get("metrics")).get("validation"))
        primary = _finite_metric(metrics.get(objective))
        if primary is None and require_validation_metric:
            continue
        secondary_name = "sharpe" if objective == "annual_return" else "annual_return"
        secondary = _finite_metric(metrics.get(secondary_name))
        drawdown = _finite_metric(metrics.get("max_drawdown"))
        strategy_config = _mapping(candidate.get("config"))
        symbol_strategy: dict[str, object] = {
            "buy_rule": buy_rule,
            "sell_rule": sell_rule,
            "candidate_id": candidate.get("candidate_id"),
        }
        for key in risk_fields:
            if key in strategy_config:
                symbol_strategy[key] = strategy_config[key]
        symbol = ref.market + ref.code
        ranked.append(
            (
                -(primary if primary is not None else 0.0),
                -(secondary if secondary is not None else float("-inf")),
                abs(drawdown) if drawdown is not None else float("inf"),
                symbol,
                symbol_strategy,
            )
        )
    ranked.sort(key=lambda item: item[:4])
    strategies = {item[3]: item[4] for item in ranked}
    total = len(ranked)
    priorities = {item[3]: float(total - index) for index, item in enumerate(ranked)}
    return strategies, priorities


def _pool_portfolio_config(
    refs: Sequence[StockRef],
    strategies: Mapping[str, Mapping[str, object]],
    priorities: Mapping[str, float],
    *,
    start_date: date,
    end_date: date,
    max_positions: int = 5,
    initial_cash: float = 1_000_000.0,
) -> PortfolioBacktestConfig:
    return PortfolioBacktestConfig(
        vipdoc_path="",
        universe="codes",
        codes=tuple(
            f"{ref.market}:{ref.code}" for ref in refs if ref.market + ref.code in strategies
        ),
        instrument_types=("stock",),
        boards=(),
        selected_signals=(),
        combine_mode="any",
        minimum_matches=None,
        ranking_value="technical.rsi14",
        rank_order="desc",
        max_positions=max_positions,
        rebalance_frequency="daily",
        start_date=start_date,
        end_date=end_date,
        initial_cash=initial_cash,
        symbol_strategies=strategies,
        symbol_priority_scores=priorities,
    )


def _report(
    callback: PoolProgressCallback | None,
    completed: int,
    total: int,
    phase: str,
    symbol: str,
    skipped: int,
) -> None:
    if callback is not None:
        callback(
            completed,
            total,
            phase,
            {
                "overall_progress": completed / max(1, total),
                "completed_symbols": completed,
                "total_symbols": total,
                "current_symbol": symbol,
                "skipped_symbols": skipped,
            },
        )


__all__ = ["MarketStrategyPoolService"]
