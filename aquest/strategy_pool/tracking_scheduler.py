"""Daily completed-market-day scheduling; never triggered by opening a page."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, Literal, cast

from aquest.market_data.auto_sync import ACTIVE_DATA_JOB_DESCRIPTIONS
from aquest.market_data.store import DuckDbMarketDataStore
from aquest.strategy_discovery.duckdb_job_store import DuckDbStrategyJobStore
from aquest.strategy_pool.tracking_store import PoolTrackingStore

TRACKING_OPERATION: Literal["market_strategy_pool_tracking"] = "market_strategy_pool_tracking"


def latest_completed_day(store: DuckDbMarketDataStore) -> str | None:
    with store.read_session() as connection:
        row = connection.execute(
            "SELECT MAX(trade_date) FROM daily_bars WHERE "
            "instrument_type='stock' AND bar_status='completed'"
        ).fetchone()
    return row[0].isoformat() if row and row[0] else None


class PoolTrackingScheduler:
    def __init__(self, store: DuckDbMarketDataStore, backend: Any, runner: Any):
        self.store, self.backend, self.runner = store, backend, runner
        self.tracks = PoolTrackingStore(store)
        self.jobs = DuckDbStrategyJobStore(store)

    def submit_if_due(self) -> str | None:
        from aquest.market_data.store_base import _WRITE_LOCK

        if self.runner.has_active_jobs(ACTIVE_DATA_JOB_DESCRIPTIONS):
            return None
        with _WRITE_LOCK:
            if any(
                item.get("operation") == TRACKING_OPERATION
                for item in self.backend.active_snapshots()
            ):
                return None
            latest = latest_completed_day(self.store)
            versions = self.tracks.current_versions()
            if not latest or not any(
                v["status"] == "pending" or str(v["summary"].get("as_of_date") or "") < latest
                for v in versions
            ):
                return None
            with self.store.read_session() as connection:
                attempts = connection.execute(
                    "SELECT request_json,created_at FROM strategy_jobs WHERE operation=? "
                    "AND json_extract_string(request_json,'$.payload.as_of_date')=?",
                    [TRACKING_OPERATION, latest],
                ).fetchall()
            attempted_ids: set[str] = set()
            for raw_request, submitted in attempts:
                previous_payload = json.loads(raw_request)["payload"]
                ids = previous_payload.get("version_ids")
                if ids is not None:
                    attempted_ids.update(ids)
                else:
                    attempted_ids.update(
                        v["version_id"]
                        for v in versions
                        if datetime.fromisoformat(v["created_at"]).timestamp() <= submitted
                    )
            pending_ids = [
                v["version_id"]
                for v in versions
                if v["status"] == "pending" and v["version_id"] not in attempted_ids
            ]
            if attempts and not pending_ids:
                return None
            return cast(
                str,
                self.backend.submit(
                    TRACKING_OPERATION,
                    dict(
                        mode="signals",
                        objective="annual_return",
                        automatic=True,
                        as_of_date=latest,
                        version_ids=pending_ids
                        if attempts
                        else [v["version_id"] for v in versions],
                        initialization=bool(attempts),
                    ),
                    description="每日策略池跟踪",
                    worker_count=1,
                    metadata=dict(mode="signals", phase_label="排队等待跟踪通道"),
                ),
            )

    async def run(self) -> None:
        import logging

        while True:
            try:
                await asyncio.to_thread(self.submit_if_due)
            except Exception:
                logging.getLogger(__name__).exception("策略池每日跟踪调度失败")
            await asyncio.sleep(60)
