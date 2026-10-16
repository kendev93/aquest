"""Bounded process evaluation of rule pairs using the ordinary stock backtester."""

from __future__ import annotations

import multiprocessing
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import Future, ProcessPoolExecutor, wait
from dataclasses import fields
from typing import Any

from aquest.indicators.cache import IndicatorCache
from aquest.market_data.adapter import DuckDbMarketDataAdapter
from aquest.market_data.models import StockRef
from aquest.market_data.store import DuckDbMarketDataStore
from aquest.portfolio_backtest.models import PortfolioBacktestConfig
from aquest.portfolio_backtest.service import PortfolioBacktestService
from aquest.screening.jobs import TaskCancelledError
from aquest.strategy_discovery.evaluation_budget import EvaluationWorkerBudget

RulePairJob = tuple[StockRef, dict[str, Any], dict[str, Any]]
RulePairMetrics = tuple[dict[str, object], dict[str, object]]
_WORKER_PORTFOLIO: PortfolioBacktestService | None = None


def config_payload(config: PortfolioBacktestConfig) -> dict[str, Any]:
    """Copy immutable mapping wrappers into a process-safe payload without changing fields."""

    def plain(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {key: plain(item) for key, item in value.items()}
        if isinstance(value, tuple):
            return tuple(plain(item) for item in value)
        return value

    return {field.name: plain(getattr(config, field.name)) for field in fields(config)}


def _initialize_validation_worker(database_path: str) -> None:
    global _WORKER_PORTFOLIO
    # 与发现寻优的 worker 同一做法：进程只服务一只股票的规则对初筛，读连接复用。
    # 逐次查询新建连接会让每次 run_stock 多付一趟连接开销。
    store = DuckDbMarketDataStore(database_path, read_only=True, pool_read_connections=True)
    adapter = DuckDbMarketDataAdapter(store, cache_stock_reads=True)
    cache_store = store if store.indicator_cache_backend == "sqlite" else None
    _WORKER_PORTFOLIO = PortfolioBacktestService(
        adapter=adapter, indicator_cache=IndicatorCache(cache_store, max_memory_entries=64)
    )


def _evaluate_rule_pair(job: RulePairJob) -> RulePairMetrics:
    from .service import _report_metrics

    if _WORKER_PORTFOLIO is None:
        raise RuntimeError("买卖规则验证进程尚未初始化")
    ref, train, validation = job
    return (
        _report_metrics(
            _WORKER_PORTFOLIO.run_stock(ref, PortfolioBacktestConfig(**train), metrics_only=True)
        ),
        _report_metrics(
            _WORKER_PORTFOLIO.run_stock(
                ref, PortfolioBacktestConfig(**validation), metrics_only=True
            )
        ),
    )


def evaluate_rule_pairs(
    database_path: str,
    jobs: Iterator[RulePairJob],
    *,
    budget: EvaluationWorkerBudget,
    requested_workers: int,
    abort_check: Callable[[], bool] | None,
) -> Iterator[RulePairMetrics]:
    """Keep at most one in-flight pair per leased worker and yield original pair order."""

    with budget.lease(requested_workers, abort_check) as workers:
        executor = ProcessPoolExecutor(
            max_workers=workers,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=_initialize_validation_worker,
            initargs=(database_path,),
        )
        pending: deque[Future[RulePairMetrics]] = deque()
        try:
            for _ in range(workers):
                job = next(jobs, None)
                if job is None:
                    break
                pending.append(executor.submit(_evaluate_rule_pair, job))
            while pending:
                future = pending[0]
                while not future.done():
                    if abort_check is not None and abort_check():
                        raise TaskCancelledError("买卖规则验证已取消")
                    wait([future], timeout=0.25)
                if abort_check is not None and abort_check():
                    raise TaskCancelledError("买卖规则验证已取消")
                pending.popleft()
                yield future.result()
                job = next(jobs, None)
                if job is not None:
                    pending.append(executor.submit(_evaluate_rule_pair, job))
        finally:
            for future in pending:
                future.cancel()
            executor.shutdown(wait=True, cancel_futures=True)
