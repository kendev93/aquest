"""Daily due-date check for automatic incremental strategy research.

跟踪调度器只刷新信号与持仓，不会重新寻优；14/21 天的到期判定只是把标的
标成 due，真正重研仍要有人提交全市场增量任务。这个调度器补上那一步：定时检查
到期/待补算的标的，有待跑工作才提交，已有任务在跑就跳过。

全市场研究是重活，所以策略保守：默认跟随自动行情同步开启，一天最多自动发起
一次；没有待跑标的时退到更长间隔，提交失败按小时退避。手动提交、清空重建与
单股重研仍走原来的接口，调度器不参与。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Literal, Protocol

from aquest.market_data.auto_sync import ACTIVE_DATA_JOB_DESCRIPTIONS
from aquest.market_data.store_base import _WRITE_LOCK
from aquest.strategy_discovery.job_store import JobOperation

logger = logging.getLogger(__name__)

#: 每次检查的间隔（秒）。到期节奏以天计，查得太勤只是白读库。
RESEARCH_CHECK_INTERVAL_SECONDS = 30 * 60
#: 两次自动提交之间的最小间隔（秒）：一天最多一次。
AUTO_RESEARCH_MIN_INTERVAL_SECONDS = 24 * 60 * 60
#: 没有待跑标的时的下一次检查间隔（秒）。
IDLE_CHECK_INTERVAL_SECONDS = 6 * 60 * 60
#: 有任务在跑时的重试间隔（秒）。
BUSY_RETRY_SECONDS = 10 * 60
#: 提交或预估失败后的重试间隔（秒），避免异常时每半小时敲一次。
FAILURE_RETRY_SECONDS = 60 * 60
#: 与研究设置页一致的默认板块。
DEFAULT_RESEARCH_BOARDS = ("main", "chinext", "star")
#: 自动任务需要沿用的研究参数。没跑过手动全市场研究时才用这里的默认值。
_RESEARCH_PARAM_KEYS = (
    "risk_trials",
    "max_hold_days",
    "reversal_multiple",
    "atr_window",
    "min_bars",
    "horizon",
)
_DEFAULT_RESEARCH_PARAMS: dict[str, object] = {
    "risk_trials": 3_000,
    "max_hold_days": 20,
    "reversal_multiple": 2.0,
    "atr_window": 14,
    "min_bars": 5,
    "horizon": 10,
}

RESEARCH_OPERATION: Literal["market_strategy_pool"] = "market_strategy_pool"
TRACKING_OPERATIONS = frozenset({RESEARCH_OPERATION, "market_strategy_pool_tracking"})


class _ResearchEstimator(Protocol):
    def estimate_incremental(self, payload: Mapping[str, object]) -> Mapping[str, object]: ...


class _PoolStoreLike(Protocol):
    def list_entries(self) -> Sequence[object]: ...

    def read_research_settings(self) -> Mapping[str, object] | None: ...


class _RunnerLike(Protocol):
    def has_active_jobs(self, descriptions: Iterable[str] | None = None) -> bool: ...


class _BackendLike(Protocol):
    def submit(
        self,
        operation: JobOperation,
        payload: Mapping[str, object],
        *,
        description: str,
        worker_count: int | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> str: ...

    def active_snapshots(self) -> list[dict[str, object]]: ...


class PoolResearchScheduler:
    def __init__(
        self,
        pool_store: _PoolStoreLike,
        backend: _BackendLike,
        runner: _RunnerLike,
        service_factory: Callable[[], _ResearchEstimator],
        *,
        research_params_loader: Callable[[], Mapping[str, object] | None] | None = None,
        check_interval_seconds: float = RESEARCH_CHECK_INTERVAL_SECONDS,
        min_interval_seconds: float = AUTO_RESEARCH_MIN_INTERVAL_SECONDS,
        idle_interval_seconds: float = IDLE_CHECK_INTERVAL_SECONDS,
        busy_retry_seconds: float = BUSY_RETRY_SECONDS,
        failure_retry_seconds: float = FAILURE_RETRY_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.pool_store = pool_store
        self.backend = backend
        self.runner = runner
        self.service_factory = service_factory
        self.research_params_loader = research_params_loader
        self.check_interval_seconds = check_interval_seconds
        self.min_interval_seconds = min_interval_seconds
        self.idle_interval_seconds = idle_interval_seconds
        self.busy_retry_seconds = busy_retry_seconds
        self.failure_retry_seconds = failure_retry_seconds
        self._clock = clock
        self._next_attempt = 0.0

    def submit_if_due(self) -> str | None:
        """到期且有工作量时提交一次增量研究；返回任务 ID，跳过时返回 None。"""

        now = self._clock()
        if now < self._next_attempt:
            return None
        if not self.pool_store.list_entries():
            # 首次全市场研究仍由用户明确发起；调度器只维护已有的池子。
            self._next_attempt = now + self.idle_interval_seconds
            return None
        if self._busy():
            self._next_attempt = now + self.busy_retry_seconds
            return None
        saved_params = self._saved_research_params()
        payload = self._payload(saved_params)
        try:
            estimate = self.service_factory().estimate_incremental(payload)
        except Exception:  # noqa: BLE001 - 预估失败不能拖垮调度循环
            logger.exception("策略池自动增量：工作量预估失败")
            self._next_attempt = now + self.failure_retry_seconds
            return None
        pending = estimate.get("pending_symbols")
        if not isinstance(pending, int) or isinstance(pending, bool) or pending <= 0:
            self._next_attempt = now + self.idle_interval_seconds
            return None
        universe = estimate.get("universe_symbols")
        reusable = estimate.get("reusable_symbols")
        if (
            isinstance(universe, int)
            and not isinstance(universe, bool)
            and isinstance(reusable, int)
            and not isinstance(reusable, bool)
            and universe > 0
            and reusable == 0
            and pending == universe
        ):
            # 整池都要重研不是“到期增量”，而是换了研究口径或全池过期。全市场
            # 寻优几小时起，宁可让用户手动点一次，也不能让调度器夜里悄悄开跑。
            logger.warning(
                "策略池自动增量：整池 %d 只均需重算，留给手动“增量更新策略池”",
                pending,
            )
            self._next_attempt = now + self.idle_interval_seconds
            return None
        # 预估不持锁；提交前在写锁里重新确认没有任务抢跑，和手动提交同一把锁。
        with _WRITE_LOCK:
            if self._busy():
                self._next_attempt = now + self.busy_retry_seconds
                return None
            try:
                job_id = self.backend.submit(
                    RESEARCH_OPERATION,
                    payload,
                    description="全市场策略池自动增量研究",
                    worker_count=1,
                    metadata={
                        "mode": "full",
                        "phase_label": "排队等待策略 worker",
                        "automatic": True,
                    },
                )
            except Exception:  # noqa: BLE001 - 提交失败下一轮再试
                logger.exception("策略池自动增量：提交失败")
                self._next_attempt = now + self.failure_retry_seconds
                return None
        self._next_attempt = now + self.min_interval_seconds
        logger.info("策略池自动增量研究已提交: %s（待跑 %d 只）", job_id, pending)
        return job_id

    def _busy(self) -> bool:
        if self.runner.has_active_jobs(ACTIVE_DATA_JOB_DESCRIPTIONS):
            return True
        return any(
            str(item.get("operation")) in TRACKING_OPERATIONS
            for item in self.backend.active_snapshots()
        )

    def _saved_research_params(self) -> dict[str, object]:
        """最近一次成功的手动全市场研究参数；读不到就退回默认口径。"""

        if self.research_params_loader is None:
            return {}
        try:
            payload = self.research_params_loader()
        except Exception:  # noqa: BLE001 - 读参数失败不能挡住自动研究
            logger.exception("策略池自动增量：读取上次研究参数失败，本轮用默认口径")
            return {}
        if not isinstance(payload, Mapping):
            return {}
        return {key: payload[key] for key in _RESEARCH_PARAM_KEYS if key in payload}

    def _payload(self, saved_params: Mapping[str, object]) -> dict[str, object]:
        saved = self.pool_store.read_research_settings()
        boards: list[str] = list(DEFAULT_RESEARCH_BOARDS)
        exclude_st = False
        if isinstance(saved, Mapping):
            raw_boards = saved.get("boards")
            if isinstance(raw_boards, list) and raw_boards:
                boards = [str(item) for item in raw_boards]
            exclude_st = bool(saved.get("exclude_st"))
        # 沿用上次手动全市场研究的搜索参数（风险试验次数、最长持有等）。不沿用的话
        # 自定义参数建出来的池会因配置指纹不符被判成全池待重算，自动任务会拿默认
        # 参数悄悄换掉研究口径。
        params = dict(_DEFAULT_RESEARCH_PARAMS)
        params.update(saved_params)
        return {
            "mode": "full",
            "automatic": True,
            **params,
            "boards": boards,
            "exclude_st": exclude_st,
        }

    async def run(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.submit_if_due)
            except Exception:  # noqa: BLE001 - 调度循环要一直活着
                logger.exception("策略池自动增量研究调度失败")
            await asyncio.sleep(self.check_interval_seconds)
