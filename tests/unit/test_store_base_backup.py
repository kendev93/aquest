from __future__ import annotations

import duckdb

from selector_app.market_data.store_base import _DuckDbStoreBase


def test_store_base_retains_only_the_latest_backup(tmp_path) -> None:
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
