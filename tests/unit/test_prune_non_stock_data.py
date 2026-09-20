"""scripts/prune_non_stock_data.py 的行为测试。

脚本没有做成包（用 scripts/container_python.sh 直接丢进容器跑），所以这里用
importlib 按文件路径加载。所有用例都在 pytest 给的临时目录里建库。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import duckdb
import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "prune_non_stock_data.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("prune_non_stock_data", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prune_module = _load_module()


def _seed(path: Path) -> None:
    connection = duckdb.connect(str(path))
    try:
        connection.execute(
            """
            CREATE TABLE instruments (
                market VARCHAR NOT NULL, code VARCHAR NOT NULL, name VARCHAR,
                instrument_type VARCHAR NOT NULL, board VARCHAR NOT NULL DEFAULT 'main',
                bar_count BIGINT NOT NULL DEFAULT 0, updated_at TIMESTAMP NOT NULL,
                PRIMARY KEY (market, code)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE daily_bars (
                market VARCHAR NOT NULL, code VARCHAR NOT NULL,
                instrument_type VARCHAR NOT NULL, board VARCHAR NOT NULL DEFAULT 'main',
                trade_date DATE NOT NULL, close DOUBLE NOT NULL,
                PRIMARY KEY (market, code, trade_date)
            )
            """
        )
        for table in ("indicator_cache", "custom_indicator_cache"):
            connection.execute(
                f"CREATE TABLE {table} (market VARCHAR NOT NULL, code VARCHAR NOT NULL)"
            )
        rows = [
            ("SH", "600000", "stock"),
            ("SZ", "000001", "stock"),
            ("SH", "510300", "fund"),
            ("SH", "000001", "index"),
        ]
        for market, code, instrument_type in rows:
            connection.execute(
                "INSERT INTO instruments VALUES (?, ?, ?, ?, 'main', 0, "
                "TIMESTAMP '2026-01-01 00:00:00')",
                [market, code, code, instrument_type],
            )
            connection.execute(
                "INSERT INTO daily_bars VALUES (?, ?, ?, 'main', DATE '2026-01-05', 1)",
                [market, code, instrument_type],
            )
            for table in ("indicator_cache", "custom_indicator_cache"):
                connection.execute(f"INSERT INTO {table} VALUES (?, ?)", [market, code])
    finally:
        connection.close()


def _counts(path: Path) -> dict[str, int]:
    connection = duckdb.connect(str(path), read_only=True)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
            ).fetchall()
        }
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("instruments", "daily_bars", "indicator_cache")
            if table in tables
        }
    finally:
        connection.close()


def test_dry_run_leaves_the_database_untouched(tmp_path: Path, capsys) -> None:
    database = tmp_path / "market.duckdb"
    _seed(database)

    assert prune_module.main(["--database", str(database)]) == 0

    out = capsys.readouterr().out
    assert "fund" in out and "index" in out
    assert "体检模式" in out
    assert _counts(database) == {"instruments": 4, "daily_bars": 4, "indicator_cache": 4}
    assert list(tmp_path.glob("*.prune-backup-*")) == []


def test_apply_deletes_non_stock_rows_only(tmp_path: Path, capsys) -> None:
    database = tmp_path / "market.duckdb"
    _seed(database)

    assert prune_module.main(["--database", str(database), "--apply", "--yes"]) == 0

    assert _counts(database) == {"instruments": 2, "daily_bars": 2, "indicator_cache": 2}
    connection = duckdb.connect(str(database), read_only=True)
    try:
        rows = connection.execute("SELECT DISTINCT instrument_type FROM instruments").fetchall()
        remaining = {row[0] for row in rows}
    finally:
        connection.close()
    assert remaining == {"stock"}
    assert "已删除" in capsys.readouterr().out


def test_apply_always_writes_a_backup_first(tmp_path: Path) -> None:
    database = tmp_path / "market.duckdb"
    _seed(database)

    prune_module.main(["--database", str(database), "--apply", "--yes"])

    backups = list(tmp_path.glob("market.duckdb.prune-backup-*"))
    assert len(backups) == 1
    backup = backups[0]
    connection = duckdb.connect(str(backup), read_only=True)
    try:
        assert int(connection.execute("SELECT COUNT(*) FROM instruments").fetchone()[0]) == 4
    finally:
        connection.close()


def test_apply_is_a_noop_when_the_store_is_already_stock_only(tmp_path: Path, capsys) -> None:
    database = tmp_path / "market.duckdb"
    _seed(database)
    prune_module.main(["--database", str(database), "--apply", "--yes"])

    capsys.readouterr()
    assert prune_module.main(["--database", str(database), "--apply", "--yes"]) == 0

    assert "没有需要删除的非股票数据" in capsys.readouterr().out
    assert _counts(database) == {"instruments": 2, "daily_bars": 2, "indicator_cache": 2}


def test_missing_database_reports_a_usage_error(tmp_path: Path, capsys) -> None:
    assert prune_module.main(["--database", str(tmp_path / "nope.duckdb")]) == 2
    assert "不存在" in capsys.readouterr().err


def test_cancelled_confirmation_changes_nothing(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "market.duckdb"
    _seed(database)
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")

    assert prune_module.main(["--database", str(database), "--apply"]) == 0

    assert _counts(database) == {"instruments": 4, "daily_bars": 4, "indicator_cache": 4}


@pytest.mark.parametrize("missing", ["indicator_cache", "custom_indicator_cache", "daily_bars"])
def test_tolerates_older_schemas_without_every_table(tmp_path: Path, missing: str) -> None:
    database = tmp_path / "market.duckdb"
    _seed(database)
    connection = duckdb.connect(str(database))
    try:
        connection.execute(f"DROP TABLE {missing}")
    finally:
        connection.close()

    assert prune_module.main(["--database", str(database), "--apply", "--yes"]) == 0
    assert _counts(database)["instruments"] == 2
