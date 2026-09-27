"""Backtests history persistence methods."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from datetime import datetime

from .store_base import (
    _BACKTEST_HISTORY_TYPES,
    _WRITE_LOCK,
    BACKTEST_HISTORY_LIMIT,
    _DuckDbStoreBase,
    _now,
)
from .store_history_common import (
    _BACKTEST_HISTORY_FULL_COLUMNS,
    _BACKTEST_HISTORY_LIST_COLUMNS,
    _backtest_history_summary,
    _decode_backtest_history_row,
    _decode_full_backtest_history_row,
    _instrument_name_map,
    _normalize_timestamp,
    _validate_backtest_history_type,
)


class BacktestHistoryMixin:
    _store: _DuckDbStoreBase

    def save_backtest_history(
        self,
        history_id: str,
        *,
        backtest_type: str,
        request: Mapping[str, object],
        result: Mapping[str, object],
        started_at: datetime | str,
        finished_at: datetime | str,
        created_at: datetime | str | None = None,
        duration_seconds: float | None = None,
    ) -> None:
        """Persist a completed single, portfolio, or fitness backtest."""

        self._store._require_writer()
        normalized_id = history_id.strip()
        if not normalized_id:
            raise ValueError("回测历史 ID 不能为空")
        if backtest_type not in _BACKTEST_HISTORY_TYPES:
            raise ValueError("回测历史类型无效")
        started = _normalize_timestamp(started_at, _now())
        finished = _normalize_timestamp(finished_at, started)
        if finished < started:
            raise ValueError("回测结束时间不能早于开始时间")
        created = _normalize_timestamp(created_at, started)
        if duration_seconds is None:
            duration = (finished - started).total_seconds()
        else:
            try:
                duration = float(duration_seconds)
            except (TypeError, ValueError) as exc:
                raise ValueError("回测耗时格式无效") from exc
        if not math.isfinite(duration) or duration < 0:
            raise ValueError("回测耗时必须是大于等于 0 的有限数字")
        request_payload = dict(request)
        result_payload = dict(result)
        summary = _backtest_history_summary(result_payload, backtest_type)
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
            try:
                connection.execute(
                    """
                    INSERT INTO backtest_history (
                        history_id, backtest_type, created_at, started_at, finished_at,
                        duration_seconds, request_json, result_json, summary_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (history_id) DO UPDATE SET
                        backtest_type = excluded.backtest_type,
                        created_at = excluded.created_at,
                        started_at = excluded.started_at,
                        finished_at = excluded.finished_at,
                        duration_seconds = excluded.duration_seconds,
                        request_json = excluded.request_json,
                        result_json = excluded.result_json,
                        summary_json = excluded.summary_json
                    """,
                    [
                        normalized_id,
                        backtest_type,
                        created,
                        started,
                        finished,
                        duration,
                        json.dumps(request_payload, ensure_ascii=False, separators=(",", ":")),
                        json.dumps(result_payload, ensure_ascii=False, separators=(",", ":")),
                        json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
                    ],
                )
                stale = connection.execute(
                    """
                    SELECT history_id FROM backtest_history
                    WHERE backtest_type = ?
                    ORDER BY finished_at DESC, history_id DESC
                    LIMIT 100000 OFFSET ?
                    """,
                    [backtest_type, BACKTEST_HISTORY_LIMIT],
                ).fetchall()
                if stale:
                    connection.executemany(
                        "DELETE FROM backtest_history WHERE history_id = ?", stale
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def save_research_trial(
        self,
        history_id: str,
        *,
        data_fingerprint: str,
        start_date: str,
        end_date: str,
        tested_conditions: int,
        created_at: datetime | str | None = None,
    ) -> None:
        """Record one completed condition-research study in the trial ledger.

        The ledger is what makes "you have now run this study N times" auditable.
        It stores just enough to decide whether two studies are the *same* trial
        (their data window) plus how many conditions they searched, so the
        caller can compute an independent-round count.
        """

        self._store._require_writer()
        normalized_id = history_id.strip()
        if not normalized_id:
            raise ValueError("研究历史 ID 不能为空")
        if not start_date or not end_date:
            raise ValueError("研究区间不能为空")
        if start_date > end_date:
            raise ValueError("研究开始日期不能晚于结束日期")
        try:
            count = int(tested_conditions)
        except (TypeError, ValueError) as exc:
            raise ValueError("检验条件数必须是整数") from exc
        if count < 0:
            raise ValueError("检验条件数不能为负数")
        created = _normalize_timestamp(created_at, _now())
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
            try:
                connection.execute(
                    """
                    INSERT INTO condition_research_trials (
                        history_id, created_at, data_fingerprint,
                        start_date, end_date, tested_conditions
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (history_id) DO UPDATE SET
                        created_at = excluded.created_at,
                        data_fingerprint = excluded.data_fingerprint,
                        start_date = excluded.start_date,
                        end_date = excluded.end_date,
                        tested_conditions = excluded.tested_conditions
                    """,
                    [
                        normalized_id,
                        created,
                        str(data_fingerprint),
                        str(start_date),
                        str(end_date),
                        count,
                    ],
                )
                # 这里**不截断**。台账要的是累计值：一轮研究就算后来被历史淘汰了，
                # 用户当时看过它给的最好结果这件事不会跟着消失，"取 K 轮里最好的"
                # 那次选择偏差也就还在。跟着 backtest_history 一起截到 20 条会让 K
                # 少算、Šidák 校正不足，正好是把 #20 要防的东西又放回去。想从零开始
                # 只能显式重置（reset_research_trials）。
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def list_research_trials(self) -> list[dict[str, object]]:
        """Return every recorded trial, oldest first."""

        with self._store._connect(read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT history_id, created_at, data_fingerprint,
                       start_date, end_date, tested_conditions
                FROM condition_research_trials
                ORDER BY created_at ASC, history_id ASC
                """
            ).fetchall()
        return [
            {
                "history_id": str(row[0]),
                "created_at": row[1],
                "data_fingerprint": str(row[2]),
                "start_date": str(row[3]),
                "end_date": str(row[4]),
                "tested_conditions": int(row[5]),
            }
            for row in rows
        ]

    def reset_research_trials(self) -> int:
        """Clear the ledger — the only way to start counting from zero.

        Returns how many entries were dropped so the caller can report it.
        """

        self._store._require_writer()
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
            try:
                total = connection.execute(
                    "SELECT COUNT(*) FROM condition_research_trials"
                ).fetchone()
                removed = int(total[0]) if total else 0
                connection.execute("DELETE FROM condition_research_trials")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return removed

    def list_backtest_history(self, backtest_type: str) -> list[dict[str, object]]:
        """Return recent summaries for one kind of backtest."""

        _validate_backtest_history_type(backtest_type)
        with self._store._connect(read_only=True) as connection:
            rows = connection.execute(
                f"""
                SELECT {_BACKTEST_HISTORY_LIST_COLUMNS}
                FROM backtest_history
                WHERE backtest_type = ?
                ORDER BY finished_at DESC, history_id DESC
                LIMIT ?
                """,
                [backtest_type, BACKTEST_HISTORY_LIMIT],
            ).fetchall()
            instrument_names = _instrument_name_map(connection)
        return [_decode_backtest_history_row(row, instrument_names, backtest_type) for row in rows]

    def list_backtest_history_all(
        self,
        *,
        limit_per_type: int = BACKTEST_HISTORY_LIMIT,
    ) -> list[dict[str, object]]:
        """Return recent summaries across every backtest kind, newest first.

        各类型分别截断：热门类型（比如组合回测）不该把冷门类型挤出对比列表，
        否则「跨类型对比」这个卖点自己就把自己废了。
        """

        limit = max(1, limit_per_type)
        with self._store._connect(read_only=True) as connection:
            rows = connection.execute(
                f"""
                SELECT {_BACKTEST_HISTORY_LIST_COLUMNS}
                FROM backtest_history
                ORDER BY finished_at DESC, history_id DESC
                """
            ).fetchall()
            instrument_names = _instrument_name_map(connection)
        kept: dict[str, int] = {}
        decoded: list[dict[str, object]] = []
        for row in rows:
            backtest_type = str(row[1])
            if kept.get(backtest_type, 0) >= limit:
                continue
            kept[backtest_type] = kept.get(backtest_type, 0) + 1
            decoded.append(_decode_backtest_history_row(row, instrument_names, backtest_type))
        return decoded

    def read_backtest_history(self, history_id: str) -> dict[str, object] | None:
        """Read one complete backtest history snapshot."""

        records = self.read_backtest_histories([history_id])
        return records[0] if records else None

    def read_backtest_histories(
        self,
        history_ids: Iterable[str],
    ) -> list[dict[str, object]]:
        """Read several complete snapshots in one connection, in the given order.

        对比要一次性取 2~5 条完整结果（曲线只在 result_json 里），逐条开连接会
        把 DuckDB 的连接开销乘以条目数，而它们本来就是同一次点击要用的数据。
        """

        ids: list[str] = []
        for value in history_ids:
            text = str(value or "").strip()
            if text and text not in ids:
                ids.append(text)
        if not ids:
            return []
        placeholders = ", ".join("?" for _ in ids)
        with self._store._connect(read_only=True) as connection:
            rows = connection.execute(
                f"""
                SELECT {_BACKTEST_HISTORY_FULL_COLUMNS}
                FROM backtest_history
                WHERE history_id IN ({placeholders})
                """,
                ids,
            ).fetchall()
            instrument_names = _instrument_name_map(connection)
        records = [_decode_full_backtest_history_row(row, instrument_names) for row in rows]
        order = {value: index for index, value in enumerate(ids)}
        records.sort(key=lambda item: order.get(str(item["history_id"]), len(order)))
        return records

    def delete_backtest_history(self, history_id: str) -> bool:
        """Delete one backtest history record."""

        self._store._require_writer()
        normalized_id = history_id.strip()
        if not normalized_id:
            return False
        with _WRITE_LOCK, self._store._connect() as connection:
            deleted = connection.execute(
                "DELETE FROM backtest_history WHERE history_id = ? RETURNING history_id",
                [normalized_id],
            ).fetchone()
        return deleted is not None
