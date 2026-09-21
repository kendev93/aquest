"""Background scheduling for the once-per-day tracking rerun."""

from __future__ import annotations

import asyncio
import logging
import uuid

from selector_app.market_data.auto_sync import ACTIVE_DATA_JOB_DESCRIPTIONS
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.screening.jobs import ProgressReporter, ScreenJobRunner

from .tracking import StrategyTrackingService, TrackingRunner

logger = logging.getLogger(__name__)

TRACKING_DESCRIPTION = "策略追踪"
TRACKING_EXCLUSIVE_GROUP = "strategy_tracking"
DEFAULT_TRACKING_INTERVAL_SECONDS = 15 * 60


def _date_key(value: object) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text.replace("-", ""))
    except ValueError:
        return None


class StrategyTrackingScheduler:
    """Replay every online strategy once per market day, after the data lands.

    顺序是关键：**必须在行情同步之后**。反了会拿昨天的数据算一遍，得到一份
    看起来正常、实际过时的结果——比报错糟糕得多。
    """

    def __init__(
        self,
        *,
        store: DuckDbMarketDataStore,
        runner: ScreenJobRunner,
        runner_service: TrackingRunner,
        interval_seconds: float = DEFAULT_TRACKING_INTERVAL_SECONDS,
        enabled: bool = True,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("策略追踪检查间隔必须大于 0")
        self._store = store
        self._runner = runner
        self._service = StrategyTrackingService(store=store, runner=runner_service)
        self._interval_seconds = interval_seconds
        self._enabled = enabled
        self._active_job_id: str | None = None

    def submit_if_due(self) -> str | None:
        """Submit one rerun batch when there is fresh data and nothing else runs."""

        if not self._enabled:
            return None
        if self._has_active_job() or self._runner.has_active_jobs(ACTIVE_DATA_JOB_DESCRIPTIONS):
            return None
        if not self._store.list_tracked_strategies():
            return None
        # 有策略已经落后于最新交易日才值得跑。"今天没人需要更新"是常态，
        # 每次都提交一个空任务是纯粹的浪费。
        if not self._pending_strategies():
            return None

        job_id = uuid.uuid4().hex
        self._active_job_id = job_id
        try:
            self._runner.submit_callable(
                self._run_batch,
                description=TRACKING_DESCRIPTION,
                job_id=job_id,
                exclusive_group=TRACKING_EXCLUSIVE_GROUP,
            )
        except Exception:
            self._active_job_id = None
            raise
        logger.info("已提交每日策略追踪任务: %s", job_id)
        return job_id

    async def run(self) -> None:
        """Keep checking while the application lifespan is alive."""

        while True:
            try:
                self.submit_if_due()
            except Exception:  # noqa: BLE001 - scheduler must not kill the web app
                logger.exception("策略追踪调度检查失败")
            await asyncio.sleep(self._interval_seconds)

    def _pending_strategies(self) -> list[str]:
        """Strategy ids whose last computed day is behind the newest market day."""

        status = self._store.status()
        latest = _date_key(status.data_end)
        if latest is None:
            return []
        pending: list[str] = []
        for record in self._store.list_tracked_strategies():
            last = _date_key(record.get("last_tracked_date"))
            if last is None or last < latest:
                pending.append(str(record["strategy_id"]))
        return pending

    def _run_batch(self, progress: ProgressReporter) -> dict[str, object]:
        del progress
        results: list[dict[str, object]] = []
        failed: list[dict[str, object]] = []
        for record in self._store.list_tracked_strategies():
            strategy_id = str(record["strategy_id"])
            # 每次都重新读在线清单：跑的过程中用户可能停掉了某个策略。
            try:
                snapshot = self._service.refresh(strategy_id)
            except Exception as exc:  # noqa: BLE001 - one bad strategy must not stop the rest
                logger.exception("策略追踪重算失败: %s", strategy_id)
                failed.append({"strategy_id": strategy_id, "error": str(exc)[:300]})
                continue
            if snapshot.last_error:
                failed.append({"strategy_id": strategy_id, "error": snapshot.last_error[:300]})
            results.append(
                {
                    "strategy_id": strategy_id,
                    "last_tracked_date": snapshot.last_tracked_date,
                    "bars": len(snapshot.observations),
                }
            )
        return {
            "refreshed": results,
            "failed": failed,
            "total": len(results) + len(failed),
        }

    def _has_active_job(self) -> bool:
        if self._active_job_id is None:
            return False
        state = self._runner.get(self._active_job_id)
        if state is not None and state.status in {"queued", "running"}:
            return True
        self._active_job_id = None
        return False


__all__ = [
    "DEFAULT_TRACKING_INTERVAL_SECONDS",
    "TRACKING_DESCRIPTION",
    "TRACKING_EXCLUSIVE_GROUP",
    "StrategyTrackingScheduler",
]
