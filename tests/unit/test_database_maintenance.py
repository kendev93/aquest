from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pandas as pd
import pytest

from selector_app.market_data.maintenance import (
    DatabaseMaintenanceScheduler,
    DatabaseMaintenanceService,
    MaintenanceResult,
    should_compact,
)
from selector_app.market_data.store import DuckDbMarketDataStore


def _store(tmp_path: Path) -> DuckDbMarketDataStore:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars(
        "SH",
        "600000",
        "stock",
        pd.DataFrame(
            {
                "date": ["2024-01-01", "2024-01-02"],
                "open": [10.0, 10.5],
                "high": [10.5, 11.0],
                "low": [9.5, 10.0],
                "close": [10.2, 10.8],
                "volume": [1000.0, 1100.0],
                "amount": [10200.0, 11880.0],
                "bar_status": ["completed", "completed"],
            }
        ),
    )
    return store


def test_should_compact_only_after_the_configured_growth_threshold() -> None:
    assert should_compact(150, 100) is True
    assert should_compact(149, 100) is False
    assert should_compact(100, None) is False


def test_maintenance_records_a_baseline_for_a_small_database(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = DatabaseMaintenanceService(
        store.database_path,
        minimum_size_bytes=10**12,
    )

    result = service.run_if_needed()

    assert result.status == "baseline"
    record = json.loads(service.record_path.read_text(encoding="utf-8"))
    assert record["baseline_size_bytes"] == store.database_path.stat().st_size
    assert record["last_successful_after_size_bytes"] is None
    assert result.before_size_bytes == store.database_path.stat().st_size


def test_maintenance_rewrites_database_and_preserves_table_data(tmp_path: Path) -> None:
    store = _store(tmp_path)
    database = store.database_path
    service = DatabaseMaintenanceService(
        database,
        minimum_size_bytes=1,
        initial_free_ratio=0.0,
    )

    result = service.run_if_needed()

    assert result.status == "completed"
    assert result.trigger == "initial_fragmentation"
    assert result.after_size_bytes is not None
    assert database.is_file()
    assert len(store.read_bars("SH", "600000")) == 2
    record = json.loads(service.record_path.read_text(encoding="utf-8"))
    assert record["status"] == "completed"
    assert record["last_successful_after_size_bytes"] == result.after_size_bytes
    assert record["last_compacted_at"]


def test_failed_maintenance_keeps_original_database_and_records_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    database = store.database_path
    original_bytes = database.read_bytes()
    service = DatabaseMaintenanceService(
        database,
        minimum_size_bytes=1,
        initial_free_ratio=0.0,
    )

    def fail_copy(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("copy failed")

    monkeypatch.setattr(service, "_copy_database", fail_copy)
    result = service.run_if_needed()

    assert result.status == "failed"
    assert result.error == "copy failed"
    assert database.read_bytes() == original_bytes
    assert not list(tmp_path.glob("market.compact-*.duckdb"))
    record = json.loads(service.record_path.read_text(encoding="utf-8"))
    assert record["status"] == "failed"
    assert record["error"] == "copy failed"


def test_maintenance_scheduler_skips_active_jobs_and_runs_when_idle() -> None:
    class Runner:
        def __init__(self, active: bool) -> None:
            self.active = active

        def has_active_jobs(self) -> bool:
            return self.active

    class Service:
        def __init__(self) -> None:
            self.calls = 0

        def run_if_needed(self) -> MaintenanceResult:
            self.calls += 1
            return MaintenanceResult(
                status="skipped",
                reason="growth_threshold_not_reached",
                before_size_bytes=1,
                after_size_bytes=1,
                trigger=None,
                started_at=None,
                finished_at=None,
                duration_seconds=0.0,
                record_path="/tmp/market.maintenance.json",
            )

    service = Service()
    active_scheduler = DatabaseMaintenanceScheduler(
        service,  # type: ignore[arg-type]
        Runner(active=True),  # type: ignore[arg-type]
    )
    idle_scheduler = DatabaseMaintenanceScheduler(
        service,  # type: ignore[arg-type]
        Runner(active=False),  # type: ignore[arg-type]
    )

    assert asyncio.run(active_scheduler.check_once()) is None
    result = asyncio.run(idle_scheduler.check_once())
    assert result is not None
    assert service.calls == 1
