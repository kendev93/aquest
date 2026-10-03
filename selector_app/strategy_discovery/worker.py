"""Independent strategy executor coordinated by the backend worker API."""

from __future__ import annotations

import logging
import signal
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread, get_ident
from typing import Literal, Protocol, cast

from selector_app.backtest.service import BacktestService
from selector_app.indicators.cache import IndicatorCache
from selector_app.individual_research.service import IndividualStockResearchService
from selector_app.market_data.adapter import DuckDbMarketDataAdapter
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.portfolio_backtest.service import PortfolioBacktestService
from selector_app.strategy_pool.resources import (
    pool_evaluation_budget_capacity,
    pool_evaluation_worker_count,
)
from selector_app.strategy_pool.service import MarketStrategyPoolService
from selector_app.strategy_pool.store import DuckDbStrategyPoolStore
from selector_app.strategy_pool.tracking_runner import PoolTrackingRunner

from .evaluation_budget import EvaluationWorkerBudget
from .job_backend import completion_state, decorate_result
from .job_store import is_job_operation
from .models import DiscoveryRequest
from .refinement import discovery_request_from_dict
from .replay import ReplayLeaderboard
from .replay_service import ReplayProgressCallback
from .service import ProgressCallback, StrategyDiscoveryService
from .snapshot import DiscoverySnapshotCache
from .worker_api_client import HttpStrategyJobClient, TransientWorkerApiError

logger = logging.getLogger(__name__)
DEFAULT_WORKER_POLL_INTERVAL_SECONDS = 0.5
DEFAULT_STALE_AFTER_SECONDS = 120.0
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 15.0


class StrategyDiscoveryServiceLike(Protocol):
    def run(
        self,
        request: DiscoveryRequest,
        *,
        progress_callback: ProgressCallback | None = None,
        abort_check: Callable[[], bool] | None = None,
    ) -> dict[str, object]: ...

    def refine(
        self,
        experiment: Mapping[str, object],
        parent_candidate_hash: str,
        *,
        max_trials: int | None = None,
        seed: int | None = None,
        radius: Literal["medium"] = "medium",
        progress_callback: ProgressCallback | None = None,
        abort_check: Callable[[], bool] | None = None,
    ) -> dict[str, object]: ...

    def replay(
        self,
        experiment: Mapping[str, object],
        leaderboard: ReplayLeaderboard,
        *,
        progress_callback: ReplayProgressCallback | None = None,
        abort_check: Callable[[], bool] | None = None,
    ) -> dict[str, object]: ...


class StrategyJobClient(Protocol):
    def recover_stale_running(self, *, stale_after_seconds: float) -> list[str]: ...

    def claim_next(self) -> str | None: ...

    def read_request(self, job_id: str) -> dict[str, object] | None: ...

    def read_state(self, job_id: str) -> dict[str, object] | None: ...

    def is_cancel_requested(self, job_id: str) -> bool: ...

    def update(
        self,
        job_id: str,
        fields: Mapping[str, object] | None = None,
        **updates: object,
    ) -> None: ...

    def write_result(self, job_id: str, result: Mapping[str, object]) -> None: ...


class StrategyDiscoveryWorker:
    """Claim and execute at most one strategy job at a time."""

    def __init__(
        self,
        job_store: StrategyJobClient,
        service: StrategyDiscoveryServiceLike,
        *,
        service_factory: Callable[[int | None], StrategyDiscoveryServiceLike] | None = None,
        strategy_pool_service: MarketStrategyPoolService | None = None,
        pool_tracking_service: PoolTrackingRunner | None = None,
        poll_interval_seconds: float = DEFAULT_WORKER_POLL_INTERVAL_SECONDS,
        stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("策略 worker 轮询间隔必须大于 0")
        if stale_after_seconds <= poll_interval_seconds:
            raise ValueError("策略 worker stale 时间必须大于轮询间隔")
        self._job_store = job_store
        self._service = service
        self._service_factory = service_factory
        self._strategy_pool_service = strategy_pool_service
        self._pool_tracking_service = pool_tracking_service
        self._poll_interval_seconds = poll_interval_seconds
        self._stale_after_seconds = stale_after_seconds
        self._heartbeat_interval_seconds = min(
            DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
            stale_after_seconds / 3,
        )

    def run_once(self) -> str | None:
        self._job_store.recover_stale_running(stale_after_seconds=self._stale_after_seconds)
        job_id = self._job_store.claim_next()
        if job_id is None:
            return None
        try:
            if self._job_store.is_cancel_requested(job_id):
                self._mark_cancelled(job_id)
                return job_id
            request = self._job_store.read_request(job_id)
            if request is None:
                raise ValueError("策略任务请求文件不存在")
            operation = request.get("operation")
            if not is_job_operation(operation):
                raise ValueError("策略任务类型无效")
            started_at = datetime.now(timezone.utc).isoformat()
            state = self._job_store.read_state(job_id) or {}
            raw_worker_count = state.get("worker_count")
            worker_count = (
                int(raw_worker_count)
                if isinstance(raw_worker_count, (int, float))
                and not isinstance(raw_worker_count, bool)
                else None
            )
            service = (
                self._service_factory(worker_count)
                if self._service_factory is not None
                else self._service
            )
            result = self._dispatch_with_heartbeat(job_id, request, service=service)
            normalized = decorate_result(
                job_id,
                operation,
                result,
                started_at=started_at,
            )
            completion = completion_state(operation, normalized)
            self._job_store.update(job_id, {"status": "running", **completion})
            self._job_store.write_result(
                job_id,
                normalized,
            )
        except Exception as exc:  # noqa: BLE001 - worker must keep serving later jobs
            if self._job_store.is_cancel_requested(job_id):
                self._mark_cancelled(job_id)
            else:
                logger.exception("策略 worker 执行任务失败: %s", job_id)
                self._job_store.update(
                    job_id,
                    status="failed",
                    phase="failed",
                    error=str(exc).strip()[:500] or "策略 worker 执行失败",
                    error_code=_error_code(exc),
                )
        return job_id

    def _dispatch_with_heartbeat(
        self,
        job_id: str,
        request: Mapping[str, object],
        *,
        service: StrategyDiscoveryServiceLike,
    ) -> dict[str, object]:
        """Keep the backend lease alive while a candidate batch blocks progress callbacks."""

        stop_event = Event()
        heartbeat_thread = Thread(
            target=self._heartbeat_loop,
            args=(job_id, stop_event),
            name=f"strategy-heartbeat-{job_id[:8]}",
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            return self._dispatch(job_id, request, service=service)
        finally:
            stop_event.set()
            heartbeat_thread.join()

    def _heartbeat_loop(self, job_id: str, stop_event: Event) -> None:
        while not stop_event.wait(self._heartbeat_interval_seconds):
            try:
                # The HTTP client implements this as a lightweight heartbeat plus
                # cancellation check. It runs independently of long process-pool batches.
                self._job_store.is_cancel_requested(job_id)
            except Exception as exc:  # noqa: BLE001 - progress callbacks also renew the lease
                logger.warning("策略 worker 定时心跳失败（任务 %s）: %s", job_id, exc)

    def run_forever(self, *, stop_event: Event | None = None) -> None:
        """Poll for work, surviving failures in the polling path itself.

        ``run_once`` only guards the job it is executing. A transient failure in
        ``recover_stale_running`` or ``claim_next`` escapes it - the backend
        restarting while it still holds the DuckDB write lock is the usual one -
        and used to take the whole worker down. Docker's restart policy brings
        the container back, but until it does nothing claims any job, and a
        portfolio backtest that is merely waiting reads to the user as
        "backtest failed, check your data". A transient error is no reason to
        stop serving the next job, so it is logged and the loop goes on.
        """

        while stop_event is None or not stop_event.is_set():
            try:
                if self.run_once() is None:
                    time.sleep(self._poll_interval_seconds)
            except Exception:  # noqa: BLE001 - one poll failing must not end the loop
                logger.exception("策略 worker 领取任务失败，稍后重试")
                time.sleep(self._poll_interval_seconds)

    def _dispatch(
        self,
        job_id: str,
        request: Mapping[str, object],
        *,
        service: StrategyDiscoveryServiceLike | None = None,
    ) -> dict[str, object]:
        selected_service = service or self._service
        operation = request.get("operation")
        payload = request.get("payload")
        if not isinstance(payload, Mapping):
            raise ValueError("策略任务 payload 格式无效")

        def abort_check() -> bool:
            return self._job_store.is_cancel_requested(job_id)

        last_progress_warning_at = 0.0

        def progress_callback(
            current: int,
            total: int,
            phase: str,
            metadata: Mapping[str, object] | None = None,
        ) -> None:
            nonlocal last_progress_warning_at
            details = dict(metadata or {})
            raw_progress = details.get("overall_progress")
            progress = (
                float(raw_progress)
                if isinstance(raw_progress, (int, float))
                else current / total
                if total > 0
                else 1.0
            )
            try:
                self._job_store.update(
                    job_id,
                    phase=phase,
                    progress=progress,
                    total_candidates=total,
                    total_scanned=current,
                    heartbeat_at=time.time(),
                    metadata=details,
                )
            except TransientWorkerApiError as exc:
                now = time.monotonic()
                if now - last_progress_warning_at >= 30.0:
                    logger.warning(
                        "策略 worker 进度暂时无法上报，计算继续并在下一次进度更新时重试: %s",
                        exc,
                    )
                    last_progress_warning_at = now

        if operation in {"discovery", "risk_optimization"}:
            raw_request = payload.get("request")
            if not isinstance(raw_request, Mapping):
                raise ValueError("策略实验请求内容无效")
            discovery_request = discovery_request_from_dict(raw_request)
            result = dict(
                selected_service.run(
                    discovery_request,
                    progress_callback=progress_callback,
                    abort_check=abort_check,
                )
            )
            if operation == "risk_optimization":
                result["mode"] = "risk_optimization"
            return result
        if operation == "refinement":
            experiment = _mapping(payload.get("experiment"), "父实验")
            parent_hash = _required_string(payload.get("parent_candidate_hash"), "父候选 hash")
            return dict(
                selected_service.refine(
                    experiment,
                    parent_hash,
                    max_trials=_optional_int(payload.get("max_trials")),
                    seed=_optional_int(payload.get("seed")),
                    radius=cast(Literal["medium"], str(payload.get("radius") or "medium")),
                    progress_callback=progress_callback,
                    abort_check=abort_check,
                )
            )
        if operation == "replay":
            experiment = _mapping(payload.get("experiment"), "复核实验")
            leaderboard = str(payload.get("leaderboard") or "")
            if leaderboard not in {"sharpe", "return"}:
                raise ValueError("策略复核榜单类型无效")
            return dict(
                selected_service.replay(
                    experiment,
                    cast(ReplayLeaderboard, leaderboard),
                    progress_callback=progress_callback,
                    abort_check=abort_check,
                )
            )
        if operation == "market_strategy_pool_tracking":
            if self._pool_tracking_service is None:
                raise ValueError("策略池跟踪服务未配置")
            writer = getattr(self._job_store, "write_pool_tracking", None)
            if not callable(writer):
                raise ValueError("策略 worker 不支持保存跟踪账户")
            tracking_writer = writer
            return self._pool_tracking_service.run(
                payload,
                progress=progress_callback,
                abort_check=abort_check,
                writer=lambda snapshot: tracking_writer(job_id, snapshot),
            )
        if operation == "market_strategy_pool":
            if self._strategy_pool_service is None:
                raise ValueError("全市场策略池服务未配置")
            writer = getattr(self._job_store, "write_pool_entry", None)
            if not callable(writer):
                raise ValueError("策略 worker 不支持逐股保存策略池检查点")
            return self._strategy_pool_service.run(
                payload,
                job_id=job_id,
                progress_callback=progress_callback,
                abort_check=abort_check,
                entry_writer=lambda entry: writer(job_id, entry),
            )
        raise ValueError("策略任务类型无效")

    def _mark_cancelled(self, job_id: str) -> None:
        self._job_store.update(
            job_id,
            status="cancelled",
            phase="cancelled",
            error="策略实验已取消",
            error_code="cancelled",
        )


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label}内容无效")
    return value


def _required_string(value: object, label: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{label}不能为空")
    return result


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("策略任务整数参数无效")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("策略任务整数参数无效") from exc


def _error_code(exc: Exception) -> str:
    message = str(exc).lower()
    if "resource_budget_exceeded" in message or "内存预算" in message:
        return "resource_budget_exceeded"
    if "取消" in str(exc):
        return "cancelled"
    return "worker_failed"


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    store = DuckDbMarketDataStore(read_only=True)
    market_adapter = DuckDbMarketDataAdapter(store, cache_stock_reads=True)
    stop_event = Event()

    def stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with HttpStrategyJobClient.from_env(
        operations=[
            "discovery",
            "risk_optimization",
            "refinement",
            "replay",
            "market_strategy_pool",
        ]
    ) as job_client:
        with TemporaryDirectory(prefix="indicator-lab-worker-snapshots-") as cache_root:
            snapshot_cache = DiscoverySnapshotCache(cache_root)
            strategy_service = StrategyDiscoveryService(
                store=store,
                market_data_adapter=market_adapter,
                evaluation_workers=1,
                persist_indicator_cache=False,
                discovery_snapshot_cache=snapshot_cache,
            )
            cache_store = store if store.indicator_cache_backend == "sqlite" else None
            # The read-only worker can share the companion SQLite cache safely.
            # A DuckDB cache would require writes through this read-only handle,
            # so fall back to in-memory calculation for that opt-in backend.
            pool_indicator_cache = IndicatorCache(cache_store, max_memory_entries=64)
            pool_backtest_service = BacktestService(
                adapter=market_adapter,
                indicator_cache=pool_indicator_cache,
            )
            pool_portfolio_service = PortfolioBacktestService(
                adapter=market_adapter,
                indicator_cache=pool_indicator_cache,
            )

            pool_evaluation_budget = EvaluationWorkerBudget(pool_evaluation_budget_capacity())

            def pool_individual_service_factory() -> IndividualStockResearchService:
                per_thread_snapshots = DiscoverySnapshotCache(
                    Path(cache_root) / f"pool-worker-{get_ident()}"
                )
                per_thread_strategy_service = StrategyDiscoveryService(
                    store=store,
                    market_data_adapter=market_adapter,
                    evaluation_workers=pool_evaluation_worker_count(),
                    evaluation_worker_budget=pool_evaluation_budget,
                    persist_indicator_cache=False,
                    discovery_snapshot_cache=per_thread_snapshots,
                )
                return IndividualStockResearchService(
                    adapter=market_adapter,
                    backtest_service=BacktestService(
                        adapter=market_adapter,
                        indicator_cache=pool_indicator_cache,
                    ),
                    portfolio_service=PortfolioBacktestService(
                        adapter=market_adapter,
                        indicator_cache=pool_indicator_cache,
                    ),
                    strategy_service=per_thread_strategy_service,
                    validation_database_path=str(store.database_path),
                    validation_workers=pool_evaluation_worker_count(),
                    evaluation_worker_budget=pool_evaluation_budget,
                )

            pool_individual_service = pool_individual_service_factory()
            worker = StrategyDiscoveryWorker(
                job_client,
                strategy_service,
                strategy_pool_service=MarketStrategyPoolService(
                    store=store,
                    adapter=market_adapter,
                    individual_service=pool_individual_service,
                    backtest_service=pool_backtest_service,
                    pool_store=DuckDbStrategyPoolStore(store),
                    portfolio_service=pool_portfolio_service,
                    individual_service_factory=pool_individual_service_factory,
                    evaluation_worker_budget=pool_evaluation_budget,
                ),
                service_factory=lambda worker_count: StrategyDiscoveryService(
                    store=store,
                    market_data_adapter=DuckDbMarketDataAdapter(store),
                    evaluation_workers=worker_count,
                    persist_indicator_cache=False,
                    discovery_snapshot_cache=snapshot_cache,
                ),
            )
            with HttpStrategyJobClient.from_env(
                operations=["market_strategy_pool_tracking"], suffix="-tracking"
            ) as tracking_client:
                tracker = StrategyDiscoveryWorker(
                    tracking_client,
                    strategy_service,
                    pool_tracking_service=PoolTrackingRunner(store, pool_evaluation_budget),
                )
                thread = Thread(
                    target=tracker.run_forever,
                    kwargs={"stop_event": stop_event},
                    name="pool-tracking-consumer",
                    daemon=True,
                )
                thread.start()
                try:
                    worker.run_forever(stop_event=stop_event)
                finally:
                    stop_event.set()
                    thread.join()


if __name__ == "__main__":
    main()


__all__ = ["StrategyDiscoveryWorker", "main"]
