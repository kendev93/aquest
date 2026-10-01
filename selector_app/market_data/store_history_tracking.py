"""Persistence methods for the tracking domain."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping

from .store_base import _WRITE_LOCK, _DuckDbStoreBase, _now
from .store_history_common import (
    _STRATEGY_COLUMNS,
    SAVED_STRATEGY_TABLE,
    STRATEGY_TRACKING_ACTIVE,
    STRATEGY_TRACKING_OFF,
    STRATEGY_TRACKING_PLAN_TABLE,
    STRATEGY_TRACKING_TABLE,
    STRATEGY_TRACKING_TRADES_TABLE,
    _decode_strategy_row,
    _tracking_date,
    _trade_date_to_iso,
    _write_tracking_plan,
    _write_tracking_points,
    _write_tracking_trades,
)
from .store_history_strategies import StrategyHistoryMixin


class StrategyTrackingHistoryMixin(StrategyHistoryMixin):
    _store: _DuckDbStoreBase

    def start_strategy_tracking(
        self,
        strategy_id: str,
        *,
        launched_at: int,
        seed_points: Iterable[Mapping[str, object]] = (),
    ) -> dict[str, object] | None:
        """Put one strategy online and seed the hand-over day.

        上线日那一行既是研究段的终点也是观察段的起点，两边共用它——曲线才
        接得上，衰减比也才有统一的零点。
        """

        self._store._require_writer()
        normalized_id = strategy_id.strip()
        if not normalized_id:
            return None
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
            try:
                updated = connection.execute(
                    f"""
                    UPDATE {SAVED_STRATEGY_TABLE} SET
                        tracking_status = ?,
                        launched_at = ?,
                        last_tracked_date = ?,
                        last_error = '',
                        tracking_state_json = NULL,
                        updated_at = ?
                    WHERE strategy_id = ? RETURNING strategy_id
                    """,
                    [
                        STRATEGY_TRACKING_ACTIVE,
                        _trade_date_to_iso(launched_at),
                        launched_at,
                        _now(),
                        normalized_id,
                    ],
                ).fetchone()
                if updated is not None:
                    _write_tracking_points(connection, normalized_id, seed_points)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        if updated is None:
            return None
        return self.read_strategy(normalized_id)

    def finish_strategy_tracking(
        self,
        strategy_id: str,
        *,
        last_tracked_date: int | None = None,
        error: str | None = None,
    ) -> dict[str, object] | None:
        """Record one rerun outcome.

        失败时只写 ``last_error`` 并**保留旧的** ``last_tracked_date``——
        停在旧日期比静默写一条"成功"重要得多，否则用户会以为今天也算过了。
        """

        self._store._require_writer()
        normalized_id = strategy_id.strip()
        if not normalized_id:
            return None
        assignments = ["updated_at = ?"]
        parameters: list[object] = [_now()]
        if error:
            assignments.append("last_error = ?")
            parameters.append(str(error).strip()[:500])
        else:
            assignments.append("last_error = ''")
            if last_tracked_date is not None:
                assignments.append("last_tracked_date = ?")
                parameters.append(int(last_tracked_date))
        parameters.append(normalized_id)
        parameters.append(STRATEGY_TRACKING_ACTIVE)
        with _WRITE_LOCK, self._store._connect() as connection:
            updated = connection.execute(
                f"UPDATE {SAVED_STRATEGY_TABLE} SET {', '.join(assignments)} "
                "WHERE strategy_id = ? AND tracking_status = ? RETURNING strategy_id",
                parameters,
            ).fetchone()
        if updated is None:
            return None
        return self.read_strategy(normalized_id)

    def stop_strategy_tracking(self, strategy_id: str) -> dict[str, object] | None:
        """Take one strategy offline, keeping the observations it already made."""

        self._store._require_writer()
        normalized_id = strategy_id.strip()
        if not normalized_id:
            return None
        with _WRITE_LOCK, self._store._connect() as connection:
            updated = connection.execute(
                f"""
                UPDATE {SAVED_STRATEGY_TABLE} SET
                    tracking_status = ?,
                    last_error = '',
                    updated_at = ?
                WHERE strategy_id = ? AND tracking_status <> ?
                RETURNING strategy_id
                """,
                [
                    STRATEGY_TRACKING_OFF,
                    _now(),
                    normalized_id,
                    STRATEGY_TRACKING_OFF,
                ],
            ).fetchone()
            if updated is not None:
                # 下线之后「明天打算买卖什么」就没有意义了，留着会变成一张还
                # 挂在页面上的过期单子。观察段和成交明细照旧保留。
                connection.execute(
                    f"DELETE FROM {STRATEGY_TRACKING_PLAN_TABLE} WHERE strategy_id = ?",
                    [normalized_id],
                )
        if updated is None:
            return None
        return self.read_strategy(normalized_id)

    def save_tracking_points(
        self,
        strategy_id: str,
        points: Iterable[Mapping[str, object]],
    ) -> int:
        """Upsert the daily observation rows of one strategy.

        按 (策略, 日期) 覆盖写，所以同一天重跑几次结果都一样——这就是幂等的
        来源，也是"机器关机漏跑几天、开机后一次补齐"能成立的原因。
        """

        self._store._require_writer()
        normalized_id = strategy_id.strip()
        if not normalized_id:
            return 0
        rows = list(points)
        if not rows:
            return 0
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
            try:
                written = _write_tracking_points(connection, normalized_id, rows)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return written

    def save_strategy_tracking_snapshot(
        self,
        strategy_id: str,
        *,
        points: Iterable[Mapping[str, object]],
        trades: Iterable[Mapping[str, object]],
        plan: Mapping[str, object] | None,
        last_tracked_date: int,
        summary: Mapping[str, object] | None = None,
        tracking_state: Mapping[str, object] | None = None,
    ) -> bool:
        """Commit one complete refresh only while the strategy is still online.

        The active check and all writes share one transaction and the same lock
        as ``stop_strategy_tracking``. A stop therefore either happens before
        this snapshot (and prevents it) or after it (and clears its plan).
        Missing plans are deleted so an older order cannot survive a no-candidate
        result.
        """

        self._store._require_writer()
        normalized_id = strategy_id.strip()
        if not normalized_id:
            return False
        prepared_points = list(points)
        prepared_trades = list(trades)
        normalized_date = _tracking_date(last_tracked_date)
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
            try:
                row = connection.execute(
                    f"SELECT tracking_status FROM {SAVED_STRATEGY_TABLE} WHERE strategy_id = ?",
                    [normalized_id],
                ).fetchone()
                if row is None or str(row[0]) != STRATEGY_TRACKING_ACTIVE:
                    connection.execute("ROLLBACK")
                    return False

                _write_tracking_points(connection, normalized_id, prepared_points)
                _write_tracking_trades(connection, normalized_id, prepared_trades)
                _write_tracking_plan(connection, normalized_id, plan)
                state_parameter = (
                    json.dumps(dict(tracking_state), ensure_ascii=False, separators=(",", ":"))
                    if tracking_state is not None
                    else None
                )
                summary_parameter = (
                    json.dumps(dict(summary), ensure_ascii=False, separators=(",", ":"))
                    if summary is not None
                    else None
                )
                if summary is None:
                    if state_parameter is None:
                        updated = connection.execute(
                            f"""
                            UPDATE {SAVED_STRATEGY_TABLE} SET
                                last_tracked_date = ?, last_error = '', updated_at = ?
                            WHERE strategy_id = ? AND tracking_status = ?
                            RETURNING strategy_id
                            """,
                            [normalized_date, _now(), normalized_id, STRATEGY_TRACKING_ACTIVE],
                        ).fetchone()
                    else:
                        updated = connection.execute(
                            f"""
                            UPDATE {SAVED_STRATEGY_TABLE} SET
                                tracking_state_json = ?, last_tracked_date = ?,
                                last_error = '', updated_at = ?
                            WHERE strategy_id = ? AND tracking_status = ?
                            RETURNING strategy_id
                            """,
                            [
                                state_parameter,
                                normalized_date,
                                _now(),
                                normalized_id,
                                STRATEGY_TRACKING_ACTIVE,
                            ],
                        ).fetchone()
                else:
                    if state_parameter is None:
                        updated = connection.execute(
                            f"""
                            UPDATE {SAVED_STRATEGY_TABLE} SET
                                summary_json = ?, last_tracked_date = ?,
                                last_error = '', updated_at = ?
                            WHERE strategy_id = ? AND tracking_status = ?
                            RETURNING strategy_id
                            """,
                            [
                                summary_parameter,
                                normalized_date,
                                _now(),
                                normalized_id,
                                STRATEGY_TRACKING_ACTIVE,
                            ],
                        ).fetchone()
                    else:
                        updated = connection.execute(
                            f"""
                            UPDATE {SAVED_STRATEGY_TABLE} SET
                                summary_json = ?, tracking_state_json = ?, last_tracked_date = ?,
                                last_error = '', updated_at = ?
                            WHERE strategy_id = ? AND tracking_status = ?
                            RETURNING strategy_id
                            """,
                            [
                                summary_parameter,
                                state_parameter,
                                normalized_date,
                                _now(),
                                normalized_id,
                                STRATEGY_TRACKING_ACTIVE,
                            ],
                        ).fetchone()
                if updated is None:
                    raise RuntimeError("策略追踪状态已变化，未保存本次重算")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return True

    def list_tracking_points(
        self,
        strategy_id: str,
        *,
        since: int | None = None,
    ) -> list[dict[str, object]]:
        """Read the observation rows of one strategy, oldest first."""

        normalized_id = strategy_id.strip()
        if not normalized_id:
            return []
        statement = (
            "SELECT trade_date, equity, cash, positions_count, benchmark_value, drawdown "
            f"FROM {STRATEGY_TRACKING_TABLE} WHERE strategy_id = ?"
        )
        parameters: list[object] = [normalized_id]
        if since is not None:
            statement += " AND trade_date >= ?"
            parameters.append(int(since))
        statement += " ORDER BY trade_date"
        with self._store._connect(read_only=True) as connection:
            rows = connection.execute(statement, parameters).fetchall()
        return [
            {
                "trade_date": int(row[0]),
                "equity": float(row[1]),
                "cash": float(row[2]),
                "positions_count": int(row[3]),
                "benchmark_value": None if row[4] is None else float(row[4]),
                "drawdown": None if row[5] is None else float(row[5]),
            }
            for row in rows
        ]

    def save_tracking_trades(
        self,
        strategy_id: str,
        rows: Iterable[Mapping[str, object]],
    ) -> int:
        """全量覆盖一个策略的成交明细。

        不增量合并：明细和净值一样出自同一次模拟，重跑一遍就是新的完整答案，
        增量合并只会留下上次跑出来的孤儿行（比如行情补齐后消失的那笔成交）。
        """

        self._store._require_writer()
        normalized_id = strategy_id.strip()
        if not normalized_id:
            return 0
        prepared = list(rows)
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
            try:
                _write_tracking_trades(connection, normalized_id, prepared)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return len(prepared)

    def list_tracking_trades(
        self,
        strategy_id: str,
    ) -> list[dict[str, object]]:
        """Read the trades of one strategy in the order they happened."""

        normalized_id = strategy_id.strip()
        if not normalized_id:
            return []
        with self._store._connect(read_only=True) as connection:
            rows = connection.execute(
                "SELECT trade_date, phase, direction, market, code, name, size, price, "
                f"pnl, reason, signal_date, commission, stamp_tax, slippage, cost_basis "
                f"FROM {STRATEGY_TRACKING_TRADES_TABLE} "
                "WHERE strategy_id = ? ORDER BY seq",
                [normalized_id],
            ).fetchall()
        return [
            {
                "trade_date": int(row[0]),
                "phase": str(row[1]),
                "direction": str(row[2]),
                "market": str(row[3]),
                "code": str(row[4]),
                "name": str(row[5]),
                "size": float(row[6]),
                "price": float(row[7]),
                "pnl": float(row[8]),
                "reason": str(row[9]),
                "signal_date": None if row[10] is None else int(row[10]),
                "commission": None if row[11] is None else float(row[11]),
                "stamp_tax": None if row[12] is None else float(row[12]),
                "slippage": None if row[13] is None else float(row[13]),
                "cost_basis": None if row[14] is None else float(row[14]),
            }
            for row in rows
        ]

    def page_tracking_trades(
        self,
        strategy_id: str,
        *,
        phase: str | None = None,
        date_from: int | None = None,
        date_to: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, object]:
        """Return a bounded, newest-first page of actual fills and its total."""

        normalized_id = strategy_id.strip()
        if not normalized_id:
            return {"rows": [], "total": 0, "limit": limit, "offset": offset}
        if not 1 <= limit <= 200 or offset < 0:
            raise ValueError("成交分页参数超出范围")
        if phase not in {None, "pre", "post"}:
            raise ValueError("成交阶段无效")
        if date_from is not None and date_to is not None and date_from > date_to:
            raise ValueError("成交日期范围无效")
        conditions = ["strategy_id = ?"]
        parameters: list[object] = [normalized_id]
        if phase is not None:
            conditions.append("phase = ?")
            parameters.append(phase)
        if date_from is not None:
            conditions.append("trade_date >= ?")
            parameters.append(int(date_from))
        if date_to is not None:
            conditions.append("trade_date <= ?")
            parameters.append(int(date_to))
        where = " AND ".join(conditions)
        with self._store._connect(read_only=True) as connection:
            count_row = connection.execute(
                f"SELECT COUNT(*) FROM {STRATEGY_TRACKING_TRADES_TABLE} WHERE {where}",
                parameters,
            ).fetchone()
            total = int(count_row[0]) if count_row is not None else 0
            rows = connection.execute(
                "SELECT trade_date, phase, direction, market, code, name, size, price, "
                f"pnl, reason, signal_date, commission, stamp_tax, slippage, cost_basis "
                f"FROM {STRATEGY_TRACKING_TRADES_TABLE} WHERE {where} "
                "ORDER BY trade_date DESC, seq DESC LIMIT ? OFFSET ?",
                [*parameters, limit, offset],
            ).fetchall()
        packed = [
            {
                "trade_date": int(row[0]),
                "phase": str(row[1]),
                "direction": str(row[2]),
                "market": str(row[3]),
                "code": str(row[4]),
                "name": str(row[5]),
                "size": float(row[6]),
                "price": float(row[7]),
                "pnl": float(row[8]),
                "reason": str(row[9]),
                "signal_date": None if row[10] is None else int(row[10]),
                "commission": None if row[11] is None else float(row[11]),
                "stamp_tax": None if row[12] is None else float(row[12]),
                "slippage": None if row[13] is None else float(row[13]),
                "cost_basis": None if row[14] is None else float(row[14]),
            }
            for row in rows
        ]
        return {"rows": packed, "total": total, "limit": limit, "offset": offset}

    def save_tracking_plan(
        self,
        strategy_id: str,
        payload: Mapping[str, object],
    ) -> None:
        """Upsert the next-session plan of one strategy.

        只留最新一份：它是"截至最近一个交易日收盘的估算"，隔天就作废，留历史
        版本只会让人误以为还能照着上周的单去下单。
        """

        self._store._require_writer()
        normalized_id = strategy_id.strip()
        if not normalized_id:
            return
        with _WRITE_LOCK, self._store._connect() as connection:
            _write_tracking_plan(connection, normalized_id, payload)

    def read_tracking_plan(self, strategy_id: str) -> dict[str, object] | None:
        normalized_id = strategy_id.strip()
        if not normalized_id:
            return None
        with self._store._connect(read_only=True) as connection:
            row = connection.execute(
                f"SELECT payload_json FROM {STRATEGY_TRACKING_PLAN_TABLE} WHERE strategy_id = ?",
                [normalized_id],
            ).fetchone()
        if row is None:
            return None
        decoded = json.loads(str(row[0]))
        return decoded if isinstance(decoded, dict) else None

    def list_tracked_strategies(self) -> list[dict[str, object]]:
        """Return every strategy that is currently online."""

        with self._store._connect(read_only=True) as connection:
            rows = connection.execute(
                f"""
                SELECT {_STRATEGY_COLUMNS}
                FROM {SAVED_STRATEGY_TABLE}
                WHERE tracking_status = ?
                ORDER BY launched_at, strategy_id
                """,
                [STRATEGY_TRACKING_ACTIVE],
            ).fetchall()
        records: list[dict[str, object]] = []
        for row in rows:
            payload = _decode_strategy_row(row)
            payload.pop("request", None)
            payload.pop("tracking_state", None)
            records.append(payload)
        return records
