"""Incremental full-universe research using the existing individual-stock pipeline."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import date
from threading import Lock, local
from typing import Literal, cast

from selector_app.backtest.models import BacktestConfig
from selector_app.backtest.service import BacktestService
from selector_app.concurrency import visible_cpu_count
from selector_app.formulas.registry import FORMULA_REGISTRY
from selector_app.indicators.cache import frame_fingerprint
from selector_app.individual_research.models import IndividualResearchConfig
from selector_app.individual_research.service import IndividualStockResearchService
from selector_app.market_data.adapter import BatchMarketDataAdapter
from selector_app.market_data.models import StockRef
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.portfolio_backtest.models import PortfolioBacktestConfig
from selector_app.portfolio_backtest.service import PortfolioBacktestService
from selector_app.screening.engine import evaluate_rule_group
from selector_app.screening.jobs import TaskCancelledError
from selector_app.screening.rules import RuleGroup
from selector_app.strategy_discovery.catalog import default_indicator_catalog
from selector_app.web.schemas.screening import RuleGroupRequest

from .resources import (
    cpu_bounded_worker_count,
    estimate_ticker_working_set_bytes,
    get_cpu_load_average_1m,
    memory_bounded_worker_count,
    memory_status,
    pool_worker_cap,
)
from .selection import common_calendar_windows, summarize_objective_winners
from .store import DuckDbStrategyPoolStore

PoolObjective = Literal["annual_return", "sharpe"]
PoolProgressCallback = Callable[[int, int, str, Mapping[str, object] | None], None]
PoolEntryWriter = Callable[[Mapping[str, object]], None]
MARKET_POOL_ALGORITHM_VERSION = "market-pool-v2"
POOL_PROGRESS_EMIT_INTERVAL_SECONDS = 1.5
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

    def __call__(self, current: int, total: int) -> None:
        fraction = min(1.0, max(0.0, current / total if total else 1.0))
        if self._coordinator is not None:
            self._coordinator.ticker_progress(
                self._index,
                self._symbol,
                fraction,
                self._phase,
                self._phase_label,
                name=self._name,
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
                    "phase_label": self._phase_label,
                },
            )

    def report_detail(self, details: Mapping[str, object]) -> None:
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
    ) -> None:
        with self._lock:
            self._memory_worker_limit = memory_worker_limit
            self._available_memory_mb = (
                max(0, available_bytes // (1024**2)) if available_bytes is not None else None
            )
            self._cpu_worker_limit = cpu_worker_limit
            self._cpu_load_average_1m = cpu_load_average_1m
            self._visible_cpus = visible_cpus

    def ticker_progress(
        self,
        index: int,
        symbol: str,
        fraction: float,
        phase: str,
        phase_label: str,
        *,
        name: str = "",
    ) -> None:
        with self._lock:
            self._fractions[index] = fraction
            self._active_symbols[index] = {
                "symbol": symbol,
                "name": name,
                "progress": fraction,
                "phase": phase,
                "phase_label": phase_label,
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
                        dict(progress)
                        for _, progress in sorted(self._active_symbols.items())
                    ],
                    "maximum_parallelism": self._maximum_workers,
                    "memory_worker_limit": self._memory_worker_limit,
                    "available_memory_mb": self._available_memory_mb,
                    "cpu_worker_limit": self._cpu_worker_limit,
                    "cpu_load_average_1m": self._cpu_load_average_1m,
                    "visible_cpus": self._visible_cpus,
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
                        dict(progress)
                        for _, progress in sorted(self._active_symbols.items())
                    ],
                    "maximum_parallelism": self._maximum_workers,
                    "memory_worker_limit": self._memory_worker_limit,
                    "available_memory_mb": self._available_memory_mb,
                    "cpu_worker_limit": self._cpu_worker_limit,
                    "cpu_load_average_1m": self._cpu_load_average_1m,
                    "visible_cpus": self._visible_cpus,
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
    ) -> None:
        self._store = store
        self._adapter = adapter
        self._individual_service = individual_service
        self._backtest_service = backtest_service
        self._pool_store = pool_store
        self._portfolio_service = portfolio_service
        self._individual_service_factory = individual_service_factory
        self._research_local = local()
        self._max_pool_workers = (
            1
            if individual_service_factory is None
            else pool_worker_cap()
            if max_pool_workers is None
            else max_pool_workers
        )
        if not 1 <= self._max_pool_workers <= 8:
            raise ValueError("全市场策略池最大并发数必须在 1 到 8 之间")

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
        clear_stock_cache = getattr(self._adapter, "clear_thread_stock_cache", None)
        if callable(clear_stock_cache):
            clear_stock_cache()
        result = self._individual_service_for_thread().run(config, progress, abort_check)
        result_market_fingerprint = result.get("market_fingerprint")
        if not isinstance(result_market_fingerprint, str) or not result_market_fingerprint:
            result_market_fingerprint = previous_market_fingerprint or _market_fingerprint(
                self._adapter, ref
            )
        if previous_market_fingerprint and result_market_fingerprint != previous_market_fingerprint:
            raise ValueError("研究期间本地行情发生变化，请重新运行该股票")
        winners = summarize_objective_winners(result)
        annual_candidate = winners["annual_return"]
        sharpe_candidate = winners["sharpe"]
        if mode == "full" and _same_candidate(annual_candidate, sharpe_candidate):
            sharpe_candidate = {
                "candidate_ref": "annual_return",
                "candidate_id": annual_candidate.get("candidate_id")
                if annual_candidate is not None
                else None,
            }
        record: dict[str, object] = {
            "market": ref.market,
            "code": ref.code,
            "name": ref.name or "",
            "annual_candidate": annual_candidate,
            "sharpe_candidate": sharpe_candidate,
            "data_fingerprint": str(result.get("data_fingerprint") or ""),
            "market_fingerprint": result_market_fingerprint,
            "config_fingerprint": config_fingerprint,
            "source_job_id": job_id,
        }
        single_result = None
        if mode == "single":
            single_result = {
                **record,
                "split_metadata": result.get("split_metadata"),
                "date_range": result.get("date_range"),
                "condition_candidates": result.get("condition_candidates"),
                "risk_trials_per_rule_set": result.get("risk_trials_per_rule_set"),
            }
        return record, single_result

    def _run_full_research(
        self,
        refs: list[StockRef],
        *,
        calendar: Sequence[date],
        windows: Mapping[str, tuple[date, date]],
        split_metadata: Mapping[str, object],
        settings: Mapping[str, object],
        value_ids: Sequence[str],
        signal_ids: Sequence[str],
        config_fingerprint: str,
        prior_entries: Mapping[tuple[str, str], tuple[str, str, str]],
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
        active: dict[
            Future[tuple[dict[str, object], dict[str, object] | None]],
            tuple[int, StockRef],
        ] = {}
        with ThreadPoolExecutor(
            max_workers=self._max_pool_workers,
            thread_name_prefix="strategy-pool-symbol",
        ) as executor:
            while next_index < total or active:
                if abort_check is not None and abort_check():
                    for future in active:
                        future.cancel()
                    raise TaskCancelledError("全市场策略池研究已取消")

                while next_index < total:
                    ref = refs[next_index]
                    index = next_index + 1
                    symbol = f"{ref.market}:{ref.code}"
                    prior = prior_entries.get((ref.market, ref.code))
                    market_fingerprint = ""
                    if (
                        prior is not None
                        and prior[1] == config_fingerprint
                        and prior[2] in {"ready", "partial", "no_strategy"}
                    ):
                        market_fingerprint = _market_fingerprint(self._adapter, ref)
                    if self._can_reuse(prior, market_fingerprint, config_fingerprint):
                        skipped += 1
                        completed += 1
                        coordinator.finish(index, symbol, "复用已完成标的", skipped=True)
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
                    memory_worker_limit = memory_bounded_worker_count(
                        status=memory,
                        estimated_worker_bytes=estimated_bytes,
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
                    target_workers = min(memory_worker_limit, cpu_worker_limit)
                    coordinator.set_resource_budget(
                        memory_worker_limit=memory_worker_limit,
                        available_bytes=memory.available_bytes,
                        cpu_worker_limit=cpu_worker_limit,
                        cpu_load_average_1m=cpu_load_average,
                        visible_cpus=visible_cpus,
                    )
                    if len(active) >= target_workers:
                        break

                    config = _individual_config(
                        ref,
                        calendar[0],
                        calendar[-1],
                        windows,
                        split_metadata,
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
                    future = executor.submit(
                        self._research_symbol,
                        ref,
                        config,
                        job_id=job_id,
                        mode="full",
                        config_fingerprint=config_fingerprint,
                        previous_market_fingerprint=market_fingerprint,
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
            return self._scan_signals(
                payload,
                job_id=job_id,
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
        horizon = _positive_int(payload.get("horizon"), 10, "波段预测窗口")
        windows = common_calendar_windows(calendar, purge_days=horizon)
        metadata = _calendar_metadata(calendar, windows, horizon)
        if mode == "portfolio":
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
        settings = _research_settings(payload, windows, risk_trials)
        settings["value_ids"] = list(catalog.value_ids)
        settings["signal_ids"] = list(catalog.signal_ids)
        formula_ids = {
            value_id.split(".", 1)[0] for value_id in (*catalog.value_ids, *catalog.signal_ids)
        }
        settings["formula_versions"] = {
            formula_id: FORMULA_REGISTRY.formula(formula_id).cache_version
            for formula_id in sorted(formula_ids)
        }
        settings["algorithm_version"] = MARKET_POOL_ALGORITHM_VERSION
        config_fingerprint = _fingerprint(settings)
        prior_entries = self._prior_entries() if mode == "full" else {}
        started = date.today()
        single_result: dict[str, object] | None = None
        completed = 0
        skipped = 0
        failed = 0
        errors: list[dict[str, str]] = []
        total = len(refs)
        total_work = total + (2 if mode == "full" else 0)
        if mode == "full":
            completed, skipped, failed, errors = self._run_full_research(
                refs,
                calendar=calendar,
                windows=windows,
                split_metadata=metadata,
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
                windows,
                metadata,
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
                completed = 1
                _report(progress_callback, completed, total_work, "保存标的候选", symbol, 0)
            except TaskCancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - expose a safe single-symbol failure
                raise ValueError(f"{symbol} 个股研究失败：{str(exc)[:500]}") from exc

        holdout_portfolios: dict[str, object] = {}
        if mode == "full":
            objectives: tuple[PoolObjective, ...] = ("annual_return", "sharpe")
            for offset, objective in enumerate(objectives, start=1):
                if abort_check is not None and abort_check():
                    raise TaskCancelledError("全市场策略池研究已取消")
                holdout_portfolios[objective] = self._portfolio_summary(
                    objective,
                    windows,
                    calendar[-1],
                    progress_callback=progress_callback,
                    progress_index=total + offset - 1,
                    progress_total=total_work,
                    abort_check=abort_check,
                )
                _report(
                    progress_callback,
                    total + offset,
                    total_work,
                    "完成组合留出段验证",
                    objective,
                    skipped,
                )

        return {
            "mode": mode,
            "total_symbols": total,
            "completed_symbols": completed,
            "skipped_symbols": skipped,
            "failed_symbols": failed,
            "errors": errors[:200],
            "common_calendar": metadata,
            "single_candidate": single_result,
            "holdout_portfolios": holdout_portfolios,
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

    def _current_portfolio_snapshot(
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
            return {"available": False, "reason": "组合回测服务未配置"}
        refs = self._list_refs(None)
        strategies, priorities = _portfolio_strategies(
            self._pool_store.list_entries(),
            refs,
            objective,
        )
        if not strategies:
            return {"available": False, "reason": "没有可用策略"}
        config = _pool_portfolio_config(
            refs,
            strategies,
            priorities,
            start_date=windows["holdout"][0],
            end_date=latest_date,
        )

        def report_progress(current: int, total: int) -> None:
            if abort_check is not None and abort_check():
                raise TaskCancelledError("模拟组合状态更新已取消")
            if progress_callback is None:
                return
            ratio = current / total if total else 1.0
            progress_callback(
                progress_index + (1 if ratio >= 1.0 else 0),
                progress_total,
                "更新模拟组合持仓",
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
        except Exception as exc:  # noqa: BLE001 - retain buy/sell rule scan results
            return {
                "available": False,
                "reason": str(exc).strip()[:500] or "模拟组合状态计算失败",
                "holdings": [],
                "risk_exit_signals": [],
            }
        as_of = str((report.current_state or {}).get("as_of") or latest_date.isoformat())
        risk_exits = [
            dict(trade)
            for trade in report.trades
            if trade.get("date") == as_of and trade.get("risk_trigger") is not None
        ]
        return {
            "available": True,
            "as_of_date": as_of,
            "current_state": dict(report.current_state) if report.current_state else None,
            "risk_exit_signals": risk_exits,
            "data_fingerprint": report.data_fingerprint,
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
        progress_callback: PoolProgressCallback | None,
        abort_check: Callable[[], bool] | None,
    ) -> dict[str, object]:
        objective = str(payload.get("objective") or "annual_return")
        if objective not in {"annual_return", "sharpe"}:
            raise ValueError("策略池目标必须是 annual_return 或 sharpe")
        if self._backtest_service is None or self._pool_store is None:
            raise ValueError("策略池信号扫描服务未配置")
        refs = self._list_refs(None)
        raw_entries = self._pool_store.list_entries()
        entries = {(str(item.get("market")), str(item.get("code"))): item for item in raw_entries}
        buy_signals: list[dict[str, object]] = []
        sell_signals: list[dict[str, object]] = []
        states: list[dict[str, object]] = []
        failed = 0
        total_work = len(refs) + 1
        latest_date: date | None = None
        for index, ref in enumerate(refs, start=1):
            if abort_check is not None and abort_check():
                raise TaskCancelledError("策略池信号扫描已取消")
            symbol = f"{ref.market}{ref.code}"
            entry = entries.get((ref.market, ref.code))
            active = _active_candidate(entry, objective)
            if active is None:
                states.append(_signal_state(ref, "no_strategy", None, objective))
                _report(progress_callback, index, total_work, "无可用策略", symbol, 0)
                continue
            try:
                buy_rule = _parse_rule(active.get("buy_rule"))
                sell_rule = _parse_rule(active.get("sell_rule"))
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
                    buy_signals.append(_signal_row(ref, active, objective, as_of, "buy"))
                if sell:
                    sell_signals.append(_signal_row(ref, active, objective, as_of, "sell"))
                state = "buy_sell" if buy and sell else "buy" if buy else "sell" if sell else "flat"
                states.append(_signal_state(ref, state, active, objective))
            except Exception as exc:  # noqa: BLE001 - one unavailable formula cannot hide other signals
                failed += 1
                states.append(_signal_state(ref, "error", active, objective, str(exc)[:200]))
            _report(progress_callback, index, total_work, "扫描当天买卖信号", symbol, 0)
        buy_signals.sort(key=lambda row: _signal_sort_key(row, objective))
        sell_signals.sort(key=lambda row: _signal_sort_key(row, objective))
        model_portfolio: dict[str, object] = {
            "available": False,
            "holdings": [],
            "risk_exit_signals": [],
            "reason": "没有足够历史数据或可用策略",
        }
        if (
            latest_date is not None
            and self._portfolio_service is not None
            and self._pool_store is not None
        ):
            try:
                horizon = _positive_int(payload.get("horizon"), 10, "波段预测窗口")
                windows = common_calendar_windows(self._common_calendar(), purge_days=horizon)
                model_portfolio = self._current_portfolio_snapshot(
                    cast(PoolObjective, objective),
                    windows,
                    latest_date,
                    progress_callback=progress_callback,
                    progress_index=len(refs),
                    progress_total=total_work,
                    abort_check=abort_check,
                )
            except ValueError as exc:
                model_portfolio = {
                    "available": False,
                    "holdings": [],
                    "risk_exit_signals": [],
                    "reason": str(exc),
                }
            except TaskCancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - direct rule signals are still useful
                model_portfolio = {
                    "available": False,
                    "holdings": [],
                    "risk_exit_signals": [],
                    "reason": str(exc).strip()[:500] or "模拟组合状态计算失败",
                }
        current_state = _mapping(model_portfolio.get("current_state"))
        current_holdings = current_state.get("holdings")
        held_symbols = (
            {
                f"{item.get('market', '')}{item.get('code', '')}"
                for item in current_holdings
                if isinstance(item, Mapping)
            }
            if isinstance(current_holdings, (list, tuple))
            else set()
        )
        for item in states:
            if item.get("status") == "flat" and item.get("symbol") in held_symbols:
                item["status"] = "held"
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
            "as_of_date": latest_date.isoformat() if latest_date else None,
            "buy_signals": buy_signals,
            "sell_signals": sell_signals,
            "states": states,
            "failed_symbols": failed,
            "completed_symbols": len(refs) - failed,
            "skipped_symbols": 0,
            "total_symbols": len(refs),
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

    def _prior_entries(self) -> dict[tuple[str, str], tuple[str, str, str]]:
        with self._store.read_session() as connection:
            rows = connection.execute(
                """
                SELECT market, code, market_fingerprint, config_fingerprint, status
                FROM strategy_pool_entries
                """
            ).fetchall()
        return {
            (str(row[0]), str(row[1])): (str(row[2] or ""), str(row[3] or ""), str(row[4] or ""))
            for row in rows
        }

    @staticmethod
    def _can_reuse(
        prior: tuple[str, str, str] | None,
        market_fingerprint: str,
        config_fingerprint: str,
    ) -> bool:
        return bool(
            prior
            and prior[0]
            and prior[0] == market_fingerprint
            and prior[1] == config_fingerprint
            and prior[2] in {"ready", "partial", "no_strategy"}
        )


def _individual_config(
    ref: StockRef,
    start_date: date,
    end_date: date,
    windows: Mapping[str, tuple[date, date]],
    metadata: Mapping[str, object],
    value_ids: Sequence[str],
    signal_ids: Sequence[str],
    settings: Mapping[str, object],
) -> IndividualResearchConfig:
    return IndividualResearchConfig(
        market=ref.market,
        code=ref.code,
        name=ref.name or "",
        start_date=start_date,
        end_date=end_date,
        value_ids=tuple(value_ids),
        signal_ids=tuple(signal_ids),
        reversal_multiple=cast(float, settings["reversal_multiple"]),
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
        split_windows=windows,
        common_split_metadata=metadata,
    )


def _research_settings(
    payload: Mapping[str, object],
    windows: Mapping[str, tuple[date, date]],
    risk_trials: int,
) -> dict[str, object]:
    settings = {
        "reversal_multiple": _positive_float(payload.get("reversal_multiple"), 3.0, "反转阈值"),
        "atr_window": _positive_int(payload.get("atr_window"), 14, "ATR 周期"),
        "min_bars": _positive_int(payload.get("min_bars"), 5, "最短波段交易日"),
        "horizon": _positive_int(payload.get("horizon"), 10, "波段预测窗口"),
        "candidate_limit": 10,
        "risk_trials": risk_trials,
        "initial_cash": 1_000_000.0,
        "commission": 0.0001,
        "min_commission": 0.1,
        "stamp_tax": 0.001,
        "slippage": 0.0,
        "execution": "next_open",
        "split_windows": {
            phase: [bounds[0].isoformat(), bounds[1].isoformat()]
            for phase, bounds in windows.items()
        },
    }
    return settings


def _market_fingerprint(adapter: BatchMarketDataAdapter, ref: StockRef) -> str:
    return frame_fingerprint(adapter.read_stock(ref))


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
    metrics = _mapping(_mapping(candidate.get("metrics")).get("validation"))
    return {
        "market": ref.market,
        "code": ref.code,
        "name": ref.name or "",
        "symbol": f"{ref.market}{ref.code}",
        "as_of_date": as_of,
        "signal_type": signal_type,
        "objective": objective,
        "annual_return": _finite_metric(metrics.get("annual_return")),
        "sharpe": _finite_metric(metrics.get("sharpe")),
        "win_rate": _finite_metric(metrics.get("win_rate")),
        "payoff_ratio": _finite_metric(metrics.get("payoff_ratio")),
        "candidate_id": candidate.get("candidate_id"),
        "quality": candidate.get("quality"),
    }


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
    return {"buy": 0, "buy_sell": 0, "sell": 1, "held": 2, "flat": 3, "no_strategy": 4}.get(
        state, 5
    )


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
        if primary is None:
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
                -primary,
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
        max_positions=5,
        rebalance_frequency="daily",
        start_date=start_date,
        end_date=end_date,
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
