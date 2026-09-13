"""Background scheduling for once-per-day online market-data refreshes."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from datetime import datetime
from inspect import Parameter, signature
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from selector_app.adapters.market_sync import (
    AbortCheck,
    DetailCallback,
    MarketDataCancelledError,
    MarketSyncConfig,
    MarketSyncReport,
)
from selector_app.indicators.precompute import (
    PRECOMPUTE_MAX_BARS,
    IndicatorPrecomputeService,
    should_defer_precompute,
)
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.screening.jobs import (
    ProgressReporter,
    ScreenJobRunner,
    TaskCancelledError,
    report_progress_detail,
)

logger = logging.getLogger(__name__)

AUTO_SYNC_DESCRIPTION = "自动行情同步"
MARKET_SYNC_EXCLUSIVE_GROUP = "market_data_sync"
MARKET_SYNC_JOB_DESCRIPTIONS = frozenset(
    {
        "本地行情导入",
        "本地行情自动导入",
        "行情同步",
        "East Money历史全量回填",
        AUTO_SYNC_DESCRIPTION,
    }
)
ACTIVE_DATA_JOB_DESCRIPTIONS = frozenset(
    {
        *MARKET_SYNC_JOB_DESCRIPTIONS,
        "指标预计算",
    }
)
# Compatibility alias for embedders that used the narrower first-release name.
ACTIVE_SYNC_DESCRIPTIONS = ACTIVE_DATA_JOB_DESCRIPTIONS
DEFAULT_AUTO_SYNC_INTERVAL_SECONDS = 15 * 60
_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


class MarketSyncService(Protocol):
    def sync(
        self,
        config: MarketSyncConfig,
        progress_callback: Callable[[int, int], None] | None = None,
        *,
        detail_callback: DetailCallback | None = None,
        abort_check: AbortCheck | None = None,
    ) -> MarketSyncReport | dict[str, object]: ...


def auto_sync_due(last_online_sync_at: str | None, *, now: datetime | None = None) -> bool:
    """Return whether the online market data has not been synced on the local day."""

    current = _as_shanghai(now or datetime.now(_SHANGHAI_TZ))
    if not last_online_sync_at:
        return True
    try:
        previous = datetime.fromisoformat(last_online_sync_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    return _as_shanghai(previous).date() < current.date()


class AutomaticMarketSyncScheduler:
    """Submit at most one online refresh per local calendar day."""

    def __init__(
        self,
        *,
        store: DuckDbMarketDataStore,
        runner: ScreenJobRunner,
        market_sync: MarketSyncService,
        precomputer: IndicatorPrecomputeService,
        vipdoc_path: str | Path | None = None,
        interval_seconds: float = DEFAULT_AUTO_SYNC_INTERVAL_SECONDS,
        enabled: bool = True,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("自动行情同步检查间隔必须大于 0")
        self._store = store
        self._runner = runner
        self._market_sync = market_sync
        self._precomputer = precomputer
        # Kept as a compatibility-shaped constructor argument for embedders.
        # Automatic sync is online-only and must never inspect this path.
        del vipdoc_path
        self._interval_seconds = interval_seconds
        self._enabled = enabled
        self._clock = clock or (lambda: datetime.now(_SHANGHAI_TZ))
        self._active_job_id: str | None = None

    def submit_if_due(self) -> str | None:
        """Submit one refresh if the store is populated and today's refresh is missing."""

        if not self._enabled:
            return None
        if self._has_active_auto_job() or self._runner.has_active_jobs(
            ACTIVE_DATA_JOB_DESCRIPTIONS
        ):
            return None

        status = self._store.status()
        if status.instrument_count == 0:
            return None
        if not auto_sync_due(status.last_online_sync_at, now=self._clock()):
            return None

        job_id = uuid.uuid4().hex
        self._active_job_id = job_id
        try:
            self._runner.submit_callable(
                self._run_sync,
                description=AUTO_SYNC_DESCRIPTION,
                job_id=job_id,
                exclusive_group=MARKET_SYNC_EXCLUSIVE_GROUP,
            )
        except Exception:
            self._active_job_id = None
            raise
        logger.info("已提交每日自动行情同步任务: %s", job_id)
        return job_id

    async def run(self, on_job_submitted: Callable[[str], None] | None = None) -> None:
        """Keep checking while the application lifespan is alive."""

        while True:
            try:
                job_id = self.submit_if_due()
                if job_id is not None and on_job_submitted is not None:
                    on_job_submitted(job_id)
            except Exception:  # noqa: BLE001 - scheduler must not kill the web app
                logger.exception("每日自动行情同步调度检查失败")
            await asyncio.sleep(self._interval_seconds)

    def _has_active_auto_job(self) -> bool:
        if self._active_job_id is None:
            return False
        state = self._runner.get(self._active_job_id)
        if state is not None and state.status in {"queued", "running"}:
            return True
        self._active_job_id = None
        return False

    def _run_sync(self, progress: ProgressReporter) -> dict[str, object]:
        job_id = self._active_job_id or ""

        def abort_check() -> bool:
            return bool(job_id) and self._runner.is_cancel_requested(job_id)

        try:
            result = _call_market_sync(
                self._market_sync,
                MarketSyncConfig(
                    universe="all",
                    provider="eastmoney",
                    bars=800,
                    prefer_store_targets=False,
                    refresh_names=False,
                ),
                progress,
                detail_callback=lambda detail: report_progress_detail(progress, detail),
                abort_check=abort_check,
            )
            if abort_check():
                raise MarketDataCancelledError("行情同步已取消")
        except MarketDataCancelledError as exc:
            raise TaskCancelledError(str(exc)) from exc
        payload = result.to_dict() if isinstance(result, MarketSyncReport) else dict(result)
        if abort_check():
            raise TaskCancelledError("行情同步已取消")
        try:
            store_status = self._store.status()
            if should_defer_precompute(store_status.bar_count):
                payload["indicator_precompute"] = {
                    "status": "deferred",
                    "reason": "large_dataset",
                    "bar_count": store_status.bar_count,
                    "max_bars": PRECOMPUTE_MAX_BARS,
                    "message": (
                        f"行情数据量为 {store_status.bar_count:,} 根，超过指标预计算上限 "
                        f"{PRECOMPUTE_MAX_BARS:,} 根，已改为按需计算"
                    ),
                }
            else:
                precomputed = self._precomputer.run(
                    vipdoc_path=".",
                    progress_callback=progress,
                )
                payload["indicator_precompute"] = precomputed.to_dict()
        except Exception:  # noqa: BLE001 - derived cache failure must not hide a sync success
            logger.exception("自动行情同步后的指标预计算失败")
            payload["indicator_precompute"] = {
                "status": "failed",
                "message": "指标预计算未完成，后续筛选或回测会按需重建",
            }
        return payload


def _as_shanghai(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=_SHANGHAI_TZ)
    return value.astimezone(_SHANGHAI_TZ)


def _call_market_sync(
    service: MarketSyncService,
    config: MarketSyncConfig,
    progress: ProgressReporter,
    *,
    detail_callback: DetailCallback,
    abort_check: AbortCheck,
) -> MarketSyncReport | dict[str, object]:
    if _supports_abort_check(service.sync):
        return service.sync(
            config,
            progress,
            detail_callback=detail_callback,
            abort_check=abort_check,
        )
    return service.sync(config, progress, detail_callback=detail_callback)


def _supports_abort_check(method: object) -> bool:
    if not callable(method):
        return False
    try:
        parameters = signature(method).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "abort_check"
        and parameter.kind in {Parameter.POSITIONAL_OR_KEYWORD, Parameter.KEYWORD_ONLY}
        for parameter in parameters
    )


__all__ = [
    "AUTO_SYNC_DESCRIPTION",
    "MARKET_SYNC_JOB_DESCRIPTIONS",
    "MARKET_SYNC_EXCLUSIVE_GROUP",
    "DEFAULT_AUTO_SYNC_INTERVAL_SECONDS",
    "AutomaticMarketSyncScheduler",
    "auto_sync_due",
]
