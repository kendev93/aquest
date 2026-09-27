"""Discovery history persistence methods."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import cast

from .store_base import (
    _WRITE_LOCK,
    STRATEGY_DISCOVERY_HISTORY_LIMIT,
    MarketDataStoreError,
    _DuckDbStoreBase,
    _now,
)
from .store_history_common import (
    _int_or_zero,
    _normalize_timestamp,
)


class StrategyDiscoveryHistoryMixin:
    _store: _DuckDbStoreBase

    def save_strategy_discovery_history(
        self,
        history_id: str,
        result: Mapping[str, object],
        *,
        created_at: datetime | None = None,
        started_at: datetime | str | None = None,
        finished_at: datetime | str | None = None,
    ) -> None:
        """Persist one completed strategy-discovery run and retain the newest 20."""

        self._store._require_writer()
        normalized_id = history_id.strip()
        if not normalized_id:
            raise ValueError("策略实验历史 ID 不能为空")
        payload = dict(result)
        request = (
            cast(Mapping[str, object], payload["request"])
            if isinstance(payload.get("request"), Mapping)
            else {}
        )
        search_metadata = (
            cast(Mapping[str, object], payload["search_metadata"])
            if isinstance(payload.get("search_metadata"), Mapping)
            else {}
        )
        validation_metadata = (
            cast(Mapping[str, object], payload["validation_metadata"])
            if isinstance(payload.get("validation_metadata"), Mapping)
            else {}
        )
        algorithm_metadata = (
            cast(Mapping[str, object], payload["algorithm_metadata"])
            if isinstance(payload.get("algorithm_metadata"), Mapping)
            else {}
        )
        sharpe = payload.get("results_by_sharpe")
        returns = payload.get("results_by_return")
        sharpe_rows = list(sharpe) if isinstance(sharpe, (list, tuple)) else []
        return_rows = list(returns) if isinstance(returns, (list, tuple)) else []
        try:
            raw_trials = search_metadata.get("evaluated_trials", 0)
            total_trials = (
                int(raw_trials)
                if isinstance(raw_trials, (int, float))
                else int(str(raw_trials or 0))
            )
            raw_duration = payload.get("duration_seconds", 0.0)
            duration = (
                float(raw_duration)
                if isinstance(raw_duration, (int, float))
                else float(str(raw_duration or 0.0))
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("策略实验历史指标格式无效") from exc
        created_hint: object | None = created_at
        if created_hint is None:
            created_hint = payload.get("created_at") or started_at or payload.get("started_at")
        created = _normalize_timestamp(created_hint, _now())
        started = _normalize_timestamp(
            started_at if started_at is not None else payload.get("started_at"),
            created,
        )
        finished_hint: object | None = (
            finished_at if finished_at is not None else payload.get("finished_at")
        )
        finished = _normalize_timestamp(
            finished_hint,
            started + timedelta(seconds=duration),
        )
        if finished < started:
            raise ValueError("策略实验结束时间不能早于开始时间")
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
            try:
                connection.execute(
                    """
                    INSERT INTO strategy_discovery_history (
                        history_id, created_at, started_at, finished_at,
                        request_json, search_metadata_json,
                        validation_metadata_json, algorithm_metadata_json, data_fingerprint,
                        results_by_sharpe_json, results_by_return_json, total_trials,
                        duration_seconds
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (history_id) DO UPDATE SET
                        created_at = excluded.created_at,
                        started_at = excluded.started_at,
                        finished_at = excluded.finished_at,
                        request_json = excluded.request_json,
                        search_metadata_json = excluded.search_metadata_json,
                        validation_metadata_json = excluded.validation_metadata_json,
                        algorithm_metadata_json = excluded.algorithm_metadata_json,
                        data_fingerprint = excluded.data_fingerprint,
                        results_by_sharpe_json = excluded.results_by_sharpe_json,
                        results_by_return_json = excluded.results_by_return_json,
                        total_trials = excluded.total_trials,
                        duration_seconds = excluded.duration_seconds
                    """,
                    [
                        normalized_id,
                        created,
                        started,
                        finished,
                        json.dumps(dict(request), ensure_ascii=False, separators=(",", ":")),
                        json.dumps(
                            dict(search_metadata), ensure_ascii=False, separators=(",", ":")
                        ),
                        json.dumps(
                            dict(validation_metadata), ensure_ascii=False, separators=(",", ":")
                        ),
                        json.dumps(
                            dict(algorithm_metadata), ensure_ascii=False, separators=(",", ":")
                        ),
                        str(payload.get("data_fingerprint") or ""),
                        json.dumps(sharpe_rows, ensure_ascii=False, separators=(",", ":")),
                        json.dumps(return_rows, ensure_ascii=False, separators=(",", ":")),
                        max(0, total_trials),
                        max(0.0, duration),
                    ],
                )
                stale = connection.execute(
                    """
                    SELECT history_id FROM strategy_discovery_history
                    ORDER BY created_at DESC, history_id DESC
                    LIMIT 100000 OFFSET ?
                    """,
                    [STRATEGY_DISCOVERY_HISTORY_LIMIT],
                ).fetchall()
                if stale:
                    connection.executemany(
                        "DELETE FROM strategy_discovery_replay_history WHERE experiment_id = ?",
                        stale,
                    )
                    connection.executemany(
                        "DELETE FROM strategy_discovery_history WHERE history_id = ?", stale
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def list_strategy_discovery_history(self) -> list[dict[str, object]]:
        """Return recent strategy-discovery summaries without full candidate payloads."""

        with self._store._connect(read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT history_id, created_at, data_fingerprint,
                       started_at, finished_at, total_trials, duration_seconds,
                       results_by_sharpe_json, results_by_return_json, request_json,
                       search_metadata_json
                FROM strategy_discovery_history
                ORDER BY created_at DESC, history_id DESC
                LIMIT ?
                """,
                [STRATEGY_DISCOVERY_HISTORY_LIMIT],
            ).fetchall()
        summaries: list[dict[str, object]] = []
        for row in rows:
            try:
                sharpe = json.loads(str(row[7]))
                returns = json.loads(str(row[8]))
                request = json.loads(str(row[9]))
                search_metadata = json.loads(str(row[10]))
            except json.JSONDecodeError as exc:
                raise MarketDataStoreError("策略实验历史内容损坏") from exc
            request_map = request if isinstance(request, dict) else {}
            search_map = search_metadata if isinstance(search_metadata, dict) else {}
            mode = str(request_map.get("mode") or "global")
            refinement = request_map.get("refinement")
            refinement_map = refinement if isinstance(refinement, dict) else {}
            legacy_total_trials = int(row[5])
            requested_trials = (
                _int_or_zero(search_map.get("requested_trials")) or legacy_total_trials
            )
            evaluated_trials = (
                _int_or_zero(search_map.get("evaluated_trials")) or legacy_total_trials
            )
            unique_trials = (
                _int_or_zero(search_map.get("unique_evaluated_trials")) or evaluated_trials
            )
            summaries.append(
                {
                    "history_id": str(row[0]),
                    "created_at": str(row[1]),
                    "data_fingerprint": str(row[2]),
                    "started_at": str(row[3]),
                    "finished_at": str(row[4]),
                    "total_trials": legacy_total_trials,
                    "requested_trials": requested_trials,
                    "evaluated_trials": evaluated_trials,
                    "unique_evaluated_trials": unique_trials,
                    "duplicate_trials": _int_or_zero(search_map.get("duplicate_trials")),
                    "duration_seconds": float(row[6]),
                    "best_sharpe": sharpe[0] if isinstance(sharpe, list) and sharpe else None,
                    "best_return": returns[0] if isinstance(returns, list) and returns else None,
                    "mode": mode,
                    "parent_history_id": refinement_map.get("parent_history_id"),
                    "parent_candidate_hash": refinement_map.get("parent_candidate_hash"),
                }
            )
        return summaries

    def read_strategy_discovery_history(self, history_id: str) -> dict[str, object] | None:
        """Read a complete strategy-discovery history snapshot."""

        with self._store._connect(read_only=True) as connection:
            row = connection.execute(
                """
                SELECT history_id, created_at, started_at, finished_at,
                       request_json, search_metadata_json, validation_metadata_json,
                       algorithm_metadata_json, data_fingerprint, results_by_sharpe_json,
                       results_by_return_json, total_trials, duration_seconds
                FROM strategy_discovery_history
                WHERE history_id = ?
                """,
                [history_id.strip()],
            ).fetchone()
        if row is None:
            return None
        try:
            request = json.loads(str(row[4]))
            search_metadata = json.loads(str(row[5]))
            validation_metadata = json.loads(str(row[6]))
            algorithm_metadata = json.loads(str(row[7]))
            results_by_sharpe = json.loads(str(row[9]))
            results_by_return = json.loads(str(row[10]))
        except json.JSONDecodeError as exc:
            raise MarketDataStoreError("策略实验历史内容损坏") from exc
        if not all(
            isinstance(value, (dict, list))
            for value in (
                request,
                search_metadata,
                validation_metadata,
                algorithm_metadata,
                results_by_sharpe,
                results_by_return,
            )
        ):
            raise MarketDataStoreError("策略实验历史内容无效")
        return {
            "history_id": str(row[0]),
            "created_at": str(row[1]),
            "started_at": str(row[2]),
            "finished_at": str(row[3]),
            "request": request,
            "search_metadata": search_metadata,
            "validation_metadata": validation_metadata,
            "algorithm_metadata": algorithm_metadata,
            "data_fingerprint": str(row[8]),
            "results_by_sharpe": results_by_sharpe,
            "results_by_return": results_by_return,
            "total_trials": int(row[11]),
            "duration_seconds": float(row[12]),
        }

    def delete_strategy_discovery_history(self, history_id: str) -> bool:
        """Delete one strategy-discovery history record."""

        self._store._require_writer()
        normalized_id = history_id.strip()
        if not normalized_id:
            return False
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
            try:
                connection.execute(
                    "DELETE FROM strategy_discovery_replay_history WHERE experiment_id = ?",
                    [normalized_id],
                )
                deleted = connection.execute(
                    "DELETE FROM strategy_discovery_history "
                    "WHERE history_id = ? RETURNING history_id",
                    [normalized_id],
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return deleted is not None

    def save_strategy_discovery_replay_history(
        self,
        replay_id: str,
        result: Mapping[str, object],
        *,
        started_at: datetime | str,
        finished_at: datetime | str,
        created_at: datetime | str | None = None,
        duration_seconds: float | None = None,
    ) -> None:
        """Persist the latest full-range replay for one experiment leaderboard."""

        self._store._require_writer()
        normalized_id = replay_id.strip()
        if not normalized_id:
            raise ValueError("策略复核 ID 不能为空")
        payload = dict(result)
        experiment_id = str(payload.get("experiment_id") or "").strip()
        leaderboard = str(payload.get("leaderboard") or "").strip()
        if not experiment_id:
            raise ValueError("策略复核缺少实验 ID")
        if leaderboard not in {"sharpe", "return"}:
            raise ValueError("策略复核榜单类型无效")
        started = _normalize_timestamp(started_at, _now())
        finished = _normalize_timestamp(finished_at, started)
        if finished < started:
            raise ValueError("策略复核结束时间不能早于开始时间")
        created = _normalize_timestamp(created_at, started)
        if duration_seconds is None:
            duration = (finished - started).total_seconds()
        else:
            try:
                duration = float(duration_seconds)
            except (TypeError, ValueError) as exc:
                raise ValueError("策略复核耗时格式无效") from exc
        if not math.isfinite(duration) or duration < 0:
            raise ValueError("策略复核耗时必须是大于等于 0 的有限数字")
        data_start = str(payload.get("data_start") or "")
        data_end = str(payload.get("data_end") or "")
        data_fingerprint = str(payload.get("data_fingerprint") or "")
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
            try:
                connection.execute(
                    """
                    INSERT INTO strategy_discovery_replay_history (
                        replay_id, experiment_id, leaderboard, created_at, started_at,
                        finished_at, data_start, data_end, data_fingerprint,
                        duration_seconds, result_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (experiment_id, leaderboard) DO UPDATE SET
                        replay_id = excluded.replay_id,
                        created_at = excluded.created_at,
                        started_at = excluded.started_at,
                        finished_at = excluded.finished_at,
                        data_start = excluded.data_start,
                        data_end = excluded.data_end,
                        data_fingerprint = excluded.data_fingerprint,
                        duration_seconds = excluded.duration_seconds,
                        result_json = excluded.result_json
                    """,
                    [
                        normalized_id,
                        experiment_id,
                        leaderboard,
                        created,
                        started,
                        finished,
                        data_start,
                        data_end,
                        data_fingerprint,
                        duration,
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    ],
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def read_strategy_discovery_replay_history(
        self, experiment_id: str, leaderboard: str
    ) -> dict[str, object] | None:
        """Read the latest persisted replay for one experiment leaderboard."""

        if leaderboard not in {"sharpe", "return"}:
            raise ValueError("策略复核榜单类型无效")
        with self._store._connect(read_only=True) as connection:
            row = connection.execute(
                """
                SELECT replay_id, experiment_id, leaderboard, created_at, started_at,
                       finished_at, data_start, data_end, data_fingerprint,
                       duration_seconds, result_json
                FROM strategy_discovery_replay_history
                WHERE experiment_id = ? AND leaderboard = ?
                """,
                [experiment_id.strip(), leaderboard],
            ).fetchone()
        if row is None:
            return None
        try:
            result = json.loads(str(row[10]))
        except json.JSONDecodeError as exc:
            raise MarketDataStoreError("策略复核历史内容损坏") from exc
        if not isinstance(result, dict):
            raise MarketDataStoreError("策略复核历史内容无效")
        return {
            "replay_id": str(row[0]),
            "experiment_id": str(row[1]),
            "leaderboard": str(row[2]),
            "created_at": str(row[3]),
            "started_at": str(row[4]),
            "finished_at": str(row[5]),
            "data_start": str(row[6]),
            "data_end": str(row[7]),
            "data_fingerprint": str(row[8]),
            "duration_seconds": float(row[9]),
            "result": result,
        }
