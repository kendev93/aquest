from __future__ import annotations

import struct
import time
from datetime import datetime
from pathlib import Path
from threading import Lock
from zoneinfo import ZoneInfo

import selector_app.market_data.day_importer as day_importer_module
from selector_app.market_data.day_importer import LocalDayImporter
from selector_app.market_data.store import DuckDbMarketDataStore

_RECORD = struct.Struct("<IIIIIfII")


def _append_day(path: Path, *dates: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        for index, value in enumerate(dates):
            handle.write(
                _RECORD.pack(
                    value,
                    1000 + index,
                    1100 + index,
                    900 + index,
                    1050 + index,
                    123456.5 + index,
                    200000 + index,
                    0,
                )
            )


def test_importer_is_full_then_incremental_and_does_not_modify_source(tmp_path: Path) -> None:
    vipdoc = tmp_path / "vipdoc"
    day = vipdoc / "sh" / "lday" / "sh600000.day"
    _append_day(day, 20240102, 20240103)
    before = day.stat()
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    importer = LocalDayImporter(store)

    first = importer.import_vipdoc(vipdoc)
    second = importer.import_vipdoc(vipdoc)

    assert first.imported_files == 1
    assert first.imported_bars == 2
    assert second.unchanged_files == 1
    assert second.imported_bars == 0
    after = day.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)

    _append_day(day, 20240104)
    third = importer.import_vipdoc(vipdoc)

    assert third.replaced_instruments == 1
    assert store.status().bar_count == 3


def test_importer_skips_unknown_day_files_and_records_reason(tmp_path: Path) -> None:
    vipdoc = tmp_path / "vipdoc"
    unknown = vipdoc / "sh" / "lday" / "sh490001.day"
    _append_day(unknown, 20240102)
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")

    report = LocalDayImporter(store).import_vipdoc(vipdoc)

    assert report.skipped_files == 1
    assert report.errors == 0
    assert report.failure_reasons["unknown_instrument"] == 1


def test_importer_reloads_unchanged_file_after_market_close_to_finalize_bar(
    tmp_path: Path,
) -> None:
    vipdoc = tmp_path / "vipdoc"
    day = vipdoc / "sh" / "lday" / "sh600000.day"
    _append_day(day, 20240830)
    current = [datetime(2024, 8, 30, 15, 4, tzinfo=ZoneInfo("Asia/Shanghai"))]
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    importer = LocalDayImporter(store, clock=lambda: current[0])

    before_close = importer.import_vipdoc(vipdoc)
    assert before_close.provisional_bars == 1
    assert len(store.read_bars("SH", "600000")) == 0

    current[0] = datetime(2024, 8, 30, 15, 6, tzinfo=ZoneInfo("Asia/Shanghai"))
    after_close = importer.import_vipdoc(vipdoc)

    assert after_close.replaced_instruments == 1
    assert after_close.imported_bars == 1
    assert len(store.read_bars("SH", "600000")) == 1


def test_importer_keeps_previous_rows_when_changed_file_is_invalid(tmp_path: Path) -> None:
    vipdoc = tmp_path / "vipdoc"
    day = vipdoc / "sh" / "lday" / "sh600000.day"
    _append_day(day, 20240102, 20240103)
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    importer = LocalDayImporter(store)
    importer.import_vipdoc(vipdoc)

    day.write_bytes(_RECORD.pack(20241301, 1000, 1100, 900, 1050, 1.0, 100, 0))
    report = importer.import_vipdoc(vipdoc)

    assert report.errors == 1
    assert len(store.read_bars("SH", "600000")) == 2
    assert store.source_file(vipdoc, "sh/lday/sh600000.day")["status"] == "failed"


def test_importer_marks_missing_source_without_deleting_history(tmp_path: Path) -> None:
    vipdoc = tmp_path / "vipdoc"
    day = vipdoc / "sh" / "lday" / "sh600000.day"
    _append_day(day, 20240102)
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    importer = LocalDayImporter(store)
    importer.import_vipdoc(vipdoc)
    day.unlink()

    report = importer.import_vipdoc(vipdoc)

    assert report.missing_files == 1
    assert store.source_file(vipdoc, "sh/lday/sh600000.day")["status"] == "missing"
    assert len(store.read_bars("SH", "600000")) == 1


def test_importer_decodes_independent_day_files_with_eight_workers(
    tmp_path: Path, monkeypatch
) -> None:
    vipdoc = tmp_path / "vipdoc"
    for index in range(16):
        _append_day(vipdoc / "sh" / "lday" / f"sh600{index:03d}.day", 20240102)

    original = day_importer_module.read_day_file
    lock = Lock()
    active = 0
    max_active = 0

    def tracking_read_day_file(*args, **kwargs):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.02)
            return original(*args, **kwargs)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(day_importer_module, "read_day_file", tracking_read_day_file)
    report = LocalDayImporter(DuckDbMarketDataStore(tmp_path / "market.duckdb")).import_vipdoc(
        vipdoc
    )

    assert report.imported_files == 16
    assert report.errors == 0
    assert 1 < max_active <= 8
