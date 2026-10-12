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
        # (market, code, instrument_type, board)：B 股的类型也是 stock，
        # 只有靠板块才能识别出来。
        rows = [
            ("SH", "600000", "stock", "main"),
            ("SZ", "000001", "stock", "main"),
            ("SH", "688001", "stock", "star"),
            ("SH", "510300", "fund", "main"),
            ("SH", "000001", "index", "main"),
            ("SH", "900001", "stock", "b_share"),
            ("SZ", "200001", "stock", "b_share"),
        ]
        for market, code, instrument_type, board in rows:
            connection.execute(
                "INSERT INTO instruments VALUES (?, ?, ?, ?, ?, 0, "
                "TIMESTAMP '2026-01-01 00:00:00')",
                [market, code, code, instrument_type, board],
            )
            connection.execute(
                "INSERT INTO daily_bars VALUES (?, ?, ?, ?, DATE '2026-01-05', 1)",
                [market, code, instrument_type, board],
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
    assert "fund" in out and "index" in out and "b_share" in out
    assert "体检模式" in out
    assert _counts(database) == {"instruments": 7, "daily_bars": 7, "indicator_cache": 7}
    assert list(tmp_path.glob("*.prune-backup-*")) == []


def test_apply_deletes_non_stock_rows_only(tmp_path: Path, capsys) -> None:
    database = tmp_path / "market.duckdb"
    _seed(database)

    assert prune_module.main(["--database", str(database), "--apply", "--yes"]) == 0

    assert _counts(database) == {"instruments": 3, "daily_bars": 3, "indicator_cache": 3}
    connection = duckdb.connect(str(database), read_only=True)
    try:
        rows = connection.execute(
            "SELECT instrument_type, board FROM instruments ORDER BY code"
        ).fetchall()
        remaining = {(row[0], row[1]) for row in rows}
    finally:
        connection.close()
    # 3 只 A 股留下；B 股虽然 instrument_type 是 stock，也一并删掉。
    assert remaining == {("stock", "main"), ("stock", "star")}
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
        assert int(connection.execute("SELECT COUNT(*) FROM instruments").fetchone()[0]) == 7
    finally:
        connection.close()


def test_apply_is_a_noop_when_the_store_is_already_stock_only(tmp_path: Path, capsys) -> None:
    database = tmp_path / "market.duckdb"
    _seed(database)
    prune_module.main(["--database", str(database), "--apply", "--yes"])

    capsys.readouterr()
    assert prune_module.main(["--database", str(database), "--apply", "--yes"]) == 0

    assert "没有需要删除的非股票数据" in capsys.readouterr().out
    assert _counts(database) == {"instruments": 3, "daily_bars": 3, "indicator_cache": 3}


def test_missing_database_reports_a_usage_error(tmp_path: Path, capsys) -> None:
    assert prune_module.main(["--database", str(tmp_path / "nope.duckdb")]) == 2
    assert "不存在" in capsys.readouterr().err


def test_cancelled_confirmation_changes_nothing(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "market.duckdb"
    _seed(database)
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")

    assert prune_module.main(["--database", str(database), "--apply"]) == 0

    assert _counts(database) == {"instruments": 7, "daily_bars": 7, "indicator_cache": 7}


def test_b_shares_are_dropped_even_though_their_type_is_stock(tmp_path: Path) -> None:
    """B 股的 instrument_type 是 stock，只看类型会整批漏掉。"""

    database = tmp_path / "market.duckdb"
    _seed(database)
    prune_module.main(["--database", str(database), "--apply", "--yes"])

    connection = duckdb.connect(str(database), read_only=True)
    try:
        codes = {row[0] for row in connection.execute("SELECT code FROM instruments").fetchall()}
    finally:
        connection.close()
    assert "900001" not in codes and "200001" not in codes
    assert {"600000", "000001", "688001"} == codes


def test_stores_without_a_board_column_fall_back_to_type(tmp_path: Path) -> None:
    """极老的库没有板块列，此时只能按 instrument_type 判（B 股会留下）。"""

    database = tmp_path / "market.duckdb"
    connection = duckdb.connect(str(database))
    try:
        connection.execute(
            "CREATE TABLE instruments (market VARCHAR, code VARCHAR, "
            "instrument_type VARCHAR, bar_count BIGINT, updated_at TIMESTAMP)"
        )
        for market, code, instrument_type in (
            ("SH", "600000", "stock"),
            ("SH", "900001", "stock"),
            ("SH", "510300", "fund"),
        ):
            connection.execute(
                "INSERT INTO instruments VALUES (?, ?, ?, 0, TIMESTAMP '2026-01-01')",
                [market, code, instrument_type],
            )
    finally:
        connection.close()

    assert prune_module.main(["--database", str(database), "--apply", "--yes"]) == 0
    assert _counts(database)["instruments"] == 2


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
    assert _counts(database)["instruments"] == 3
