"""Durable per-symbol automatic strategies and objective-specific overrides."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Literal, cast

import duckdb

from aquest.market_data.store import DuckDbMarketDataStore
from aquest.market_data.store_base import _WRITE_LOCK

from .tracking_store import PoolTrackingStore

Objective = Literal["annual_return", "sharpe"]

#: 研究范围偏好只有一行，固定主键。
RESEARCH_SETTINGS_ROW_ID = 1

#: 特别关注上限：面板要一眼看得完，不能退化成第二个全市场列表。
FOCUS_LIMIT = 100
#: 关注名单里的名称只是策略池暂时无此标的时的展示用快照，与股票组成员同规格截断。
FOCUS_NAME_MAX_LENGTH = 60


class DuckDbStrategyPoolStore:
    """Store compact winners, hand edits, and per-symbol research status."""

    def __init__(self, store: DuckDbMarketDataStore) -> None:
        self._store = store

    def initialize(self) -> None:
        # Main database initialization creates the table; this additive DDL upgrades old pools.
        with self._connection() as connection:
            connection.execute("SELECT 1 FROM strategy_pool_entries LIMIT 0")
            connection.execute(
                "ALTER TABLE strategy_pool_entries ADD COLUMN IF NOT EXISTS benchmark_json VARCHAR"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS strategy_pool_research_settings (
                    id INTEGER NOT NULL,
                    boards_json VARCHAR NOT NULL,
                    exclude_st BOOLEAN NOT NULL DEFAULT FALSE,
                    updated_at TIMESTAMP NOT NULL,
                    PRIMARY KEY (id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS strategy_pool_focus (
                    market VARCHAR NOT NULL,
                    code VARCHAR NOT NULL,
                    name VARCHAR NOT NULL DEFAULT '',
                    created_at TIMESTAMP NOT NULL,
                    PRIMARY KEY (market, code)
                )
                """
            )

    def read_research_settings(self) -> dict[str, object] | None:
        """读取保存的研究范围偏好；没有保存过或内容损坏时返回 None。"""

        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT boards_json, exclude_st, updated_at
                FROM strategy_pool_research_settings WHERE id = ?
                """,
                [RESEARCH_SETTINGS_ROW_ID],
            ).fetchone()
        if row is None:
            return None
        try:
            boards = json.loads(str(row[0]))
        except (TypeError, ValueError):
            return None
        if not isinstance(boards, list) or not boards:
            return None
        return {"boards": boards, "exclude_st": bool(row[1])}

    def save_research_settings(
        self, *, boards: Sequence[str], exclude_st: bool
    ) -> dict[str, object]:
        """单行 upsert 保存研究范围偏好，返回规范化后的值。"""

        normalized = sorted(set(boards))
        boards_json = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with _WRITE_LOCK, self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO strategy_pool_research_settings
                    (id, boards_json, exclude_st, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    boards_json = excluded.boards_json,
                    exclude_st = excluded.exclude_st,
                    updated_at = excluded.updated_at
                """,
                [RESEARCH_SETTINGS_ROW_ID, boards_json, bool(exclude_st), now],
            )
        return {"boards": normalized, "exclude_st": bool(exclude_st)}

    def read_focus(self) -> list[dict[str, object]]:
        """特别关注名单，按关注时间正序；一只都没关注时返回空列表。"""

        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT market, code, name FROM strategy_pool_focus
                ORDER BY created_at, market, code
                """
            ).fetchall()
        return [{"market": row[0], "code": row[1], "name": row[2]} for row in rows]

    def add_focus(self, market: str, code: str, name: str = "") -> list[dict[str, object]]:
        """关注一只标的；重复关注幂等返回当前名单，超出上限时抛错。"""

        _validate_symbol(market, code)
        clean_name = str(name or "").strip()[:FOCUS_NAME_MAX_LENGTH]
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with _WRITE_LOCK, self._transaction() as connection:
            already = connection.execute(
                "SELECT 1 FROM strategy_pool_focus WHERE market = ? AND code = ?",
                [market, code],
            ).fetchone()
            if already is None:
                counted = connection.execute("SELECT COUNT(*) FROM strategy_pool_focus").fetchone()
                if int(counted[0] if counted else 0) >= FOCUS_LIMIT:
                    raise ValueError(f"特别关注最多 {FOCUS_LIMIT} 只。")
                connection.execute(
                    """
                    INSERT INTO strategy_pool_focus (market, code, name, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    [market, code, clean_name, now],
                )
        return self.read_focus()

    def remove_focus(self, market: str, code: str) -> list[dict[str, object]]:
        """取消关注；没有关注过时同样幂等返回当前名单。"""

        _validate_symbol(market, code)
        with _WRITE_LOCK, self._transaction() as connection:
            connection.execute(
                "DELETE FROM strategy_pool_focus WHERE market = ? AND code = ?",
                [market, code],
            )
        return self.read_focus()

    def get(self, market: str, code: str) -> dict[str, object] | None:
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT market, code, name, annual_candidate_json, sharpe_candidate_json,
                       manual_annual_json, manual_sharpe_json, status, last_error,
                       data_fingerprint, market_fingerprint, config_fingerprint,
                       source_job_id, updated_at, benchmark_json
                FROM strategy_pool_entries WHERE market = ? AND code = ?
                """,
                [market, code],
            ).fetchone()
        return _row_to_entry(row) if row else None

    def list_entries(self) -> list[dict[str, object]]:
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT market, code, name, annual_candidate_json, sharpe_candidate_json,
                       manual_annual_json, manual_sharpe_json, status, last_error,
                       data_fingerprint, market_fingerprint, config_fingerprint,
                       source_job_id, updated_at, benchmark_json
                FROM strategy_pool_entries ORDER BY market, code
                """
            ).fetchall()
        return [_row_to_entry(row) for row in rows]

    def upsert_automatic(
        self,
        *,
        market: str,
        code: str,
        name: str,
        annual_candidate: Mapping[str, object] | None,
        sharpe_candidate: Mapping[str, object] | None,
        data_fingerprint: str,
        config_fingerprint: str,
        source_job_id: str,
        market_fingerprint: str = "",
        benchmark: Mapping[str, object] | None = None,
    ) -> None:
        _validate_symbol(market, code)
        candidates = [annual_candidate, sharpe_candidate]
        status = (
            "ready"
            if all(candidate is not None for candidate in candidates)
            else "partial"
            if any(candidate is not None for candidate in candidates)
            else "no_strategy"
        )
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with _WRITE_LOCK, self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO strategy_pool_entries (
                    market, code, name, annual_candidate_json, sharpe_candidate_json,
                    status, last_error, data_fingerprint, market_fingerprint, config_fingerprint,
                    source_job_id, updated_at, benchmark_json
                ) VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?)
                ON CONFLICT (market, code) DO UPDATE SET
                    name = excluded.name,
                    annual_candidate_json = excluded.annual_candidate_json,
                    sharpe_candidate_json = excluded.sharpe_candidate_json,
                    status = excluded.status,
                    last_error = '',
                    data_fingerprint = excluded.data_fingerprint,
                    market_fingerprint = excluded.market_fingerprint,
                    config_fingerprint = excluded.config_fingerprint,
                    source_job_id = excluded.source_job_id,
                    updated_at = excluded.updated_at,
                    benchmark_json = excluded.benchmark_json
                """,
                [
                    market,
                    code,
                    name,
                    _dump_json(annual_candidate),
                    _dump_json(sharpe_candidate),
                    status,
                    data_fingerprint,
                    market_fingerprint,
                    config_fingerprint,
                    source_job_id,
                    now,
                    _dump_json(benchmark),
                ],
            )
            PoolTrackingStore.sync_entry(connection, market, code)

    def update_benchmark(
        self,
        market: str,
        code: str,
        benchmark: Mapping[str, object],
    ) -> None:
        """Backfill benchmark metrics without rewriting saved strategy choices."""

        _validate_symbol(market, code)
        with _WRITE_LOCK, self._transaction() as connection:
            connection.execute(
                "UPDATE strategy_pool_entries SET benchmark_json = ?, updated_at = ? "
                "WHERE market = ? AND code = ?",
                [
                    _dump_json(benchmark),
                    datetime.now(timezone.utc).replace(tzinfo=None),
                    market,
                    code,
                ],
            )

    def record_symbol_error(
        self,
        market: str,
        code: str,
        error: str,
        *,
        source_job_id: str,
        status: str = "failed",
        data_fingerprint: str | None = None,
        market_fingerprint: str | None = None,
        config_fingerprint: str | None = None,
    ) -> None:
        _validate_symbol(market, code)
        if status not in {"failed", "insufficient_history"}:
            raise ValueError("策略池标的错误状态无效")
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with _WRITE_LOCK, self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO strategy_pool_entries (
                    market, code, name, status, last_error, source_job_id, updated_at
                    , data_fingerprint, market_fingerprint, config_fingerprint
                ) VALUES (?, ?, '', ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (market, code) DO UPDATE SET
                    status = excluded.status, last_error = excluded.last_error,
                    source_job_id = excluded.source_job_id, updated_at = excluded.updated_at,
                    data_fingerprint = COALESCE(
                        NULLIF(excluded.data_fingerprint, ''),
                        strategy_pool_entries.data_fingerprint
                    ),
                    market_fingerprint = COALESCE(
                        NULLIF(excluded.market_fingerprint, ''),
                        strategy_pool_entries.market_fingerprint
                    ),
                    config_fingerprint = COALESCE(
                        NULLIF(excluded.config_fingerprint, ''),
                        strategy_pool_entries.config_fingerprint
                    ),
                    annual_candidate_json = CASE
                        WHEN excluded.status = 'insufficient_history'
                          OR (
                              excluded.config_fingerprint <> ''
                              AND excluded.config_fingerprint
                                  <> strategy_pool_entries.config_fingerprint
                          )
                        THEN NULL ELSE strategy_pool_entries.annual_candidate_json END,
                    sharpe_candidate_json = CASE
                        WHEN excluded.status = 'insufficient_history'
                          OR (
                              excluded.config_fingerprint <> ''
                              AND excluded.config_fingerprint
                                  <> strategy_pool_entries.config_fingerprint
                          )
                        THEN NULL ELSE strategy_pool_entries.sharpe_candidate_json END
                """,
                [
                    market,
                    code,
                    status,
                    error[:500],
                    source_job_id,
                    now,
                    data_fingerprint or "",
                    market_fingerprint or "",
                    config_fingerprint or "",
                ],
            )
            PoolTrackingStore.sync_entry(connection, market, code)

    def persist_worker_entry(self, entry: Mapping[str, object], *, source_job_id: str) -> None:
        if entry.get("reset") is True:
            with _WRITE_LOCK, self._transaction() as connection:
                PoolTrackingStore.archive_all(connection)
                connection.execute("DELETE FROM strategy_pool_entries")
            return
        market = str(entry.get("market") or "").upper()
        code = str(entry.get("code") or "")
        name = str(entry.get("name") or "")
        error = entry.get("error")
        if isinstance(error, str) and error:
            self.record_symbol_error(
                market,
                code,
                error,
                source_job_id=source_job_id,
                status=str(entry.get("status") or "failed"),
                data_fingerprint=(
                    str(entry["data_fingerprint"])
                    if entry.get("data_fingerprint") is not None
                    else None
                ),
                market_fingerprint=(
                    str(entry["market_fingerprint"])
                    if entry.get("market_fingerprint") is not None
                    else None
                ),
                config_fingerprint=(
                    str(entry["config_fingerprint"])
                    if entry.get("config_fingerprint") is not None
                    else None
                ),
            )
            return
        if entry.get("benchmark_only"):
            benchmark = entry.get("benchmark")
            if isinstance(benchmark, Mapping):
                self.update_benchmark(market, code, benchmark)
            return
        annual = entry.get("annual_candidate")
        sharpe = entry.get("sharpe_candidate")
        benchmark = entry.get("benchmark")
        self.upsert_automatic(
            market=market,
            code=code,
            name=name,
            annual_candidate=annual if isinstance(annual, Mapping) else None,
            sharpe_candidate=sharpe if isinstance(sharpe, Mapping) else None,
            data_fingerprint=str(entry.get("data_fingerprint") or ""),
            market_fingerprint=str(entry.get("market_fingerprint") or ""),
            config_fingerprint=str(entry.get("config_fingerprint") or ""),
            source_job_id=source_job_id,
            benchmark=benchmark if isinstance(benchmark, Mapping) else None,
        )

    def set_manual_override(
        self,
        market: str,
        code: str,
        objective: Objective,
        candidate: Mapping[str, object],
        benchmark: Mapping[str, object] | None = None,
    ) -> None:
        _validate_symbol(market, code)
        _validate_objective(objective)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        column = "manual_annual_json" if objective == "annual_return" else "manual_sharpe_json"
        with _WRITE_LOCK, self._transaction() as connection:
            connection.execute(
                f"""
                INSERT INTO strategy_pool_entries (
                    market, code, name, {column}, benchmark_json, status, updated_at
                ) VALUES (?, ?, '', ?, ?, 'partial', ?)
                ON CONFLICT (market, code) DO UPDATE SET
                    {column} = excluded.{column},
                    benchmark_json = COALESCE(
                        excluded.benchmark_json, strategy_pool_entries.benchmark_json
                    ),
                    updated_at = excluded.updated_at
                """,
                [market, code, _dump_json(candidate), _dump_json(benchmark), now],
            )

            PoolTrackingStore.sync_entry(connection, market, code)

    def clear_manual_override(self, market: str, code: str, objective: Objective) -> None:
        _validate_symbol(market, code)
        _validate_objective(objective)
        column = "manual_annual_json" if objective == "annual_return" else "manual_sharpe_json"
        with _WRITE_LOCK, self._transaction() as connection:
            connection.execute(
                f"UPDATE strategy_pool_entries SET {column} = NULL, updated_at = ? "
                "WHERE market = ? AND code = ?",
                [datetime.now(timezone.utc).replace(tzinfo=None), market, code],
            )
            PoolTrackingStore.sync_entry(connection, market, code)

    def active_candidate(
        self, market: str, code: str, objective: Objective
    ) -> dict[str, object] | None:
        _validate_objective(objective)
        item = self.get(market, code)
        if item is None:
            return None
        overrides = cast(Mapping[str, object], item["manual_overrides"])
        automatic = cast(Mapping[str, object], item["automatic_candidates"])
        candidate = overrides.get(objective) or automatic.get(objective)
        if isinstance(candidate, Mapping) and isinstance(candidate.get("candidate_ref"), str):
            reference = candidate["candidate_ref"]
            candidate = automatic.get(reference)
        return dict(candidate) if isinstance(candidate, Mapping) else None

    @contextmanager
    def _connection(self) -> Iterator[duckdb.DuckDBPyConnection]:
        connection = self._store._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _read_connection(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """读策略只有一份：市场库的 `read_session`（只读优先 + 被自己打回就退回读写）。

        这里以前自己决定退路，而任务表的同名方法只重试、不退路——同一个锁压力下
        一个能读一个硬失败。策略收口到 `store_base` 之后，卫星库不再各自理解 DuckDB
        的锁语义。
        """

        with self._store.read_session() as connection:
            yield connection

    @contextmanager
    def _transaction(self) -> Iterator[duckdb.DuckDBPyConnection]:
        with self._store.write_transaction() as connection:
            yield connection


def _row_to_entry(row: object) -> dict[str, object]:
    if not isinstance(row, tuple):
        raise ValueError("个股策略池记录格式无效")
    return {
        "market": row[0],
        "code": row[1],
        "name": row[2],
        "automatic_candidates": {
            "annual_return": _load_json(row[3]),
            "sharpe": _load_json(row[4]),
        },
        "manual_overrides": {
            "annual_return": _load_json(row[5]),
            "sharpe": _load_json(row[6]),
        },
        "status": row[7],
        "last_error": row[8],
        "data_fingerprint": row[9],
        "market_fingerprint": row[10],
        "config_fingerprint": row[11],
        "source_job_id": row[12],
        "updated_at": str(row[13]) if row[13] is not None else None,
        "benchmark": _load_json(row[14]),
    }


def _dump_json(value: Mapping[str, object] | None) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(dict(value), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("个股策略候选无法序列化") from exc


def _load_json(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    try:
        payload = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("个股策略候选 JSON 无效") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("个股策略候选必须是对象")
    return {str(key): cast(object, item) for key, item in payload.items()}


def _validate_symbol(market: str, code: str) -> None:
    if market not in {"SH", "SZ"} or len(code) != 6 or not code.isdigit():
        raise ValueError("策略池证券代码无效")


def _validate_objective(objective: str) -> None:
    if objective not in {"annual_return", "sharpe"}:
        raise ValueError("策略池优化目标无效")


__all__ = ["DuckDbStrategyPoolStore", "Objective"]
