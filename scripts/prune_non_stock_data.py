#!/usr/bin/env python3
"""删除行情库里的非股票标的（基金/ETF/债券/指数）及其 K 线与指标缓存。

本系统只研究股票，非股票标的在界面与接口层面已经不再出现；但历史库里可能
还留着以前同步/导入进来的行。这个脚本负责把它们清掉。

安全约定（重要）：
  * 默认只体检，不改任何数据。必须显式加 `--apply` 才会删除。
  * 删除前先在库文件旁边写一份完整备份（`<库名>.prune-backup-<UTC时间戳>`），
    备份不会被自动清理，确认无误后自己删。
  * 只依赖 duckdb + 标准库，可以直接用
    `scripts/container_python.sh scripts/prune_non_stock_data.py --apply`
    丢进 backend 容器里跑（数据库在容器可见的命名卷上）。

用法：
    python scripts/prune_non_stock_data.py [--database /path/market.duckdb]
    python scripts/prune_non_stock_data.py --apply
    python scripts/prune_non_stock_data.py --apply --yes      # 非交互环境

注意：DuckDB 是单写者。跑 `--apply` 之前先停掉后端，否则会以
"Conflicting lock is held in PID x" 直接失败（失败是好事，说明没抢到锁）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb

KEPT_TYPE = "stock"
# 缓存表没有 instrument_type 列，只能按标的反查。
CACHE_TABLES = ("indicator_cache", "custom_indicator_cache")
BAR_TABLE = "daily_bars"
INSTRUMENT_TABLE = "instruments"


def resolve_database_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    configured = os.getenv("SELECTOR_DATA_DIR")
    if configured:
        return Path(configured).expanduser() / "market.duckdb"
    try:
        from selector_app.market_data.store_base import default_database_path
    except Exception:  # 容器里可能没有装 selector_app，退回容器内的默认位置
        return Path("/data/market/market.duckdb")
    return default_database_path()


def existing_tables(connection: duckdb.DuckDBPyConnection) -> set[str]:
    rows = connection.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def instrument_type_counts(connection: duckdb.DuckDBPyConnection) -> list[tuple[str, int]]:
    rows = connection.execute(
        f"SELECT instrument_type, COUNT(*) FROM {INSTRUMENT_TABLE} GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall()
    return [(str(row[0]), int(row[1])) for row in rows]


def bar_type_counts(connection: duckdb.DuckDBPyConnection) -> list[tuple[str, int, int]]:
    """按类型统计 K 线：行数 + 涉及标的数。"""

    rows = connection.execute(
        f"""
        SELECT instrument_type, COUNT(*) AS bars, COUNT(DISTINCT market || '.' || code) AS codes
        FROM {BAR_TABLE} GROUP BY 1 ORDER BY 2 DESC
        """
    ).fetchall()
    return [(str(row[0]), int(row[1]), int(row[2])) for row in rows]


def _scan(connection: duckdb.DuckDBPyConnection, tables: set[str]) -> dict[str, object]:
    """统计将要被删除的行数。缓存表没有类型列，只能按非股票标的反查。"""

    report: dict[str, object] = {}
    if INSTRUMENT_TABLE in tables:
        report["instruments_by_type"] = instrument_type_counts(connection)
        report["instruments_to_delete"] = int(
            connection.execute(
                f"SELECT COUNT(*) FROM {INSTRUMENT_TABLE} WHERE instrument_type <> ?",
                [KEPT_TYPE],
            ).fetchone()[0]
        )
        report["instruments_total"] = int(
            connection.execute(f"SELECT COUNT(*) FROM {INSTRUMENT_TABLE}").fetchone()[0]
        )
    if BAR_TABLE in tables:
        report["bars_by_type"] = bar_type_counts(connection)
        report["bars_to_delete"] = int(
            connection.execute(
                f"SELECT COUNT(*) FROM {BAR_TABLE} WHERE instrument_type <> ?",
                [KEPT_TYPE],
            ).fetchone()[0]
        )
        report["bars_total"] = int(
            connection.execute(f"SELECT COUNT(*) FROM {BAR_TABLE}").fetchone()[0]
        )
    cache_rows: dict[str, int] = {}
    for table in CACHE_TABLES:
        if table not in tables:
            continue
        cache_rows[table] = int(
            connection.execute(
                f"""
                SELECT COUNT(*) FROM {table}
                WHERE (market, code) IN (
                    SELECT market, code FROM {INSTRUMENT_TABLE} WHERE instrument_type <> ?
                )
                """,
                [KEPT_TYPE],
            ).fetchone()[0]
        )
    report["cache_rows_to_delete"] = cache_rows
    return report


def print_report(path: Path, report: dict[str, object]) -> None:
    print(f"数据库: {path}")
    print()
    counts = report.get("instruments_by_type") or []
    if counts:
        print("标的 instrument_type 分布：")
        for type_name, count in counts:
            mark = "保留" if type_name == KEPT_TYPE else "删除"
            print(f"  {type_name:<8} {count:>8}  → {mark}")
    bars = report.get("bars_by_type") or []
    if bars:
        print()
        print("日线 instrument_type 分布：")
        for type_name, count, codes in bars:
            mark = "保留" if type_name == KEPT_TYPE else "删除"
            print(f"  {type_name:<8} {count:>12} 行 / {codes:>6} 只  → {mark}")
    print()
    print(
        "将删除：标的 {instruments} 只、日线 {bars} 行".format(
            instruments=report.get("instruments_to_delete", 0),
            bars=report.get("bars_to_delete", 0),
        )
    )
    cache_rows = report.get("cache_rows_to_delete") or {}
    if cache_rows:
        detail = "、".join(f"{table} {count} 行" for table, count in cache_rows.items())
        print(f"        指标缓存 {detail}（缓存是可重建的派生数据）")


def backup_database(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = path.with_name(f"{path.name}.prune-backup-{stamp}")
    shutil.copy2(path, target)
    return target


# 缓存表按「标的已被判定为非股票」反查；日线除此之外还看自己的类型列。
_NON_STOCK_CODES = (
    f"(market, code) IN (SELECT market, code FROM {INSTRUMENT_TABLE} WHERE instrument_type <> ?)"
)


def prune(connection: duckdb.DuckDBPyConnection, tables: set[str]) -> dict[str, int]:
    # DuckDB 没有 changes()，删除前先把命中行数记下来。
    deleted: dict[str, int] = {}
    connection.execute("BEGIN TRANSACTION")
    try:
        for table in CACHE_TABLES:
            if table not in tables:
                continue
            where = f"WHERE {_NON_STOCK_CODES}"
            count = connection.execute(
                f"SELECT COUNT(*) FROM {table} {where}", [KEPT_TYPE]
            ).fetchone()[0]
            deleted[table] = int(count)
            connection.execute(f"DELETE FROM {table} {where}", [KEPT_TYPE])
        if BAR_TABLE in tables:
            where = f"WHERE instrument_type <> ? OR {_NON_STOCK_CODES}"
            deleted[BAR_TABLE] = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {BAR_TABLE} {where}", [KEPT_TYPE, KEPT_TYPE]
                ).fetchone()[0]
            )
            connection.execute(f"DELETE FROM {BAR_TABLE} {where}", [KEPT_TYPE, KEPT_TYPE])
        if INSTRUMENT_TABLE in tables:
            where = "WHERE instrument_type <> ?"
            deleted[INSTRUMENT_TABLE] = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {INSTRUMENT_TABLE} {where}", [KEPT_TYPE]
                ).fetchone()[0]
            )
            connection.execute(f"DELETE FROM {INSTRUMENT_TABLE} {where}", [KEPT_TYPE])
    except Exception:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")
    connection.execute("CHECKPOINT")
    return deleted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="删除行情库里的非股票标的与数据")
    parser.add_argument(
        "--database",
        help="行情库路径（默认取 SELECTOR_DATA_DIR 或程序默认位置）",
    )
    parser.add_argument("--apply", action="store_true", help="真正执行删除（默认只体检）")
    parser.add_argument("--yes", action="store_true", help="跳过交互确认")
    args = parser.parse_args(argv)

    path = resolve_database_path(args.database)
    if not path.exists():
        print(f"行情库不存在: {path}", file=sys.stderr)
        return 2

    # 体检用只读连接：后端还在跑也能看，不会被锁挡住。
    if args.apply:
        try:
            connection = duckdb.connect(str(path), read_only=False)
        except duckdb.Error as exc:
            print(
                "打不开写连接，通常说明后端还占着这个库。"
                "先停掉 backend 再跑（docker compose stop backend）："
                f"\n  {exc}",
                file=sys.stderr,
            )
            return 1
    else:
        connection = duckdb.connect(str(path), read_only=True)

    try:
        tables = existing_tables(connection)
        if INSTRUMENT_TABLE not in tables:
            print(f"库里没有 {INSTRUMENT_TABLE} 表，无需清理: {path}")
            return 0
        report = _scan(connection, tables)
        print_report(path, report)

        to_delete = int(report.get("instruments_to_delete", 0)) + int(
            report.get("bars_to_delete", 0)
        )
        if not args.apply:
            print()
            print("这是体检模式，没有改动任何数据。确认无误后加 --apply 执行删除。")
            return 0
        if to_delete == 0:
            print()
            print("没有需要删除的非股票数据。")
            return 0

        print()
        if not args.yes:
            answer = input("确认删除上面这些行？输入 yes 继续：").strip().lower()
            if answer != "yes":
                print("已取消，没有改动任何数据。")
                return 0

        # 先 CHECKPOINT 把 WAL 落盘，再复制，保证备份是完整的。
        connection.execute("CHECKPOINT")
        backup = backup_database(path)
        print(f"已备份到: {backup}")
        deleted = prune(connection, tables)
        print("已删除：" + "、".join(f"{table} {count} 行" for table, count in deleted.items()))
        print()
        print("库文件不会立刻变小，被释放的空间会被后续写入复用。")
        print("确认没问题后自行删除备份，重启后端即可。")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
