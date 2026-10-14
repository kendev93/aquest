"""Background scheduling for once-per-day online market-data refreshes."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from inspect import Parameter, signature
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from aquest.adapters.market_sync import (
    AbortCheck,
    DetailCallback,
    MarketDataCancelledError,
    MarketSyncConfig,
    MarketSyncReport,
)
from aquest.indicators.precompute import (
    PRECOMPUTE_MAX_BARS,
    IndicatorPrecomputeService,
    should_defer_precompute,
)
from aquest.market_data.models import stored_end_year
from aquest.market_data.store import DuckDbMarketDataStore
from aquest.screening.jobs import (
    ProgressReporter,
    ScreenJobRunner,
    TaskCancelledError,
    report_progress_detail,
)

logger = logging.getLogger(__name__)

AUTO_SYNC_DESCRIPTION = "自动行情同步"
#: 健康检查与修复也走行情任务通道：它们读写同一个库，且都能跑十几秒以上，
#: 必须能被 ``/market-data/jobs/{job_id}`` 查到、能被取消、能被「已有行情任务」闸门挡住。
HEALTH_CHECK_DESCRIPTION = "行情健康检查"
HEALTH_REPAIR_DESCRIPTION = "行情修复"
#: 先删后下的整段重刷：和修复共用闸门，但范围是用户选的市场/板块而不是问题标的。
FULL_REFRESH_DESCRIPTION = "行情全量重刷"
MARKET_SYNC_EXCLUSIVE_GROUP = "market_data_sync"
MARKET_SYNC_JOB_DESCRIPTIONS = frozenset(
    {
        "本地行情导入",
        "本地行情自动导入",
        "行情同步",
        "全市场行情同步",
        "历史全量回填",
        FULL_REFRESH_DESCRIPTION,
        AUTO_SYNC_DESCRIPTION,
        HEALTH_CHECK_DESCRIPTION,
        HEALTH_REPAIR_DESCRIPTION,
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
#: 连续失败后的等待上限：源站限流或断网时，按 15 分钟猛敲一整个交易日只会更糟。
AUTO_SYNC_MAX_BACKOFF_SECONDS: float = 2 * 60 * 60
#: 指数位移的上限，只为了不把连错几个月的次数算成天文数字（真实上限由上面的封顶决定）。
_AUTO_SYNC_BACKOFF_MAX_STEPS = 24
AUTO_SYNC_AFTER_CLOSE = time(18, 30)
TRADING_CALENDAR_LOOKBACK_DAYS = 120
TRADING_CALENDAR_PROVIDER = "eastmoney_sse_composite"
_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


class IndicatorCacheLike(Protocol):
    """The slice of ``IndicatorCache`` the scheduler needs.

    刻意只用 Protocol：``indicators.cache`` 反过来依赖 ``market_data.store``，
    从本模块直接导入会把两个包锁成环。
    """

    def clear_memory(self) -> None: ...


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
        indicator_cache: IndicatorCacheLike | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("自动行情同步检查间隔必须大于 0")
        self._store = store
        self._runner = runner
        self._market_sync = market_sync
        self._precomputer = precomputer
        self._indicator_cache = indicator_cache
        # Kept as a compatibility-shaped constructor argument for embedders.
        # Automatic sync is online-only and must never inspect this path.
        del vipdoc_path
        self._interval_seconds = interval_seconds
        self._enabled = enabled
        self._clock = clock or (lambda: datetime.now(_SHANGHAI_TZ))
        self._active_job_id: str | None = None
        self._failure_streak = 0

    def submit_if_due(self) -> str | None:
        """Submit one session-bounded refresh when the latest market session is missing."""

        if not self._enabled:
            return None
        self._settle_auto_job()
        if self._has_active_auto_job() or self._runner.has_active_jobs(
            ACTIVE_DATA_JOB_DESCRIPTIONS
        ):
            return None

        status = self._store.status()
        if status.instrument_count == 0:
            return None
        now = _as_shanghai(self._clock())
        today = now.date()
        if not auto_sync_due(status.last_online_sync_at, now=now):
            return None

        get_meta = getattr(self._store, "get_meta", None)
        last_check_date = get_meta("last_auto_sync_check_date") if callable(get_meta) else None
        if last_check_date == today.isoformat():
            return None

        end_date: date | None = None
        fetch_calendar = getattr(self._market_sync, "fetch_trading_calendar", None)
        if callable(fetch_calendar):
            # Daily bars are complete only after the market close and provider publication.
            # Before then, wait locally instead of asking every stock for an unfinished date.
            if now.time() < AUTO_SYNC_AFTER_CLOSE:
                return None
            try:
                calendar_start = today - timedelta(days=TRADING_CALENDAR_LOOKBACK_DAYS)
                calendar_dates = tuple(
                    sorted(
                        {
                            item
                            for item in fetch_calendar(
                                calendar_start,
                                today,
                                timeout=10.0,
                            )
                            if isinstance(item, date) and item <= today
                        }
                    )
                )
                if not calendar_dates:
                    raise ValueError("上证综指没有返回有效交易日")
                save_calendar = getattr(self._store, "save_market_trading_calendar", None)
                if not callable(save_calendar):
                    raise RuntimeError("行情库不支持保存交易日历")
                save_calendar(
                    calendar_dates,
                    source=TRADING_CALENDAR_PROVIDER,
                    coverage_start=calendar_start,
                    coverage_end=today,
                )
                end_date = calendar_dates[-1]
                local_data_end = _parse_date(status.data_end)
                if local_data_end is not None and end_date <= local_data_end:
                    self._store.set_meta("last_auto_sync_check_date", today.isoformat())
                    self._store.set_meta("last_auto_sync_check_session", end_date.isoformat())
                    logger.info(
                        "自动行情同步跳过：本地行情已覆盖最近交易日 %s（核对日 %s）",
                        end_date,
                        today,
                    )
                    return None
            except Exception:  # noqa: BLE001 - calendar failure must not suppress a real sync
                logger.exception("读取交易日历失败，自动同步回退到自然日范围")
                end_date = today

        job_id = uuid.uuid4().hex
        self._active_job_id = job_id
        try:
            self._runner.submit_callable(
                lambda progress: self._run_sync(progress, end_date=end_date),
                description=AUTO_SYNC_DESCRIPTION,
                job_id=job_id,
                exclusive_group=MARKET_SYNC_EXCLUSIVE_GROUP,
            )
        except Exception:
            self._active_job_id = None
            self._note_failure("提交自动行情同步任务失败")
            raise
        logger.info("已提交每日自动行情同步任务: %s", job_id)
        return job_id

    async def run(self, on_job_submitted: Callable[[str], None] | None = None) -> None:
        """Keep checking while the application lifespan is alive."""

        while True:
            try:
                job_id = await asyncio.to_thread(self.submit_if_due)
                if job_id is not None and on_job_submitted is not None:
                    on_job_submitted(job_id)
            except Exception:  # noqa: BLE001 - scheduler must not kill the web app
                logger.exception("每日自动行情同步调度检查失败")
            await asyncio.sleep(self.next_check_delay_seconds())

    def next_check_delay_seconds(self) -> float:
        """距离下一次调度检查该等多久。

        没有失败时就是配置的 ``interval_seconds``；连续失败按 15 → 30 → 60 → 120 分钟
        退避并封顶 2 小时，成功后由 ``_settle_auto_job`` 清零。
        """

        if self._failure_streak <= 0:
            return self._interval_seconds
        # 2.0 而不是 2：整数幂在类型上算不出确定结果，这里要的就是一个倍数。
        growth: float = 2.0 ** min(self._failure_streak, _AUTO_SYNC_BACKOFF_MAX_STEPS)
        return min(self._interval_seconds * growth, AUTO_SYNC_MAX_BACKOFF_SECONDS)

    def _settle_auto_job(self) -> None:
        """给上一轮自动任务收尾：成功把退避清零，失败进入下一档等待。"""

        if self._active_job_id is None:
            return
        state = self._runner.get(self._active_job_id)
        if state is None:
            # 任务记录已被回收，判不出成败：放开引用继续调度，退避留到下一个真实终态。
            self._active_job_id = None
            return
        if state.status in {"queued", "running"}:
            return
        self._active_job_id = None
        if state.status == "completed":
            if self._failure_streak:
                logger.info(
                    "自动行情同步恢复正常，检查间隔回到 %s 分钟",
                    round(self._interval_seconds / 60, 1),
                )
            self._failure_streak = 0
            return
        if state.status == "failed":
            self._note_failure("自动行情同步任务失败", error=state.error)
        # cancelled 是用户主动停的，不算故障。

    def _note_failure(self, reason: str, *, error: str | None = None) -> None:
        self._failure_streak += 1
        logger.warning(
            "%s（连续 %s 次），%s 分钟后再检查调度：%s",
            reason,
            self._failure_streak,
            round(self.next_check_delay_seconds() / 60, 1),
            error or "无错误详情",
        )

    def _drop_indicator_cache(self) -> None:
        if self._indicator_cache is None:
            return
        try:
            self._indicator_cache.clear_memory()
        except Exception:  # noqa: BLE001 - cache is derived; never fail the sync
            logger.exception("自动行情同步后清理指标缓存失败")

    def _has_active_auto_job(self) -> bool:
        if self._active_job_id is None:
            return False
        state = self._runner.get(self._active_job_id)
        return bool(state is not None and state.status in {"queued", "running"})

    def _run_sync(
        self,
        progress: ProgressReporter,
        *,
        end_date: date | None = None,
    ) -> dict[str, object]:
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
                    end_date=end_date,
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
        # 行情已经写进库了，进程内的指标缓存和标的版本号副本必须一起丢掉，
        # 否则「日期不变、数值变了」的重写会继续命中旧条目——自动同步每天都
        # 可能触发这种重写。必须在预计算之前做：预计算也会读版本号。
        self._drop_indicator_cache()
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
                    # 与手动同步同一套参数：增量补算 + 按行情库实际末年锚定。
                    only_changed=True,
                    end_year=stored_end_year(store_status),
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


def _parse_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


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
    "HEALTH_CHECK_DESCRIPTION",
    "HEALTH_REPAIR_DESCRIPTION",
    "FULL_REFRESH_DESCRIPTION",
    "MARKET_SYNC_JOB_DESCRIPTIONS",
    "MARKET_SYNC_EXCLUSIVE_GROUP",
    "DEFAULT_AUTO_SYNC_INTERVAL_SECONDS",
    "AUTO_SYNC_MAX_BACKOFF_SECONDS",
    "AUTO_SYNC_AFTER_CLOSE",
    "TRADING_CALENDAR_LOOKBACK_DAYS",
    "TRADING_CALENDAR_PROVIDER",
    "AutomaticMarketSyncScheduler",
    "auto_sync_due",
]
