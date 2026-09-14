from __future__ import annotations

import time
from pathlib import Path

import pytest

from selector_app.market_data.sync_lock import (
    MarketDataSyncBusyError,
    MarketDataSyncLock,
)


def test_shared_sync_lock_is_exclusive_and_reports_owner(tmp_path: Path) -> None:
    database = tmp_path / "market_data.db"
    first = MarketDataSyncLock(database, owner="indicator-lab")
    second = MarketDataSyncLock(database, owner="etfquest")

    with first.acquire(job_id="job-1", phase="fetching") as lease:
        assert lease.snapshot()["owner"] == "indicator-lab"
        assert lease.snapshot()["job_id"] == "job-1"
        assert first.current()["phase"] == "fetching"
        with pytest.raises(MarketDataSyncBusyError):
            with second.acquire(job_id="job-2", phase="planning"):
                pass

    with second.acquire(job_id="job-2", phase="merging"):
        assert first.current()["owner"] == "etfquest"

    assert first.current() is None


def test_shared_sync_lock_reclaims_expired_lease(tmp_path: Path) -> None:
    database = tmp_path / "market_data.db"
    lock_path = database.with_name("market_data.sync.lock")
    lock_path.write_text(
        '{"token":"old","owner":"crashed","job_id":"old-job","phase":"fetching","heartbeat_at":0}',
        encoding="utf-8",
    )

    lock = MarketDataSyncLock(database, owner="indicator-lab", lease_seconds=1)
    with lock.acquire(job_id="new-job", phase="planning"):
        assert lock.current()["job_id"] == "new-job"
        assert time.time() > 1
