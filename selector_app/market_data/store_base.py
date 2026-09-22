"""Shared DuckDB lifecycle and normalization primitives.

The public store is intentionally a small facade.  This module owns the
connection lifecycle, schema migrations, and invariants shared by the storage
repositories.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from threading import RLock, local
from typing import get_args

import duckdb
import numpy as np
import pandas as pd

from .day_format import classify_board
from .maintenance import cleanup_database_backups, create_database_backup
from .models import (
    BacktestHistoryType,
    DataStoreStatus,
    InstrumentBoard,
    InstrumentType,
)

SCHEMA_VERSION = 17
SCREEN_HISTORY_LIMIT = 10
STRATEGY_DISCOVERY_HISTORY_LIMIT = 20
# 每种类型最多保留这么多条，超出的最旧记录会被删掉。
# ⚠️ 这条限制有个容易被忽略的连带影响：想统计「这个库累计跑过多少次研究」时不能数
# 这张表——数出来最多就是这个数。跨运行的试验次数校正（候选池 #20）要的是累计值，
# 必须另建持久化计数，别在这里取巧。
BACKTEST_HISTORY_LIMIT = 20
# 从 BacktestHistoryType 派生，而不是再抄一份字面量：抄一份就会漏更新。
_BACKTEST_HISTORY_TYPES = set(get_args(BacktestHistoryType))
_APP_DATA_DIR_NAME = ".indicator-lab"
_BAR_COLUMNS = [
    "market",
    "code",
    "instrument_type",
    "board",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "source",
    "provider",
    "price_basis",
    "bar_status",
    "imported_at",
]
_FRAME_COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount", "bar_status"]
_WRITE_LOCK = RLock()
_CONNECT_RETRY_DELAYS = (0.05, 0.1, 0.25, 0.5, 1.0)
# 本系统只研究股票；基金、债券、指数都不入库。
_INSTRUMENT_TYPES = {"stock"}
# DuckDB 默认按**宿主机**内存给自己配额，看不到容器的 cgroup 上限。批量写指标
# 缓存（十几 GB 的 blob）时它会一路吃内存直到被容器 OOM 杀掉——实测就是这样
# 把整个后端打崩的。显式给定一个上限，DuckDB 才会主动淘汰缓冲页。
_DUCKDB_MEMORY_RATIO = 0.5
_DUCKDB_MIN_MEMORY_BYTES = 1 << 30
_INSTRUMENT_BOARDS = {"main", "star", "chinext"}


class MarketDataStoreError(RuntimeError):
    """Safe, user-facing storage failure without exposing DuckDB internals."""


def default_data_dir() -> Path:
    configured = os.getenv("SELECTOR_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / _APP_DATA_DIR_NAME


def default_database_path() -> Path:
    return default_data_dir() / "market.duckdb"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _date_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value)
    return None if text in {"NaT", "nan", "None"} else text[:10]


def _timestamp_text(value: object) -> str | None:
    return None if value is None else str(value)


def _clean_name(value: object) -> str | None:
    if value is None:
        return None
    missing = pd.isna(value)
    if isinstance(missing, (bool, np.bool_)) and bool(missing):
        return None
    text = str(value).strip()
    return text or None


def _append_filter(
    conditions: list[str],
    parameters: list[object],
    column: str,
    values: Iterable[str] | None,
) -> None:
    selected = tuple(str(value) for value in values or ())
    if not selected:
        return
    placeholders = ", ".join("?" for _ in selected)
    conditions.append(f"{column} IN ({placeholders})")
    parameters.extend(selected)


def _read_memory_limit_bytes() -> int | None:
    for raw_path in (
        "/sys/fs/cgroup/memory.max",
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",
    ):
        try:
            raw = Path(raw_path).read_text(encoding="ascii").strip()
        except (OSError, ValueError):
            continue
        if not raw or raw == "max":
            continue
        try:
            limit = int(raw)
        except ValueError:
            continue
        if 0 < limit < 1 << 60:
            return limit
    try:
        total = int(os.sysconf("SC_PHYS_PAGES")) * int(os.sysconf("SC_PAGE_SIZE"))
    except (AttributeError, OSError, ValueError):
        return None
    return total if total > 0 else None


@lru_cache(maxsize=1)
def _duckdb_memory_limit() -> str | None:
    """Return a DuckDB `memory_limit` matching what this process may really use."""

    limit = _read_memory_limit_bytes()
    if limit is None:
        return None
    budget = max(int(limit * _DUCKDB_MEMORY_RATIO), _DUCKDB_MIN_MEMORY_BYTES)
    return f"{budget // (1 << 30)}GiB" if budget >= (1 << 30) else f"{budget // (1 << 20)}MiB"


class _DuckDbStoreBase:
    """Own connections and shared invariants for all storage repositories."""

    def __init__(
        self,
        database_path: str | Path | None = None,
        *,
        read_only: bool = False,
        pool_read_connections: bool = False,
    ) -> None:
        self.database_path = Path(database_path or default_database_path()).expanduser()
        self.read_only = read_only
        self._initialized = False
        # 打开一条 DuckDB 连接实测约 9.5 ms（占 read_bars 的 57%），逐标的读取时
        # 是主要开销。研究 worker 是只读短命进程，复用同一条只读连接可以省掉这部分。
        # 默认关闭：写库进程里长期持有只读连接可能读到本进程刚写入前的快照。
        # 跨阶段一致性不受影响——研究的三个阶段各自新建进程池，也就是各自新建连接，
        # 因此阶段之间的行情变化仍会被指纹校验捕获。
        self.pool_read_connections = pool_read_connections
        self._read_sessions = local()

    def _connect(self, *, read_only: bool | None = None) -> duckdb.DuckDBPyConnection:
        mode = self.read_only if read_only is None else read_only
        if mode:
            if not self.read_only:
                self._ensure_initialized()
            elif not self.database_path.exists():
                raise FileNotFoundError(f"行情数据库不存在: {self.database_path}")
        else:
            self._ensure_initialized()
        last_error: duckdb.Error | None = None
        for delay in (0.0, *_CONNECT_RETRY_DELAYS):
            if delay:
                time.sleep(delay)
            try:
                connection = self._open(read_only=mode)
            except duckdb.Error as exc:
                last_error = exc
                continue
            return connection
        raise MarketDataStoreError(
            "行情数据库当前被其它进程占用或存在瞬态 I/O 错误，请稍后重试"
        ) from last_error

    @contextmanager
    def _open_session(self, *, read_only: bool) -> Iterator[duckdb.DuckDBPyConnection]:
        connection = self._open(read_only=read_only)
        try:
            yield connection
        finally:
            connection.close()

    def _open(self, *, read_only: bool) -> duckdb.DuckDBPyConnection:
        # 必须在 connect 时通过 config 传入：事后 `SET memory_limit` 在只读连接上
        # 会失败，而同一库文件的连接配置不一致时 DuckDB 会直接拒绝再开连接。
        limit = _duckdb_memory_limit()
        config: dict[str, str | bool | int | float | list[str]] | None = None
        if limit is not None:
            config = {"memory_limit": limit}
        return duckdb.connect(str(self.database_path), read_only=read_only, config=config)

    @contextmanager
    def read_session(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """Yield a read-only connection, reusing one per thread when pooling is on."""

        if not self.pool_read_connections:
            connection = self._connect(read_only=True)
            try:
                yield connection
            finally:
                connection.close()
            return
        existing = getattr(self._read_sessions, "connection", None)
        if existing is None:
            existing = self._connect(read_only=True)
            self._read_sessions.connection = existing
        yield existing

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        if self.read_only:
            if not self.database_path.exists():
                raise FileNotFoundError(f"行情数据库不存在: {self.database_path}")
            self._initialized = True
            return
        with _WRITE_LOCK:
            if self._initialized:
                return
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()
            self._initialized = True

    def _initialize(self) -> None:
        existing_version = self._schema_version_on_disk()
        if existing_version is not None and existing_version > SCHEMA_VERSION:
            raise RuntimeError(f"行情数据库 schema 版本过高: {existing_version} > {SCHEMA_VERSION}")
        if existing_version is not None and existing_version < SCHEMA_VERSION:
            self._backup_file()
        with self._open_session(read_only=False) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS instruments (
                    market VARCHAR NOT NULL,
                    code VARCHAR NOT NULL,
                    name VARCHAR,
                    instrument_type VARCHAR NOT NULL,
                    board VARCHAR NOT NULL DEFAULT 'main',
                    source_path VARCHAR,
                    first_date DATE,
                    last_date DATE,
                    bar_count BIGINT NOT NULL DEFAULT 0,
                    source_state VARCHAR NOT NULL DEFAULT 'active',
                    last_provider VARCHAR,
                    updated_at TIMESTAMP NOT NULL,
                    PRIMARY KEY (market, code)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_bars (
                    market VARCHAR NOT NULL,
                    code VARCHAR NOT NULL,
                    instrument_type VARCHAR NOT NULL,
                    board VARCHAR NOT NULL DEFAULT 'main',
                    trade_date DATE NOT NULL,
                    open DOUBLE NOT NULL,
                    high DOUBLE NOT NULL,
                    low DOUBLE NOT NULL,
                    close DOUBLE NOT NULL,
                    volume DOUBLE NOT NULL,
                    amount DOUBLE NOT NULL,
                    source VARCHAR NOT NULL,
                    provider VARCHAR NOT NULL DEFAULT 'tdx',
                    price_basis VARCHAR NOT NULL,
                    bar_status VARCHAR NOT NULL,
                    imported_at TIMESTAMP NOT NULL,
                    UNIQUE (market, code, trade_date, price_basis)
                )
                """
            )
            self._migrate_schema(connection, existing_version or 0)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS data_meta (
                    key VARCHAR PRIMARY KEY,
                    value VARCHAR NOT NULL,
                    updated_at TIMESTAMP NOT NULL
                )
                """
            )
            if (existing_version or 0) < 11:
                connection.execute(
                    """
                    INSERT INTO data_meta (key, value, updated_at)
                    SELECT 'last_online_provider', 'tdx', ?
                    WHERE EXISTS (
                        SELECT 1 FROM daily_bars WHERE source = 'online'
                    )
                    ON CONFLICT (key) DO NOTHING
                    """,
                    [_now()],
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS indicator_cache (
                    market VARCHAR NOT NULL,
                    code VARCHAR NOT NULL,
                    timeframe VARCHAR NOT NULL,
                    price_basis VARCHAR NOT NULL,
                    formula_id VARCHAR NOT NULL,
                    formula_version VARCHAR NOT NULL,
                    data_fingerprint VARCHAR NOT NULL,
                    bar_offset BIGINT NOT NULL,
                    data_start DATE,
                    data_end DATE,
                    data_length BIGINT NOT NULL,
                    values_blob BLOB NOT NULL,
                    signals_blob BLOB NOT NULL,
                    computed_at TIMESTAMP NOT NULL,
                    PRIMARY KEY (
                        market, code, timeframe, price_basis, formula_id,
                        formula_version, data_fingerprint, bar_offset
                    )
                )
                """
            )
            # 缓存按自然年分块后主键多了 bar_offset。旧行没有这一列，且块边界与
            # 新布局不兼容（旧的一行就是全历史），留着只会占空间——缓存是可重建
            # 的派生数据，直接整表重建比逐行转换简单也安全。
            cache_columns = {
                str(row[0])
                for row in connection.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'main' AND table_name = 'indicator_cache'"
                ).fetchall()
            }
            if cache_columns and "bar_offset" not in cache_columns:
                connection.execute("DROP TABLE indicator_cache")
                connection.execute(
                    """
                    CREATE TABLE indicator_cache (
                        market VARCHAR NOT NULL,
                        code VARCHAR NOT NULL,
                        timeframe VARCHAR NOT NULL,
                        price_basis VARCHAR NOT NULL,
                        formula_id VARCHAR NOT NULL,
                        formula_version VARCHAR NOT NULL,
                        data_fingerprint VARCHAR NOT NULL,
                        bar_offset BIGINT NOT NULL,
                        data_start DATE,
                        data_end DATE,
                        data_length BIGINT NOT NULL,
                        values_blob BLOB NOT NULL,
                        signals_blob BLOB NOT NULL,
                        computed_at TIMESTAMP NOT NULL,
                        PRIMARY KEY (
                            market, code, timeframe, price_basis, formula_id,
                            formula_version, data_fingerprint, bar_offset
                        )
                    )
                    """
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS custom_indicator_cache (
                    market VARCHAR NOT NULL,
                    code VARCHAR NOT NULL,
                    timeframe VARCHAR NOT NULL,
                    price_basis VARCHAR NOT NULL,
                    formula_hash VARCHAR NOT NULL,
                    parameter_hash VARCHAR NOT NULL,
                    engine_version VARCHAR NOT NULL,
                    parameter_json VARCHAR NOT NULL,
                    value_names_json VARCHAR NOT NULL,
                    signal_names_json VARCHAR NOT NULL,
                    data_fingerprint VARCHAR NOT NULL,
                    data_start DATE,
                    data_end DATE,
                    data_length BIGINT NOT NULL,
                    minimum_bars BIGINT NOT NULL,
                    recommended_bars BIGINT NOT NULL,
                    values_blob BLOB NOT NULL,
                    signals_blob BLOB NOT NULL,
                    computed_at TIMESTAMP NOT NULL,
                    PRIMARY KEY (
                        market, code, timeframe, price_basis, formula_hash,
                        parameter_hash, engine_version, data_fingerprint
                    )
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS screen_history (
                    history_id VARCHAR PRIMARY KEY,
                    created_at TIMESTAMP NOT NULL,
                    total_candidates BIGINT NOT NULL,
                    total_scanned BIGINT NOT NULL,
                    total_signals BIGINT NOT NULL,
                    errors BIGINT NOT NULL,
                    skipped BIGINT NOT NULL,
                    result_count BIGINT NOT NULL,
                    meta_json VARCHAR NOT NULL,
                    results_json VARCHAR NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS strategy_discovery_history (
                    history_id VARCHAR PRIMARY KEY,
                    created_at TIMESTAMP NOT NULL,
                    started_at TIMESTAMP NOT NULL,
                    finished_at TIMESTAMP NOT NULL,
                    request_json VARCHAR NOT NULL,
                    search_metadata_json VARCHAR NOT NULL,
                    validation_metadata_json VARCHAR NOT NULL,
                    algorithm_metadata_json VARCHAR NOT NULL,
                    data_fingerprint VARCHAR NOT NULL,
                    results_by_sharpe_json VARCHAR NOT NULL,
                    results_by_return_json VARCHAR NOT NULL,
                    total_trials BIGINT NOT NULL,
                    duration_seconds DOUBLE NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS backtest_history (
                    history_id VARCHAR PRIMARY KEY,
                    backtest_type VARCHAR NOT NULL,
                    created_at TIMESTAMP NOT NULL,
                    started_at TIMESTAMP NOT NULL,
                    finished_at TIMESTAMP NOT NULL,
                    duration_seconds DOUBLE NOT NULL,
                    request_json VARCHAR NOT NULL,
                    result_json VARCHAR NOT NULL,
                    summary_json VARCHAR NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS condition_research_trials (
                    history_id VARCHAR PRIMARY KEY,
                    created_at TIMESTAMP NOT NULL,
                    data_fingerprint VARCHAR NOT NULL,
                    start_date VARCHAR NOT NULL,
                    end_date VARCHAR NOT NULL,
                    tested_conditions BIGINT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS strategy_discovery_replay_history (
                    replay_id VARCHAR PRIMARY KEY,
                    experiment_id VARCHAR NOT NULL,
                    leaderboard VARCHAR NOT NULL,
                    created_at TIMESTAMP NOT NULL,
                    started_at TIMESTAMP NOT NULL,
                    finished_at TIMESTAMP NOT NULL,
                    data_start VARCHAR NOT NULL,
                    data_end VARCHAR NOT NULL,
                    data_fingerprint VARCHAR NOT NULL,
                    duration_seconds DOUBLE NOT NULL,
                    result_json VARCHAR NOT NULL,
                    UNIQUE (experiment_id, leaderboard)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS strategy_jobs (
                    job_id VARCHAR PRIMARY KEY,
                    operation VARCHAR NOT NULL,
                    request_json VARCHAR NOT NULL,
                    state_json VARCHAR NOT NULL,
                    result_json VARCHAR,
                    status VARCHAR NOT NULL,
                    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
                    worker_id VARCHAR,
                    created_at DOUBLE NOT NULL,
                    heartbeat_at DOUBLE NOT NULL,
                    updated_at DOUBLE NOT NULL
                )
                """
            )
            # 策略库：一套可重放的组合回测配置 + 保存时的绩效快照。它和
            # backtest_history 的区别是——历史记录"跑过什么"（满了自动淘汰），
            # 策略记录"还想再跑一遍"（只有用户显式删才消失）。request_json 里
            # 的日期区间在保存时就固化成绝对日期，否则"近一年"这类相对模式
            # 会让三个月后的追踪重算悄悄换样本。
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS saved_strategies (
                    strategy_id VARCHAR PRIMARY KEY,
                    name VARCHAR NOT NULL,
                    note VARCHAR NOT NULL DEFAULT '',
                    source_history_id VARCHAR,
                    created_at TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP NOT NULL,
                    request_json VARCHAR NOT NULL,
                    summary_json VARCHAR NOT NULL,
                    tracking_status VARCHAR NOT NULL DEFAULT 'off',
                    launched_at DATE,
                    last_tracked_date INTEGER,
                    last_error VARCHAR NOT NULL DEFAULT '',
                    UNIQUE (name)
                )
                """
            )
            # 策略追踪：每个策略每个交易日一行净值。主键是 (策略, 日期)，写入用
            # upsert —— 重跑同一段会覆盖而不是翻倍，所以重算天然幂等，机器关机
            # 漏跑几天也能一次补齐。这里刻意不存交易明细（需要时从上线快照重算），
            # 也不存 Checkpoint 全量（它会把研究段的整份 equity/trades/states
            # 复制进来，每天重写会撑爆表）。
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS strategy_tracking (
                    strategy_id VARCHAR NOT NULL,
                    trade_date INTEGER NOT NULL,
                    equity DOUBLE NOT NULL,
                    cash DOUBLE NOT NULL,
                    positions_count INTEGER NOT NULL,
                    benchmark_value DOUBLE,
                    drawdown DOUBLE,
                    PRIMARY KEY (strategy_id, trade_date)
                )
                """
            )
            # 筛选方案：公式选股页整套可复现的输入。和 screen_history 的区别是——
            # 历史记"跑过什么"（满了自动淘汰），方案记"还想再跑一遍"（只有显式删
            # 才消失）。payload_json 只存输入，绝不存扫描结果：结果依赖当时行情，
            # 存下来下次加载的人会以为它还成立。
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS screen_presets (
                    preset_id VARCHAR PRIMARY KEY,
                    name VARCHAR NOT NULL,
                    note VARCHAR NOT NULL DEFAULT '',
                    created_at TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP NOT NULL,
                    payload_json VARCHAR NOT NULL,
                    UNIQUE (name)
                )
                """
            )
            # 候选库：条件研究挖出来的候选条件，带证据快照与采纳状态。和
            # backtest_history 的区别是——历史记"跑过什么"（满了自动淘汰），候选记
            # "我当时认不认可这条"（只有显式删才消失）。evidence_json 存的是入库
            # 那一刻的证据快照，不回指历史记录：历史只留最近 20 条，候选不能跟着
            # 一起消失。
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS candidate_library (
                    candidate_id VARCHAR PRIMARY KEY,
                    source VARCHAR NOT NULL,
                    source_history_id VARCHAR NOT NULL,
                    source_candidate_id VARCHAR NOT NULL,
                    side VARCHAR NOT NULL,
                    label VARCHAR NOT NULL,
                    condition_json VARCHAR NOT NULL,
                    evidence_json VARCHAR NOT NULL,
                    status VARCHAR NOT NULL DEFAULT 'pending',
                    note VARCHAR NOT NULL DEFAULT '',
                    published_strategy_id VARCHAR,
                    created_at TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP NOT NULL,
                    UNIQUE (source_history_id, source_candidate_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS stock_groups (
                    group_id VARCHAR PRIMARY KEY,
                    name VARCHAR NOT NULL,
                    note VARCHAR NOT NULL DEFAULT '',
                    source VARCHAR NOT NULL DEFAULT 'manual',
                    source_history_id VARCHAR,
                    created_at TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP NOT NULL,
                    items_json VARCHAR NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO data_meta (key, value, updated_at)
                VALUES ('schema_version', ?, ?)
                ON CONFLICT (key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                [str(SCHEMA_VERSION), _now()],
            )
        cleanup_database_backups(self.database_path)

    @staticmethod
    def _migrate_schema(connection: duckdb.DuckDBPyConnection, version: int) -> None:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
            ).fetchall()
        }
        if version < 2:
            for table in ("instruments", "daily_bars"):
                if table not in tables:
                    continue
                columns = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'main' AND table_name = ?",
                        [table],
                    ).fetchall()
                }
                if "board" in columns:
                    continue
                connection.execute(f"ALTER TABLE {table} ADD COLUMN board VARCHAR DEFAULT 'main'")
                # 只区分股票的板块。B 股/基金/债券/指数不再是合法板块值：这种老库
                # 里如果还留着它们，用 scripts/prune_non_stock_data.py 清掉
                # （脚本按 instrument_type + 板块白名单判定）。
                connection.execute(
                    f"UPDATE {table} SET board = CASE "
                    "WHEN market = 'SH' AND substr(code, 1, 2) = '68' THEN 'star' "
                    "WHEN market = 'SZ' AND substr(code, 1, 2) = '30' THEN 'chinext' "
                    "ELSE 'main' END"
                )
        if version < 3 and "instruments" in tables:
            columns = {
                str(row[0])
                for row in connection.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'main' AND table_name = 'instruments'"
                ).fetchall()
            }
            if "name" not in columns:
                connection.execute("ALTER TABLE instruments ADD COLUMN name VARCHAR")
        if version < 8 and "strategy_discovery_history" in tables:
            columns = {
                str(row[0])
                for row in connection.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'main' AND table_name = 'strategy_discovery_history'"
                ).fetchall()
            }
            if "started_at" not in columns:
                connection.execute(
                    "ALTER TABLE strategy_discovery_history ADD COLUMN started_at TIMESTAMP"
                )
            if "finished_at" not in columns:
                connection.execute(
                    "ALTER TABLE strategy_discovery_history ADD COLUMN finished_at TIMESTAMP"
                )
            connection.execute(
                "UPDATE strategy_discovery_history "
                "SET started_at = created_at WHERE started_at IS NULL"
            )
            connection.execute(
                "UPDATE strategy_discovery_history "
                "SET finished_at = created_at + duration_seconds * INTERVAL '1 second' "
                "WHERE finished_at IS NULL"
            )
        if version < 10:
            if "instruments" in tables:
                instrument_columns = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'main' AND table_name = 'instruments'"
                    ).fetchall()
                }
                if "last_provider" not in instrument_columns:
                    connection.execute("ALTER TABLE instruments ADD COLUMN last_provider VARCHAR")
            if "daily_bars" in tables:
                bar_columns = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'main' AND table_name = 'daily_bars'"
                    ).fetchall()
                }
                if "provider" not in bar_columns:
                    connection.execute(
                        "ALTER TABLE daily_bars ADD COLUMN provider VARCHAR DEFAULT 'tdx'"
                    )
                connection.execute(
                    "UPDATE daily_bars SET provider = CASE "
                    "WHEN source = 'local' THEN 'local' ELSE 'tdx' END "
                    "WHERE provider IS NULL OR provider = ''"
                )
        if version < 11 and "instruments" in tables and "daily_bars" in tables:
            connection.execute(
                """
                UPDATE instruments AS instruments
                SET last_provider = (
                    SELECT bars.provider
                    FROM daily_bars AS bars
                    WHERE bars.market = instruments.market
                      AND bars.code = instruments.code
                    ORDER BY bars.trade_date DESC
                    LIMIT 1
                )
                WHERE instruments.last_provider IS NULL
                """
            )
        if version < 12 and "instruments" in tables and "daily_bars" in tables:
            connection.execute(
                "UPDATE daily_bars SET provider = 'local' "
                "WHERE source = 'local' AND provider <> 'local'"
            )
            connection.execute(
                """
                UPDATE instruments AS instruments
                SET last_provider = (
                    SELECT bars.provider
                    FROM daily_bars AS bars
                    WHERE bars.market = instruments.market
                      AND bars.code = instruments.code
                    ORDER BY bars.trade_date DESC
                    LIMIT 1
                )
                WHERE EXISTS (
                    SELECT 1 FROM daily_bars AS bars
                    WHERE bars.market = instruments.market
                      AND bars.code = instruments.code
                )
                """
            )

        if version < 15 and "saved_strategies" in tables:
            columns = {
                str(row[0])
                for row in connection.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'main' AND table_name = 'saved_strategies'"
                ).fetchall()
            }
            # DuckDB 不支持 ADD COLUMN 带约束（NOT NULL / DEFAULT），
            # 所以先加裸列，再用 UPDATE 把已有行回填成建表时的默认值。
            for column, definition in (
                ("tracking_status", "VARCHAR"),
                ("launched_at", "DATE"),
                ("last_tracked_date", "INTEGER"),
                ("last_error", "VARCHAR"),
            ):
                if column not in columns:
                    connection.execute(
                        f"ALTER TABLE saved_strategies ADD COLUMN {column} {definition}"
                    )
            connection.execute(
                "UPDATE saved_strategies SET tracking_status = 'off' WHERE tracking_status IS NULL"
            )
            connection.execute(
                "UPDATE saved_strategies SET last_error = '' WHERE last_error IS NULL"
            )

    def _schema_version_on_disk(self) -> int | None:
        if not self.database_path.exists():
            return None
        try:
            with duckdb.connect(str(self.database_path), read_only=True) as connection:
                row = connection.execute(
                    "SELECT value FROM data_meta WHERE key = 'schema_version'"
                ).fetchone()
        except duckdb.CatalogException:
            return None
        except duckdb.Error as exc:
            raise MarketDataStoreError("行情数据库无法读取，请检查文件完整性") from exc
        if row is None:
            return None
        try:
            return int(str(row[0]))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("行情数据库 schema 版本无效") from exc

    def _backup_file(self) -> Path:
        return create_database_backup(self.database_path)

    def backup_database(self) -> Path:
        """Create a recoverable copy before an operator-managed migration."""

        self._require_writer()
        with _WRITE_LOCK:
            self._ensure_initialized()
            return self._backup_file()

    def _require_writer(self) -> None:
        if self.read_only:
            raise RuntimeError("只读行情连接不能写入")

    @staticmethod
    def _normalize_frame(
        frame: pd.DataFrame,
        *,
        market: str,
        code: str,
        instrument_type: InstrumentType,
        board: InstrumentBoard | None = None,
        source: str,
        provider: str | None = None,
    ) -> pd.DataFrame:
        normalized_market = market.strip().upper()
        normalized_code = code.strip()
        if normalized_market not in {"SH", "SZ"}:
            raise ValueError(f"行情市场无效: {market}")
        if re.fullmatch(r"\d{6}", normalized_code) is None:
            raise ValueError(f"行情代码无效: {code}")
        if instrument_type not in _INSTRUMENT_TYPES:
            raise ValueError(f"证券类型无效: {instrument_type}")
        resolved_board = board or classify_board(normalized_market, normalized_code)
        if resolved_board is None or resolved_board not in _INSTRUMENT_BOARDS:
            raise ValueError(f"证券板块无效: {normalized_market} {normalized_code}")
        missing = [column for column in _FRAME_COLUMNS if column not in frame.columns]
        if missing:
            raise ValueError(f"行情数据缺少字段: {', '.join(missing)}")
        normalized = frame.loc[:, _FRAME_COLUMNS].copy()
        normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce")
        if normalized["date"].isna().any():
            raise ValueError("行情数据包含无效日期")
        for column in ("open", "high", "low", "close", "volume", "amount"):
            normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
        numeric_columns = ["open", "high", "low", "close", "volume", "amount"]
        numeric_values = normalized[numeric_columns].to_numpy(dtype=float)
        if not np.isfinite(numeric_values).all():
            raise ValueError("行情数据包含无效数值")
        if not normalized["bar_status"].isin(["completed", "provisional"]).all():
            raise ValueError("行情数据包含无效 K 线状态")
        normalized = normalized.sort_values("date", kind="stable").drop_duplicates(
            subset=["date"], keep="last"
        )
        imported_at = _now()
        result = pd.DataFrame(
            {
                "market": normalized_market,
                "code": normalized_code,
                "instrument_type": instrument_type,
                "board": resolved_board,
                "trade_date": normalized["date"].dt.date,
                "open": normalized["open"].astype(float),
                "high": normalized["high"].astype(float),
                "low": normalized["low"].astype(float),
                "close": normalized["close"].astype(float),
                "volume": normalized["volume"].astype(float),
                "amount": normalized["amount"].astype(float),
                "source": source,
                "provider": provider or ("local" if source == "local" else "tdx"),
                "price_basis": "raw",
                "bar_status": normalized["bar_status"].astype(str),
                "imported_at": imported_at,
            }
        )
        return result[_BAR_COLUMNS].reset_index(drop=True)

    @staticmethod
    def _frame_from_query(result: pd.DataFrame) -> pd.DataFrame:
        if result.empty:
            return pd.DataFrame(
                columns=[
                    "market",
                    "code",
                    "instrument_type",
                    "board",
                    "date",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "amount",
                    "source",
                    "provider",
                    "price_basis",
                    "bar_status",
                    "imported_at",
                ]
            )
        result = result.rename(columns={"trade_date": "date"})
        result["date"] = pd.to_datetime(result["date"])
        return result

    def _refresh_instrument(
        self,
        connection: duckdb.DuckDBPyConnection,
        market: str,
        code: str,
        instrument_type: InstrumentType,
        board: InstrumentBoard,
        name: str | None = None,
        source_path: str | None = None,
        source_state: str = "active",
        provider: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO instruments (
                market, code, name, instrument_type, board, source_path, first_date, last_date,
                bar_count, source_state, last_provider, updated_at
            )
            SELECT ?, ?, ?, ?, ?, ?, MIN(trade_date), MAX(trade_date), COUNT(*), ?, ?, ?
            FROM daily_bars
            WHERE market = ? AND code = ?
            ON CONFLICT (market, code) DO UPDATE SET
                name = COALESCE(excluded.name, instruments.name),
                instrument_type = excluded.instrument_type,
                board = excluded.board,
                source_path = COALESCE(excluded.source_path, instruments.source_path),
                first_date = excluded.first_date,
                last_date = excluded.last_date,
                bar_count = excluded.bar_count,
                source_state = excluded.source_state,
                last_provider = COALESCE(excluded.last_provider, instruments.last_provider),
                updated_at = excluded.updated_at
            """,
            [
                market,
                code,
                _clean_name(name),
                instrument_type,
                board,
                source_path,
                source_state,
                provider,
                _now(),
                market,
                code,
            ],
        )

    def status(self) -> DataStoreStatus:
        # Every writer in this process holds the same lock for the complete
        # DuckDB transaction.  Taking it before opening a read-only connection
        # prevents the status poller from racing a writer and turning a normal
        # progress update into a misleading "database cannot be opened" error.
        with _WRITE_LOCK, self._connect(read_only=True) as connection:
            instrument_row = connection.execute("SELECT COUNT(*) FROM instruments").fetchone()
            bar_row = connection.execute("SELECT COUNT(*) FROM daily_bars").fetchone()
            date_row = connection.execute(
                "SELECT MIN(trade_date), MAX(trade_date) "
                "FROM daily_bars WHERE bar_status = 'completed'"
            ).fetchone()
            instrument_count = int(instrument_row[0]) if instrument_row else 0
            bar_count = int(bar_row[0]) if bar_row else 0
            first, last = date_row if date_row else (None, None)
            online_sync = connection.execute(
                "SELECT value FROM data_meta WHERE key = 'last_online_sync_at'"
            ).fetchone()
            online_provider = connection.execute(
                "SELECT value FROM data_meta WHERE key = 'last_online_provider'"
            ).fetchone()
            online_provider_version = connection.execute(
                "SELECT value FROM data_meta WHERE key = 'last_online_provider_version'"
            ).fetchone()
        return DataStoreStatus(
            database_path=str(self.database_path),
            schema_version=SCHEMA_VERSION,
            instrument_count=instrument_count,
            bar_count=bar_count,
            data_start=_date_text(first),
            data_end=_date_text(last),
            last_online_sync_at=str(online_sync[0]) if online_sync else None,
            last_online_provider=str(online_provider[0]) if online_provider else None,
            last_online_provider_version=(
                str(online_provider_version[0]) if online_provider_version else None
            ),
        )

    def get_store_status(self) -> DataStoreStatus:
        """Protocol-friendly alias for the public store status snapshot."""

        return self.status()

    def set_meta(self, key: str, value: str) -> None:
        self._require_writer()
        with _WRITE_LOCK, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO data_meta (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT (key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                [key, value, _now()],
            )

    def get_meta(self, key: str) -> str | None:
        """Read one metadata value without exposing the DuckDB connection."""

        with self._connect(read_only=True) as connection:
            row = connection.execute(
                "SELECT value FROM data_meta WHERE key = ?",
                [key],
            ).fetchone()
        return str(row[0]) if row else None


__all__ = [
    "SCHEMA_VERSION",
    "SCREEN_HISTORY_LIMIT",
    "STRATEGY_DISCOVERY_HISTORY_LIMIT",
    "BACKTEST_HISTORY_LIMIT",
    "_BACKTEST_HISTORY_TYPES",
    "BacktestHistoryType",
    "MarketDataStoreError",
    "_BAR_COLUMNS",
    "_WRITE_LOCK",
    "_DuckDbStoreBase",
    "_append_filter",
    "_clean_name",
    "_date_text",
    "_now",
    "_timestamp_text",
    "default_data_dir",
    "default_database_path",
]
