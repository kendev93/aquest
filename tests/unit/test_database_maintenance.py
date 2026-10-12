from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from aquest.market_data.maintenance import (
    BACKUP_RETENTION_DAYS,
    DatabaseMaintenanceScheduler,
    DatabaseMaintenanceService,
    MaintenanceResult,
    resolve_backup_retention_days,
    should_compact,
    swap_in_compacted_database,
)
from aquest.market_data.store import DuckDbMarketDataStore


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
    record = service._load_record()
    assert record["baseline_size_bytes"] == result.before_size_bytes
    assert record["last_successful_after_size_bytes"] is None
    assert result.after_size_bytes == result.before_size_bytes


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
    record = service._load_record()
    assert record["status"] == "completed"
    assert record["last_successful_after_size_bytes"] == result.after_size_bytes
    assert record["last_compacted_at"]


def test_failed_maintenance_keeps_original_database_and_records_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    database = store.database_path
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
    assert len(store.read_bars("SH", "600000")) == 2
    assert not list(tmp_path.glob("market.compact-*.duckdb"))
    record = service._load_record()
    assert record["status"] == "failed"
    assert record["error"] == "copy failed"


def test_maintenance_record_is_persisted_in_duckdb_and_imports_legacy_json(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    service = DatabaseMaintenanceService(store.database_path, minimum_size_bytes=10**12)
    legacy = {
        "version": 1,
        "status": "baseline",
        "last_successful_after_size_bytes": 1234,
    }
    service.record_path.write_text(json.dumps(legacy), encoding="utf-8")

    assert service._load_record() == legacy
    with duckdb.connect(str(store.database_path), read_only=True) as connection:
        saved = connection.execute(
            "SELECT value FROM data_meta WHERE key = 'maintenance_state'"
        ).fetchone()
    assert saved is not None
    assert json.loads(saved[0]) == legacy

    service._write_record({**legacy, "status": "checked"})
    assert json.loads(saved[0]) == legacy
    assert service._load_record()["status"] == "checked"


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
    remote_job_scheduler = DatabaseMaintenanceScheduler(
        service,  # type: ignore[arg-type]
        Runner(active=False),  # type: ignore[arg-type]
        additional_busy_check=lambda: True,
    )
    idle_scheduler = DatabaseMaintenanceScheduler(
        service,  # type: ignore[arg-type]
        Runner(active=False),  # type: ignore[arg-type]
    )

    assert asyncio.run(active_scheduler.check_once()) is None
    assert asyncio.run(remote_job_scheduler.check_once()) is None
    result = asyncio.run(idle_scheduler.check_once())
    assert result is not None
    assert service.calls == 1


def _make_database(path: Path, value: str) -> None:
    with duckdb.connect(str(path)) as connection:
        connection.execute("CREATE TABLE marker (value VARCHAR)")
        connection.execute("INSERT INTO marker VALUES (?)", [value])


def _marker(path: Path) -> str:
    with duckdb.connect(str(path), read_only=True) as connection:
        return str(connection.execute("SELECT value FROM marker").fetchone()[0])


def _supports_hard_links(directory: Path) -> bool:
    source = directory / "link-probe-src"
    target = directory / "link-probe-dst"
    source.write_text("x", encoding="utf-8")
    try:
        os.link(source, target)
        return True
    except OSError:
        return False
    finally:
        source.unlink(missing_ok=True)
        target.unlink(missing_ok=True)


def test_compaction_swap_keeps_the_previous_file_as_the_backup(tmp_path: Path) -> None:
    database = tmp_path / "market.duckdb"
    temp = tmp_path / "market.compact.duckdb"
    _make_database(database, "old")
    _make_database(temp, "new")

    backup_path = swap_in_compacted_database(
        database,
        temp,
        validate=lambda path: _marker(path),
        retention_days=3,
    )

    assert backup_path is not None
    assert _marker(database) == "new"
    assert _marker(backup_path) == "old"
    assert not temp.exists()


@pytest.mark.skipif(
    not _supports_hard_links(Path(tempfile.gettempdir())),
    reason="文件系统不支持硬链接",
)
def test_compaction_backup_is_a_hard_link_not_a_copy(tmp_path: Path) -> None:
    """备份必须复用原文件的 inode，否则压缩仍要第三份磁盘拷贝。"""

    database = tmp_path / "market.duckdb"
    temp = tmp_path / "market.compact.duckdb"
    _make_database(database, "old")
    _make_database(temp, "new")
    original_inode = database.stat().st_ino

    backup_path = swap_in_compacted_database(
        database,
        temp,
        validate=lambda path: _marker(path),
        retention_days=3,
    )

    assert backup_path is not None
    assert backup_path.stat().st_ino == original_inode
    assert database.stat().st_ino != original_inode


def test_zero_retention_keeps_no_backup_after_a_successful_swap(tmp_path: Path) -> None:
    database = tmp_path / "market.duckdb"
    temp = tmp_path / "market.compact.duckdb"
    _make_database(database, "old")
    _make_database(temp, "new")

    backup_path = swap_in_compacted_database(
        database,
        temp,
        validate=lambda path: _marker(path),
        retention_days=0,
    )

    assert backup_path is None
    assert _marker(database) == "new"
    assert list(tmp_path.glob("*.bak-*")) == []


def test_failed_validation_restores_the_previous_database(tmp_path: Path) -> None:
    database = tmp_path / "market.duckdb"
    temp = tmp_path / "market.compact.duckdb"
    _make_database(database, "old")
    _make_database(temp, "new")

    def _reject(_path: Path) -> None:
        raise RuntimeError("临时 DuckDB 校验失败：表行数或 schema 不一致")

    with pytest.raises(RuntimeError, match="校验失败"):
        swap_in_compacted_database(
            database,
            temp,
            validate=_reject,
            retention_days=3,
        )

    assert _marker(database) == "old"


def test_swap_falls_back_to_a_real_copy_without_hard_link_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "market.duckdb"
    temp = tmp_path / "market.compact.duckdb"
    _make_database(database, "old")
    _make_database(temp, "new")
    original_inode = database.stat().st_ino

    def _no_links(*_args: object, **_kwargs: object) -> None:
        raise OSError("hard links unsupported")

    monkeypatch.setattr(os, "link", _no_links)

    backup_path = swap_in_compacted_database(
        database,
        temp,
        validate=lambda path: _marker(path),
        retention_days=3,
    )

    assert backup_path is not None
    assert backup_path.stat().st_ino != original_inode
    assert _marker(backup_path) == "old"
    assert _marker(database) == "new"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, BACKUP_RETENTION_DAYS),
        ("", BACKUP_RETENTION_DAYS),
        ("  ", BACKUP_RETENTION_DAYS),
        ("0", 0),
        ("7", 7),
    ],
)
def test_backup_retention_days_resolution(raw: str | None, expected: int) -> None:
    assert resolve_backup_retention_days(raw) == expected


@pytest.mark.parametrize("raw", ["abc", "-1", "1.5"])
def test_backup_retention_days_rejects_invalid_values(raw: str) -> None:
    with pytest.raises(ValueError):
        resolve_backup_retention_days(raw)
