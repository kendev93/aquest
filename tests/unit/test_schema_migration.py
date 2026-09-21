"""旧库升级：schema_version 落后的库必须能现场升到当前版本。

这里只测「迁移」本身，不测业务。踩过的坑：DuckDB 的
``ALTER TABLE ... ADD COLUMN`` 不支持 NOT NULL / DEFAULT 约束，
加列只能加裸列再 UPDATE 回填；单测里直接建空库走不到这条路径，
所以必须造一个旧版本的真实 duckdb 文件来跑。
"""

from __future__ import annotations

import duckdb
import pytest

from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.market_data.store_base import SCHEMA_VERSION

_LEGACY_STRATEGY_COLUMNS = (
    "strategy_id VARCHAR PRIMARY KEY",
    "name VARCHAR NOT NULL",
    "note VARCHAR NOT NULL DEFAULT ''",
    "source_history_id VARCHAR",
    "created_at TIMESTAMP NOT NULL",
    "updated_at TIMESTAMP NOT NULL",
    "request_json VARCHAR NOT NULL",
    "summary_json VARCHAR NOT NULL",
    "UNIQUE (name)",
)


def _legacy_database(path, version: int, *, created_at: str = "2026-09-01 09:00:00") -> None:
    """造一个 ``version`` 版本的库，表里预先放一行旧数据。"""

    with duckdb.connect(str(path)) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS data_meta ("
            "key VARCHAR PRIMARY KEY, value VARCHAR NOT NULL, updated_at TIMESTAMP NOT NULL)"
        )
        connection.execute(
            "INSERT INTO data_meta (key, value, updated_at) VALUES ('schema_version', ?, ?)",
            [str(version), created_at],
        )
        connection.execute(f"CREATE TABLE saved_strategies ({', '.join(_LEGACY_STRATEGY_COLUMNS)})")
        connection.execute(
            "INSERT INTO saved_strategies (strategy_id, name, note, source_history_id,"
            " created_at, updated_at, request_json, summary_json)"
            " VALUES ('old-1', '旧策略', '', NULL, ?, ?, '{}', '{}')",
            [created_at, created_at],
        )


def test_migrating_a_v14_database_adds_the_tracking_columns(tmp_path) -> None:
    path = tmp_path / "market.duckdb"
    _legacy_database(path, 14)

    store = DuckDbMarketDataStore(path)
    record = store.read_strategy("old-1")

    assert record is not None
    # 建表时的 NOT NULL DEFAULT 由迁移回填，读出来必须等价于新库。
    assert record["tracking_status"] == "off"
    assert record["last_error"] == ""
    assert record["launched_at"] is None
    assert record["last_tracked_date"] is None
    assert store.status().schema_version == SCHEMA_VERSION


def test_migration_creates_the_tracking_table(tmp_path) -> None:
    """迁移必须把 strategy_tracking 建出来，否则追踪一写就炸。"""

    path = tmp_path / "market.duckdb"
    _legacy_database(path, 14)

    store = DuckDbMarketDataStore(path)
    written = store.save_tracking_points(
        "old-1",
        [{"trade_date": 20260910, "equity": 1.05, "cash": 100.0, "positions_count": 3}],
    )

    assert written == 1
    points = store.list_tracking_points("old-1")
    assert [(point["trade_date"], point["equity"]) for point in points] == [(20260910, 1.05)]


def test_migration_is_idempotent(tmp_path) -> None:
    """已经升到 15 的库再打开一次不能炸，也不能把数据改坏。"""

    path = tmp_path / "market.duckdb"
    _legacy_database(path, 14)

    first = DuckDbMarketDataStore(path)
    first.start_strategy_tracking("old-1", launched_at=20260910)

    second = DuckDbMarketDataStore(path)
    record = second.read_strategy("old-1")

    assert record is not None
    assert record["tracking_status"] == "active"
    assert record["launched_at"] == "2026-09-10"


@pytest.mark.parametrize("version", [13, 14])
def test_older_databases_all_migrate_to_the_current_version(tmp_path, version: int) -> None:
    path = tmp_path / "market.duckdb"
    _legacy_database(path, version)

    store = DuckDbMarketDataStore(path)

    assert store.status().schema_version == SCHEMA_VERSION
    assert store.read_strategy("old-1") is not None
