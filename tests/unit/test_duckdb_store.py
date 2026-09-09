from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from selector_app.market_data.models import InstrumentRef
from selector_app.market_data.store import DuckDbMarketDataStore, default_data_dir


def _bars(*rows: tuple[str, str, str, float, str]) -> pd.DataFrame:
    values = rows or (
        ("SH", "600000", "2024-01-02", 10.0, "local"),
        ("SH", "600000", "2024-01-03", 11.0, "local"),
    )
    return pd.DataFrame(
        [
            {
                "market": market,
                "code": code,
                "instrument_type": "stock",
                "date": pd.Timestamp(day),
                "open": close - 0.5,
                "high": close + 0.5,
                "low": close - 1.0,
                "close": close,
                "volume": 1000.0,
                "amount": close * 1000,
                "source": source,
                "price_basis": "raw",
                "bar_status": "completed",
            }
            for market, code, day, close, source in values
        ]
    )


def test_default_data_dir_uses_indicator_lab_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("SELECTOR_DATA_DIR", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert default_data_dir() == tmp_path / ".indicator-lab"


def test_store_initializes_schema_and_reads_one_symbol(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars())

    frame = store.read_bars("SH", "600000", start_date=date(2024, 1, 1))

    assert list(frame["date"].dt.strftime("%Y-%m-%d")) == ["2024-01-02", "2024-01-03"]
    assert list(frame["close"]) == [10.0, 11.0]


def test_store_excludes_provisional_bars_by_default(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    frame = _bars()
    frame.loc[1, "bar_status"] = "provisional"
    store.replace_local_bars("SH", "600000", "stock", frame)

    completed = store.read_bars("SH", "600000")
    all_bars = store.read_bars("SH", "600000", include_provisional=True)

    assert len(completed) == 1
    assert len(all_bars) == 2


def test_online_rows_fill_missing_dates_but_never_replace_local(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars())
    online = _bars(
        ("SH", "600000", "2024-01-03", 99.0, "online"),
        ("SH", "600000", "2024-01-04", 12.0, "online"),
    )
    store.upsert_online_bars(online)

    frame = store.read_bars("SH", "600000")

    assert list(frame["close"]) == [10.0, 11.0, 12.0]
    assert list(frame["source"]) == ["local", "local", "online"]


def test_batch_query_returns_sorted_instrument_groups(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars(
        "SH", "600001", "stock", _bars(("SH", "600001", "2024-01-02", 8.0, "local"))
    )
    store.replace_local_bars("SH", "600000", "stock", _bars())

    frame = store.read_many_bars(
        [InstrumentRef("SH", "600000", "stock"), InstrumentRef("SH", "600001", "stock")]
    )

    assert list(zip(frame["market"], frame["code"], strict=True)) == [
        ("SH", "600000"),
        ("SH", "600000"),
        ("SH", "600001"),
    ]


def test_batch_query_with_empty_selection_returns_empty_frame(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars())

    assert store.read_many_bars([]).empty


def test_store_status_reports_counts_and_range(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars())

    status = store.status()

    assert status.database_path == str(tmp_path / "market.duckdb")
    assert status.instrument_count == 1
    assert status.bar_count == 2
    assert status.data_start == "2024-01-02"
    assert status.data_end == "2024-01-03"


def test_store_persists_instrument_name_metadata(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars())

    assert store.update_instrument_names({("SH", "600000"): "浦发银行"}) == 1

    ref = store.list_instruments()[0]
    assert ref.name == "浦发银行"


def test_read_only_store_can_query_but_cannot_write(tmp_path: Path) -> None:
    database = tmp_path / "market.duckdb"
    store = DuckDbMarketDataStore(database)
    store.replace_local_bars("SH", "600000", "stock", _bars())
    read_only = DuckDbMarketDataStore(database, read_only=True)

    assert len(read_only.read_bars("SH", "600000")) == 2
    with pytest.raises(RuntimeError, match="只读"):
        read_only.set_meta("test", "value")


def test_store_retries_transient_duckdb_lock(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "market.duckdb"
    store = DuckDbMarketDataStore(database)
    store.replace_local_bars("SH", "600000", "stock", _bars())
    original_connect = duckdb.connect
    attempts = 0

    def flaky_connect(path, *, read_only=False):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise duckdb.IOException("Could not set lock on file: transient lock")
        return original_connect(path, read_only=read_only)

    monkeypatch.setattr(duckdb, "connect", flaky_connect)

    assert len(store.read_bars("SH", "600000")) == 2
    assert attempts == 3


def test_backup_database_creates_recoverable_copy(tmp_path: Path) -> None:
    database = tmp_path / "market.duckdb"
    store = DuckDbMarketDataStore(database)
    store.replace_local_bars("SH", "600000", "stock", _bars())

    backup = store.backup_database()

    assert backup.is_file()
    assert backup.read_bytes() == database.read_bytes()


def test_schema_upgrade_backups_existing_database_before_initialization(tmp_path: Path) -> None:
    database = tmp_path / "market.duckdb"
    with duckdb.connect(str(database)) as connection:
        connection.execute(
            "CREATE TABLE data_meta (key VARCHAR PRIMARY KEY, value VARCHAR, updated_at TIMESTAMP)"
        )
        connection.execute(
            "INSERT INTO data_meta VALUES ('schema_version', '0', CURRENT_TIMESTAMP)"
        )

    DuckDbMarketDataStore(database).status()

    backups = list(tmp_path.glob("market.bak-*.duckdb"))
    assert len(backups) == 1


def test_screen_history_keeps_only_the_latest_ten_results(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    result = {
        "market": "SH",
        "code": "600000",
        "signal_date": 20240103,
        "last_close": 11.0,
        "matched_signals": ["indicator_three.prepare_rally"],
        "match_count": 1,
        "indicator_values": {"indicator_three.var1": 9.0},
    }
    meta = {
        "total_candidates": 1,
        "total_scanned": 1,
        "total_signals": 1,
        "errors": 0,
        "skipped": 0,
        "failure_reasons": {},
        "skip_reasons": {},
    }

    for index in range(12):
        store.save_screen_history(
            f"job-{index}",
            results=[result],
            meta=meta,
            created_at=datetime(2024, 1, 1 + index, tzinfo=timezone.utc),
        )

    history = store.list_screen_history()
    loaded = store.read_screen_history("job-11")

    assert len(history) == 10
    assert [item["history_id"] for item in history] == [
        f"job-{index}" for index in range(11, 1, -1)
    ]
    assert loaded is not None
    assert loaded["results"] == [result]
    assert loaded["meta"] == meta
    assert store.read_screen_history("job-0") is None
