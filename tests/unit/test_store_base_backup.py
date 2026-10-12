from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import duckdb

from aquest.market_data.maintenance import cleanup_database_backups
from aquest.market_data.store_base import _DuckDbStoreBase


def test_store_base_retains_recent_backups_and_cleans_expired_ones(tmp_path) -> None:
    database = tmp_path / "market.duckdb"
    with duckdb.connect(str(database)) as connection:
        connection.execute("CREATE TABLE sample (value INTEGER)")
        connection.execute("INSERT INTO sample VALUES (1)")

    store = _DuckDbStoreBase(database)
    first = store._backup_file()
    second = store._backup_file()

    backups = list(tmp_path.glob("market.bak-*.duckdb"))
    assert first != second
    assert not first.exists()
    assert backups == [second]
    assert second.read_bytes() == database.read_bytes()

    old_timestamp = (datetime.now(timezone.utc) - timedelta(days=4)).timestamp()
    os.utime(second, (old_timestamp, old_timestamp))
    assert cleanup_database_backups(database, now=datetime.now(timezone.utc)) == (second,)
    assert not second.exists()
