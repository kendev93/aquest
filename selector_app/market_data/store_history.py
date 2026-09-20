"""Repository operations for screening, discovery, replay, and backtest history."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Iterator, Mapping
from datetime import datetime, timedelta, timezone
from typing import cast

import duckdb

from .store_base import (
    _BACKTEST_HISTORY_TYPES,
    _WRITE_LOCK,
    BACKTEST_HISTORY_LIMIT,
    SCREEN_HISTORY_LIMIT,
    STRATEGY_DISCOVERY_HISTORY_LIMIT,
    MarketDataStoreError,
    _clean_name,
    _DuckDbStoreBase,
    _now,
)

# 用户资产表：记录的是用户自己的研究成果，丢了无法重建，所以支持导出导入。
# 行情与指标缓存刻意不在其中——它们可以从数据源重新同步/重算。
ASSET_TABLES: tuple[str, ...] = (
    "backtest_history",
    "screen_history",
    "strategy_discovery_history",
    "strategy_discovery_replay_history",
)


def _require_asset_table(table: str) -> str:
    """Reject anything that is not a known user-asset table.

    The table name is interpolated into SQL, so it must never come straight
    from a caller.
    """

    normalized = str(table or "").strip()
    if normalized not in ASSET_TABLES:
        raise MarketDataStoreError(f"不支持的资产表: {table}")
    return normalized


def _int_or_zero(value: object) -> int:
    try:
        return max(0, int(value)) if isinstance(value, (int, float, str)) else 0
    except (TypeError, ValueError):
        return 0


def _normalize_timestamp(value: object | None, fallback: datetime) -> datetime:
    """Normalize API or DuckDB timestamps to naive UTC datetimes."""

    if value is None:
        timestamp = fallback
    elif isinstance(value, datetime):
        timestamp = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            timestamp = fallback
        else:
            try:
                timestamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("历史时间格式无效") from exc
    else:
        raise ValueError("历史时间格式无效")
    if timestamp.tzinfo is not None:
        return timestamp.astimezone(timezone.utc).replace(tzinfo=None)
    return timestamp


def _validate_backtest_history_type(backtest_type: str) -> None:
    if backtest_type not in _BACKTEST_HISTORY_TYPES:
        raise ValueError("回测历史类型无效")


def _finite_or_none(value: object) -> float | None:
    """Return a finite float, or None for anything unusable (NaN, None, text)."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _typed_history_summary(
    backtest_type: str | None,
    result: Mapping[str, object],
) -> dict[str, object]:
    """Extra list-friendly fields for the two research flavours.

    Plain backtests are described well enough by the shared fields, but a
    condition or swing study is judged by what it found, and that lives deep
    inside the result. Pulling it out here keeps the history list readable
    without loading every full result into the page.
    """

    if backtest_type == "condition_research":
        return _condition_research_summary(result)
    if backtest_type == "swing_research":
        return _swing_research_summary(result)
    return {}


def _condition_research_summary(result: Mapping[str, object]) -> dict[str, object]:
    """Candidate counts plus the strongest surviving condition."""

    summary: dict[str, object] = {}
    baseline = result.get("baseline")
    if isinstance(baseline, Mapping):
        train = baseline.get("train")
        if isinstance(train, Mapping):
            pooled = train.get("pooled_trades")
            if isinstance(pooled, Mapping):
                win_rate = _finite_or_none(pooled.get("win_rate"))
                if win_rate is not None:
                    summary["baseline_win_rate"] = win_rate
    candidates = result.get("candidates")
    if not isinstance(candidates, (list, tuple)):
        return summary
    rows = [item for item in candidates if isinstance(item, Mapping)]
    qualified = [item for item in rows if item.get("passed_constraints") is True]
    summary["candidate_count"] = len(rows)
    summary["qualified_count"] = len(qualified)
    # 合格候选优先：一个提升很高但连约束都没过的条件只是噪声，不该当门面。
    best: Mapping[str, object] | None = None
    best_lift = 0.0
    for item in qualified or rows:
        mining = item.get("mining")
        if not isinstance(mining, Mapping):
            continue
        lift = _finite_or_none(mining.get("lift"))
        if lift is None:
            continue
        if best is None or lift > best_lift:
            best, best_lift = item, lift
    if best is None:
        return summary
    mining = best.get("mining")
    summary["top_lift"] = best_lift
    side = best.get("side")
    if isinstance(side, str):
        summary["top_side"] = side
    condition = best.get("condition")
    if isinstance(condition, Mapping):
        summary["top_condition"] = dict(condition)
    if isinstance(mining, Mapping):
        for key in ("outcome_rate", "baseline_rate", "support_count", "instrument_count"):
            value = _finite_or_none(mining.get(key))
            if value is not None:
                summary[f"top_{key}"] = value
    return summary


def _swing_research_summary(result: Mapping[str, object]) -> dict[str, object]:
    """Study parameters plus the strongest indicator it surfaced."""

    summary: dict[str, object] = {}
    for key in (
        "stock_count",
        "scanned_stocks",
        "reversal_multiple",
        "min_bars",
        "atr_window",
        "horizon",
        "permutations",
        "block_size",
        "alpha",
        "value_count",
        "significant_count",
    ):
        if key in result:
            summary[key] = result[key]
    date_range = result.get("date_range")
    if isinstance(date_range, Mapping):
        summary["date_range"] = dict(date_range)
    labels = result.get("labels")
    if isinstance(labels, (list, tuple)):
        summary["label_names"] = [
            str(item.get("display_name") or item.get("id"))
            for item in labels
            if isinstance(item, Mapping)
        ]
    scores = result.get("scores")
    if isinstance(scores, (list, tuple)):
        rows = [item for item in scores if isinstance(item, Mapping)]
        thresholds = [
            value
            for value in (_finite_or_none(item.get("ic_threshold")) for item in rows)
            if value is not None
        ]
        if thresholds:
            summary["noise_floor"] = max(thresholds)
        best: Mapping[str, object] | None = None
        best_ic = 0.0
        for item in rows:
            ic_mean = _finite_or_none(item.get("ic_mean"))
            if ic_mean is None:
                continue
            if best is None or ic_mean > best_ic:
                best, best_ic = item, ic_mean
        if best is not None:
            summary["top_indicator"] = {
                "value_id": str(best.get("value_id") or ""),
                "display_name": str(best.get("display_name") or best.get("value_id") or ""),
                "ic_mean": best_ic,
                "label": str(best.get("label") or ""),
                "order": str(best.get("order") or ""),
                "significant": bool(best.get("significant")),
            }
    plan = result.get("condition_plan")
    if isinstance(plan, Mapping):
        tradeable = _finite_or_none(plan.get("tradeable_count"))
        if tradeable is not None:
            summary["tradeable_count"] = tradeable
    return summary


def _backtest_history_summary(
    result: Mapping[str, object],
    backtest_type: str | None = None,
) -> dict[str, object]:
    """Extract a small list-friendly summary without duplicating full results."""

    summary: dict[str, object] = {}
    for key in (
        "market",
        "code",
        "name",
        "universe",
        "bars",
        "start_date",
        "end_date",
        "train_end_date",
        "validation_end_date",
        "total_candidates",
        "processed",
        "skipped",
        "errors",
    ):
        if key in result:
            summary[key] = result[key]
    performance = result.get("performance")
    if isinstance(performance, Mapping):
        summary["performance"] = dict(performance)
    trades = result.get("trades")
    if isinstance(trades, (list, tuple)):
        summary["trade_count"] = len(trades)
    fitness_results = result.get("results")
    if isinstance(fitness_results, (list, tuple)):
        summary["result_count"] = len(fitness_results)
    instrument_names = _backtest_history_instrument_names(result)
    if instrument_names:
        summary["instrument_names"] = instrument_names
    summary.update(_typed_history_summary(backtest_type, result))
    return summary


def _backtest_history_instrument_names(result: Mapping[str, object]) -> list[dict[str, str]]:
    """Collect a bounded name preview for multi-instrument history rows."""

    instruments: dict[tuple[str, str], dict[str, str]] = {}

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            market = value.get("market")
            code = value.get("code")
            name = _clean_name(value.get("name"))
            if name is not None and market is not None and code is not None:
                key = (str(market), str(code))
                instruments[key] = {"market": key[0], "code": key[1], "name": name}
            for child in value.values():
                visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)

    visit(result)
    return list(instruments.values())[:12]


def _enrich_backtest_history_names(
    value: object,
    names: Mapping[tuple[str, str], str],
) -> object:
    """Add current instrument names to old history payloads on read."""

    if isinstance(value, Mapping):
        enriched = {
            str(key): _enrich_backtest_history_names(child, names) for key, child in value.items()
        }
        market = enriched.get("market")
        code = enriched.get("code")
        if (not _clean_name(enriched.get("name"))) and market is not None and code is not None:
            name = names.get((str(market), str(code)))
            if name is not None:
                enriched["name"] = name
        return enriched
    if isinstance(value, list):
        return [_enrich_backtest_history_names(child, names) for child in value]
    if isinstance(value, tuple):
        return tuple(_enrich_backtest_history_names(child, names) for child in value)
    return value


def _instrument_name_map(connection: duckdb.DuckDBPyConnection) -> dict[tuple[str, str], str]:
    rows = connection.execute(
        "SELECT market, code, name FROM instruments WHERE name IS NOT NULL"
    ).fetchall()
    return {(str(row[0]), str(row[1])): str(row[2]) for row in rows if str(row[2]).strip()}


class DuckDbHistoryStore:
    """Persist and retrieve user-facing screening and backtest history."""

    def __init__(self, store: _DuckDbStoreBase) -> None:
        self._store = store

    def save_screen_history(
        self,
        history_id: str,
        *,
        results: Iterable[Mapping[str, object]],
        meta: Mapping[str, object],
        created_at: datetime | None = None,
    ) -> None:
        """Save one completed formula-screen result and trim older entries."""

        self._store._require_writer()
        normalized_id = history_id.strip()
        if not normalized_id:
            raise ValueError("筛选历史 ID 不能为空")
        result_rows = [dict(result) for result in results]
        meta_dict = dict(meta)
        required_counts: dict[str, int] = {}
        for key in ("total_candidates", "total_scanned", "total_signals", "errors", "skipped"):
            raw_value = meta_dict.get(key, 0)
            required_counts[key] = int(raw_value) if isinstance(raw_value, (int, float, str)) else 0
        created = created_at or _now()
        if created.tzinfo is not None:
            created = created.astimezone(timezone.utc).replace(tzinfo=None)
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
            try:
                connection.execute(
                    """
                    INSERT INTO screen_history (
                        history_id, created_at, total_candidates, total_scanned,
                        total_signals, errors, skipped, result_count, meta_json, results_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (history_id) DO UPDATE SET
                        created_at = excluded.created_at,
                        total_candidates = excluded.total_candidates,
                        total_scanned = excluded.total_scanned,
                        total_signals = excluded.total_signals,
                        errors = excluded.errors,
                        skipped = excluded.skipped,
                        result_count = excluded.result_count,
                        meta_json = excluded.meta_json,
                        results_json = excluded.results_json
                    """,
                    [
                        normalized_id,
                        created,
                        required_counts["total_candidates"],
                        required_counts["total_scanned"],
                        required_counts["total_signals"],
                        required_counts["errors"],
                        required_counts["skipped"],
                        len(result_rows),
                        json.dumps(meta_dict, ensure_ascii=False, separators=(",", ":")),
                        json.dumps(result_rows, ensure_ascii=False, separators=(",", ":")),
                    ],
                )
                stale = connection.execute(
                    """
                    SELECT history_id
                    FROM screen_history
                    ORDER BY created_at DESC, history_id DESC
                    LIMIT 100000 OFFSET ?
                    """,
                    [SCREEN_HISTORY_LIMIT],
                ).fetchall()
                if stale:
                    connection.executemany(
                        "DELETE FROM screen_history WHERE history_id = ?",
                        stale,
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def list_screen_history(self) -> list[dict[str, object]]:
        """Return the most recent formula-screen history summaries."""

        with self._store._connect(read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT history_id, created_at, total_candidates, total_scanned,
                       total_signals, errors, skipped, result_count
                FROM screen_history
                ORDER BY created_at DESC, history_id DESC
                LIMIT ?
                """,
                [SCREEN_HISTORY_LIMIT],
            ).fetchall()
        return [
            {
                "history_id": str(row[0]),
                "created_at": str(row[1]),
                "total_candidates": int(row[2]),
                "total_scanned": int(row[3]),
                "total_signals": int(row[4]),
                "errors": int(row[5]),
                "skipped": int(row[6]),
                "result_count": int(row[7]),
            }
            for row in rows
        ]

    def read_screen_history(self, history_id: str) -> dict[str, object] | None:
        """Read one formula-screen history record with its complete results."""

        with self._store._connect(read_only=True) as connection:
            row = connection.execute(
                """
                SELECT history_id, created_at, meta_json, results_json
                FROM screen_history
                WHERE history_id = ?
                """,
                [history_id.strip()],
            ).fetchone()
        if row is None:
            return None
        try:
            meta = json.loads(str(row[2]))
            results = json.loads(str(row[3]))
        except json.JSONDecodeError as exc:
            raise MarketDataStoreError("筛选历史内容损坏") from exc
        if not isinstance(meta, dict) or not isinstance(results, list):
            raise MarketDataStoreError("筛选历史内容无效")
        return {
            "history_id": str(row[0]),
            "created_at": str(row[1]),
            "meta": meta,
            "results": results,
        }

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

    def list_backtest_history(self, backtest_type: str) -> list[dict[str, object]]:
        """Return recent summaries for one kind of backtest."""

        _validate_backtest_history_type(backtest_type)
        with self._store._connect(read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT history_id, backtest_type, created_at, started_at, finished_at,
                       duration_seconds, result_json, summary_json
                FROM backtest_history
                WHERE backtest_type = ?
                ORDER BY finished_at DESC, history_id DESC
                LIMIT ?
                """,
                [backtest_type, BACKTEST_HISTORY_LIMIT],
            ).fetchall()
            instrument_names = _instrument_name_map(connection)
        summaries: list[dict[str, object]] = []
        for row in rows:
            summary: object
            try:
                summary = json.loads(str(row[7]))
            except json.JSONDecodeError as exc:
                raise MarketDataStoreError("回测历史内容损坏") from exc
            if not isinstance(summary, dict):
                raise MarketDataStoreError("回测历史摘要无效")
            try:
                result = json.loads(str(row[6]))
            except json.JSONDecodeError:
                result = None
            if isinstance(result, dict):
                enriched_result = _enrich_backtest_history_names(result, instrument_names)
                if isinstance(enriched_result, dict):
                    enriched_summary = _enrich_backtest_history_names(summary, instrument_names)
                    summary = dict(enriched_summary) if isinstance(enriched_summary, dict) else {}
                    summary.update(_backtest_history_summary(enriched_result, backtest_type))
            summaries.append(
                {
                    "history_id": str(row[0]),
                    "backtest_type": str(row[1]),
                    "created_at": str(row[2]),
                    "started_at": str(row[3]),
                    "finished_at": str(row[4]),
                    "duration_seconds": float(row[5]),
                    "summary": summary,
                }
            )
        return summaries

    def read_backtest_history(self, history_id: str) -> dict[str, object] | None:
        """Read one complete backtest history snapshot."""

        with self._store._connect(read_only=True) as connection:
            row = connection.execute(
                """
                SELECT history_id, backtest_type, created_at, started_at, finished_at,
                       duration_seconds, request_json, result_json, summary_json
                FROM backtest_history
                WHERE history_id = ?
                """,
                [history_id.strip()],
            ).fetchone()
            instrument_names = _instrument_name_map(connection)
        if row is None:
            return None
        try:
            request = json.loads(str(row[6]))
            result = json.loads(str(row[7]))
            summary = json.loads(str(row[8]))
        except json.JSONDecodeError as exc:
            raise MarketDataStoreError("回测历史内容损坏") from exc
        if not all(isinstance(value, dict) for value in (request, result, summary)):
            raise MarketDataStoreError("回测历史内容无效")
        enriched_result = _enrich_backtest_history_names(result, instrument_names)
        enriched_summary = _enrich_backtest_history_names(summary, instrument_names)
        if isinstance(enriched_result, dict):
            result = enriched_result
            if isinstance(enriched_summary, dict):
                summary = dict(enriched_summary)
                summary.update(_backtest_history_summary(result, str(row[1])))
        return {
            "history_id": str(row[0]),
            "backtest_type": str(row[1]),
            "created_at": str(row[2]),
            "started_at": str(row[3]),
            "finished_at": str(row[4]),
            "duration_seconds": float(row[5]),
            "request": request,
            "result": result,
            "summary": summary,
        }

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

    def history_table_columns(self, table: str) -> tuple[str, ...]:
        """Column names of one user-asset table, in physical order."""

        _require_asset_table(table)
        with self._store._connect(read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = ?
                ORDER BY ordinal_position
                """,
                [table],
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def iter_history_rows(
        self,
        table: str,
        *,
        batch_size: int = 200,
    ) -> Iterator[dict[str, object]]:
        """Stream every row of one user-asset table as plain dictionaries.

        Rows are fetched in batches so exporting a multi-megabyte research
        report never materialises the whole table in memory.
        """

        _require_asset_table(table)
        if batch_size < 1:
            raise ValueError("批量读取行数必须大于 0")
        columns = self.history_table_columns(table)
        if not columns:
            return
        projection = ", ".join(f'"{column}"' for column in columns)
        with self._store._connect(read_only=True) as connection:
            cursor = connection.execute(f'SELECT {projection} FROM "{table}"')
            while True:
                batch = cursor.fetchmany(batch_size)
                if not batch:
                    return
                for row in batch:
                    yield dict(zip(columns, row, strict=True))

    def insert_history_rows(
        self,
        table: str,
        rows: Iterable[Mapping[str, object]],
        *,
        batch_size: int = 200,
    ) -> tuple[int, int]:
        """Insert user-asset rows, skipping any that already exist.

        Returns ``(inserted, attempted)``.  Conflicts are matched by primary key
        *and* by any unique constraint, so importing the same backup twice is a
        no-op instead of a duplicate or an error.
        """

        self._store._require_writer()
        _require_asset_table(table)
        if batch_size < 1:
            raise ValueError("批量写入行数必须大于 0")
        columns = self.history_table_columns(table)
        if not columns:
            return (0, 0)
        projection = ", ".join(f'"{column}"' for column in columns)
        placeholders = ", ".join("?" for _ in columns)
        statement = (
            f'INSERT INTO "{table}" ({projection}) VALUES ({placeholders}) ON CONFLICT DO NOTHING'
        )
        count_statement = f'SELECT COUNT(*) FROM "{table}"'

        inserted = 0
        attempted = 0
        batch: list[tuple[object, ...]] = []
        with _WRITE_LOCK, self._store._connect() as connection:
            for row in rows:
                batch.append(tuple(row.get(column) for column in columns))
                if len(batch) < batch_size:
                    continue
                before = _count_rows(connection, count_statement)
                connection.executemany(statement, batch)
                inserted += _count_rows(connection, count_statement) - before
                attempted += len(batch)
                batch.clear()
            if batch:
                before = _count_rows(connection, count_statement)
                connection.executemany(statement, batch)
                inserted += _count_rows(connection, count_statement) - before
                attempted += len(batch)
        return inserted, attempted


def _count_rows(connection: duckdb.DuckDBPyConnection, statement: str) -> int:
    row = connection.execute(statement).fetchone()
    return int(row[0]) if row is not None else 0
