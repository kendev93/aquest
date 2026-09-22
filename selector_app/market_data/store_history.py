"""Repository operations for screening, discovery, replay, and backtest history."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Iterator, Mapping
from datetime import date, datetime, timedelta, timezone
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
    "saved_strategies",
    "strategy_tracking",
    "strategy_discovery_history",
    "strategy_discovery_replay_history",
    "screen_presets",
    "candidate_library",
)
SAVED_STRATEGY_TABLE = "saved_strategies"
STRATEGY_TRACKING_TABLE = "strategy_tracking"
STRATEGY_NAME_MAX_LENGTH = 60
STRATEGY_NOTE_MAX_LENGTH = 500
SCREEN_PRESET_TABLE = "screen_presets"
PRESET_NAME_MAX_LENGTH = 60
PRESET_NOTE_MAX_LENGTH = 200
CANDIDATE_LIBRARY_TABLE = "candidate_library"
CANDIDATE_NOTE_MAX_LENGTH = 300
#: 候选状态。adopted 是人的表态，published 是人的二次确认——没有自动从 pending
#: 跳到 published 的路径，因为「通过置换检验」和「我认可这条」是两件事。
CANDIDATE_STATUS_PENDING = "pending"
CANDIDATE_STATUS_ADOPTED = "adopted"
CANDIDATE_STATUS_REJECTED = "rejected"
CANDIDATE_STATUS_PUBLISHED = "published"
_CANDIDATE_STATUSES = frozenset(
    {
        CANDIDATE_STATUS_PENDING,
        CANDIDATE_STATUS_ADOPTED,
        CANDIDATE_STATUS_REJECTED,
        CANDIDATE_STATUS_PUBLISHED,
    }
)
#: 策略追踪状态。只有 off / active 两态——上线后要改规则只能停掉旧的、新开一个，
#: 没有「暂停后改参数继续」这种说法，否则样本外就不再是样本外了。
STRATEGY_TRACKING_OFF = "off"
STRATEGY_TRACKING_ACTIVE = "active"
_STRATEGY_TRACKING_STATUSES = frozenset({STRATEGY_TRACKING_OFF, STRATEGY_TRACKING_ACTIVE})
#: 策略列。上线曾经打算存一份「上线日末的模拟状态快照」用来增量重跑，后来确认
#: 走不通（见 docs/strategy-tracking-plan.md §2.1 的行情指纹校验），改全量重跑后
#: 这个字段就没用了——重算所需的全部信息就是 request_json 本身。
_STRATEGY_COLUMNS = (
    "strategy_id, name, note, source_history_id, created_at, updated_at, "
    "request_json, summary_json, tracking_status, launched_at, last_tracked_date, last_error"
)
_PRESET_COLUMNS = "preset_id, name, note, created_at, updated_at, payload_json"
_CANDIDATE_COLUMNS = (
    "candidate_id, source, source_history_id, source_candidate_id, side, label, "
    "condition_json, evidence_json, status, note, published_strategy_id, created_at, updated_at"
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
        # 因子检验与波段研究把区间放在 date_range 里，没有顶层 start/end；
        # 摘要缺了它，历史列表就显示不出区间，对比页也判断不了有没有曲线。
        "date_range",
        "scanned_stocks",
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


def _tracking_fields(row: tuple[object, ...]) -> dict[str, object]:
    """Decode the strategy-tracking columns that follow the base eight."""

    tracking: dict[str, object] = {
        "tracking_status": str(row[8] or STRATEGY_TRACKING_OFF),
        "launched_at": str(row[9]) if row[9] is not None else None,
        "last_tracked_date": None if row[10] is None else _tracking_date(row[10]),
        "last_error": str(row[11] or ""),
    }
    return tracking


def _decode_strategy_row(row: tuple[object, ...]) -> dict[str, object]:
    """Decode one ``saved_strategies`` row into an API-ready dict."""

    try:
        request = json.loads(str(row[6]))
        summary = json.loads(str(row[7]))
    except json.JSONDecodeError as exc:
        raise MarketDataStoreError("策略内容损坏") from exc
    if not isinstance(request, dict) or not isinstance(summary, dict):
        raise MarketDataStoreError("策略内容无效")
    return {
        "strategy_id": str(row[0]),
        "name": str(row[1]),
        "note": str(row[2]),
        "source_history_id": str(row[3]) if row[3] is not None else None,
        "created_at": str(row[4]),
        "updated_at": str(row[5]),
        "request": request,
        "summary": summary,
        **_tracking_fields(row),
    }


def _decode_preset_row(row: tuple[object, ...]) -> dict[str, object]:
    """Decode one ``screen_presets`` row into an API-ready dict."""

    try:
        payload = json.loads(str(row[5]))
    except json.JSONDecodeError as exc:
        raise MarketDataStoreError("方案内容损坏") from exc
    if not isinstance(payload, dict):
        raise MarketDataStoreError("方案内容无效")
    return {
        "preset_id": str(row[0]),
        "name": str(row[1]),
        "note": str(row[2]),
        "created_at": str(row[3]),
        "updated_at": str(row[4]),
        "payload": payload,
    }


def _decode_candidate_row(row: tuple[object, ...]) -> dict[str, object]:
    """Decode one ``candidate_library`` row into an API-ready dict."""

    try:
        condition = json.loads(str(row[6]))
        evidence = json.loads(str(row[7]))
    except json.JSONDecodeError as exc:
        raise MarketDataStoreError("候选内容损坏") from exc
    if not isinstance(condition, dict) or not isinstance(evidence, dict):
        raise MarketDataStoreError("候选内容无效")
    published = row[10]
    return {
        "candidate_id": str(row[0]),
        "source": str(row[1]),
        "source_history_id": str(row[2]),
        "source_candidate_id": str(row[3]),
        "side": str(row[4]),
        "label": str(row[5]),
        "condition": condition,
        "evidence": evidence,
        "status": str(row[8]),
        "note": str(row[9]),
        "published_strategy_id": None if published is None else str(published),
        "created_at": str(row[11]),
        "updated_at": str(row[12]),
    }


def _preset_record(record: Mapping[str, object]) -> dict[str, object]:
    """Render an in-memory preset record (datetimes) into its API shape."""

    payload = record.get("payload")
    return {
        "preset_id": str(record["preset_id"]),
        "name": str(record["name"]),
        "note": str(record["note"]),
        "created_at": str(record["created_at"]),
        "updated_at": str(record["updated_at"]),
        "payload": dict(payload) if isinstance(payload, Mapping) else {},
    }


def _strategy_record(payload: Mapping[str, object]) -> dict[str, object]:
    """Normalise an in-memory strategy payload the same way a decoded row looks."""

    return {
        "strategy_id": str(payload["strategy_id"]),
        "name": str(payload["name"]),
        "note": str(payload["note"]),
        "source_history_id": payload.get("source_history_id"),
        "created_at": str(payload["created_at"]),
        "updated_at": str(payload["updated_at"]),
        "request": dict(cast(Mapping[str, object], payload["request"])),
        "summary": dict(cast(Mapping[str, object], payload["summary"])),
        "tracking_status": str(payload.get("tracking_status") or STRATEGY_TRACKING_OFF),
        "launched_at": payload.get("launched_at"),
        "last_tracked_date": payload.get("last_tracked_date"),
        "last_error": str(payload.get("last_error") or ""),
    }


def _tracking_date(value: object) -> int:
    """Narrow one ``YYYYMMDD`` observation key."""

    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("追踪交易日期无效")
    try:
        converted = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"追踪交易日期无效: {value}") from exc
    try:
        datetime.strptime(str(converted), "%Y%m%d")
    except ValueError as exc:
        raise ValueError(f"追踪交易日期无效: {converted}") from exc
    return converted


def _trade_date_to_iso(trade_date: int) -> date:
    """Convert a ``YYYYMMDD`` observation key into the DATE column type."""

    return datetime.strptime(str(_tracking_date(trade_date)), "%Y%m%d").date()


def _tracking_number(value: object, label: str) -> float:
    """Narrow one numeric observation field.

    这些值来自重算服务而不是用户输入，但仍然逐个校验：一个 NaN 净值会顺着
    JSON 一路走到页面上的曲线里，并且在持久化后就再也无法挽回了。
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"追踪{label}无效")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"追踪{label}必须是有限数字")
    return number


def _write_tracking_points(
    connection: duckdb.DuckDBPyConnection,
    strategy_id: str,
    points: Iterable[Mapping[str, object]],
) -> int:
    """Upsert observation rows inside an open transaction."""

    rows: list[tuple[object, ...]] = []
    for point in points:
        benchmark = point.get("benchmark_value")
        drawdown = point.get("drawdown")
        rows.append(
            (
                strategy_id,
                _tracking_date(point["trade_date"]),
                _tracking_number(point["equity"], "净值"),
                _tracking_number(point["cash"], "现金"),
                max(0, int(_tracking_number(point["positions_count"], "持仓数"))),
                None if benchmark is None else _tracking_number(benchmark, "基准值"),
                None if drawdown is None else _tracking_number(drawdown, "回撤"),
            )
        )
    if not rows:
        return 0
    connection.executemany(
        f"""
        INSERT INTO {STRATEGY_TRACKING_TABLE} (
            strategy_id, trade_date, equity, cash, positions_count,
            benchmark_value, drawdown
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (strategy_id, trade_date) DO UPDATE SET
            equity = excluded.equity,
            cash = excluded.cash,
            positions_count = excluded.positions_count,
            benchmark_value = excluded.benchmark_value,
            drawdown = excluded.drawdown
        """,
        rows,
    )
    return len(rows)


def _instrument_name_map(connection: duckdb.DuckDBPyConnection) -> dict[tuple[str, str], str]:
    rows = connection.execute(
        "SELECT market, code, name FROM instruments WHERE name IS NOT NULL"
    ).fetchall()
    return {(str(row[0]), str(row[1])): str(row[2]) for row in rows if str(row[2]).strip()}


def _decode_backtest_history_row(
    row: tuple[object, ...],
    instrument_names: Mapping[tuple[str, str], str],
    backtest_type: str,
) -> dict[str, object]:
    """Decode one ``backtest_history`` row into a list-friendly summary.

    摘要在写入时就落库了，但**曲线只在完整 result 里**，而列表页为了性能和体积
    不返回 result；这里把 result 一起解出来只是为了重算摘要（老记录的摘要字段
    可能比现在少），解完就丢，不进返回值。
    """

    try:
        summary: object = json.loads(str(row[7]))
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
    return {
        "history_id": str(row[0]),
        "backtest_type": str(row[1]),
        "created_at": str(row[2]),
        "started_at": str(row[3]),
        "finished_at": str(row[4]),
        "duration_seconds": float(row[5]) if isinstance(row[5], (int, float)) else 0.0,
        "summary": summary,
    }


def _decode_full_backtest_history_row(
    row: tuple[object, ...],
    instrument_names: Mapping[tuple[str, str], str],
) -> dict[str, object]:
    """Decode one ``backtest_history`` row into a complete snapshot."""

    try:
        request = json.loads(str(row[6]))
        result = json.loads(str(row[7]))
        summary = json.loads(str(row[8]))
    except json.JSONDecodeError as exc:
        raise MarketDataStoreError("回测历史内容损坏") from exc
    if not all(isinstance(value, dict) for value in (request, result, summary)):
        raise MarketDataStoreError("回测历史内容无效")
    backtest_type = str(row[1])
    enriched_result = _enrich_backtest_history_names(result, instrument_names)
    enriched_summary = _enrich_backtest_history_names(summary, instrument_names)
    if isinstance(enriched_result, dict):
        result = enriched_result
        if isinstance(enriched_summary, dict):
            summary = dict(enriched_summary)
            summary.update(_backtest_history_summary(result, backtest_type))
    return {
        "history_id": str(row[0]),
        "backtest_type": backtest_type,
        "created_at": str(row[2]),
        "started_at": str(row[3]),
        "finished_at": str(row[4]),
        "duration_seconds": float(row[5]) if isinstance(row[5], (int, float)) else 0.0,
        "request": request,
        "result": result,
        "summary": summary,
    }


_BACKTEST_HISTORY_LIST_COLUMNS = (
    "history_id, backtest_type, created_at, started_at, finished_at, "
    "duration_seconds, result_json, summary_json"
)
_BACKTEST_HISTORY_FULL_COLUMNS = (
    "history_id, backtest_type, created_at, started_at, finished_at, "
    "duration_seconds, request_json, result_json, summary_json"
)


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
                # 与 backtest_history 同步截断：历史记录被淘汰的研究不该继续
                # 留在台账里贡献轮数，否则"删掉重来"反而会抬高别人的门槛。
                stale = connection.execute(
                    """
                    SELECT history_id FROM condition_research_trials
                    ORDER BY created_at DESC, history_id DESC
                    LIMIT 100000 OFFSET ?
                    """,
                    [BACKTEST_HISTORY_LIMIT],
                ).fetchall()
                if stale:
                    connection.executemany(
                        "DELETE FROM condition_research_trials WHERE history_id = ?", stale
                    )
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

    # ------------------------------------------------------------------
    # 策略库：一套可重放的组合回测配置。和 backtest_history 的区别见
    # store_base 里建表处的注释——历史会淘汰，策略不会。
    # ------------------------------------------------------------------

    def strategy_name_taken(self, name: str, *, exclude_strategy_id: str = "") -> bool:
        """Whether a strategy name is already used by another record."""

        normalized = name.strip()
        if not normalized:
            return False
        with self._store._connect(read_only=True) as connection:
            row = connection.execute(
                f"SELECT 1 FROM {SAVED_STRATEGY_TABLE} WHERE name = ? AND strategy_id <> ? LIMIT 1",
                [normalized, exclude_strategy_id.strip()],
            ).fetchone()
        return row is not None

    def save_strategy(
        self,
        strategy_id: str,
        *,
        name: str,
        request: Mapping[str, object],
        summary: Mapping[str, object],
        note: str = "",
        source_history_id: str | None = None,
        created_at: datetime | str | None = None,
    ) -> dict[str, object]:
        """Persist one reusable strategy configuration."""

        self._store._require_writer()
        normalized_id = strategy_id.strip()
        if not normalized_id:
            raise ValueError("策略 ID 不能为空")
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("策略名称不能为空")
        if len(normalized_name) > STRATEGY_NAME_MAX_LENGTH:
            raise ValueError(f"策略名称不能超过 {STRATEGY_NAME_MAX_LENGTH} 个字符")
        normalized_note = note.strip()
        if len(normalized_note) > STRATEGY_NOTE_MAX_LENGTH:
            raise ValueError(f"策略备注不能超过 {STRATEGY_NOTE_MAX_LENGTH} 个字符")
        if self.strategy_name_taken(normalized_name, exclude_strategy_id=normalized_id):
            raise ValueError(f"已存在同名策略: {normalized_name}")
        created = _normalize_timestamp(created_at, _now())
        payload = {
            "strategy_id": normalized_id,
            "name": normalized_name,
            "note": normalized_note,
            "source_history_id": (source_history_id or "").strip() or None,
            "created_at": created,
            "updated_at": created,
            "request": dict(request),
            "summary": dict(summary),
        }
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
            try:
                connection.execute(
                    f"""
                    INSERT INTO {SAVED_STRATEGY_TABLE} (
                        strategy_id, name, note, source_history_id,
                        created_at, updated_at, request_json, summary_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (strategy_id) DO UPDATE SET
                        name = excluded.name,
                        note = excluded.note,
                        source_history_id = excluded.source_history_id,
                        updated_at = excluded.updated_at,
                        request_json = excluded.request_json,
                        summary_json = excluded.summary_json
                    """,
                    [
                        payload["strategy_id"],
                        payload["name"],
                        payload["note"],
                        payload["source_history_id"],
                        created,
                        created,
                        json.dumps(payload["request"], ensure_ascii=False, separators=(",", ":")),
                        json.dumps(payload["summary"], ensure_ascii=False, separators=(",", ":")),
                    ],
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return _strategy_record(payload)

    def list_strategies(self) -> list[dict[str, object]]:
        """Return every saved strategy, most recently updated first."""

        with self._store._connect(read_only=True) as connection:
            rows = connection.execute(
                f"""
                SELECT {_STRATEGY_COLUMNS}
                FROM {SAVED_STRATEGY_TABLE}
                ORDER BY updated_at DESC, strategy_id DESC
                """
            ).fetchall()
        records: list[dict[str, object]] = []
        for row in rows:
            payload = _decode_strategy_row(row)
            payload.pop("request", None)
            records.append(payload)
        return records

    def read_strategy(self, strategy_id: str) -> dict[str, object] | None:
        """Read one saved strategy including its replayable request."""

        with self._store._connect(read_only=True) as connection:
            row = connection.execute(
                f"""
                SELECT {_STRATEGY_COLUMNS}
                FROM {SAVED_STRATEGY_TABLE}
                WHERE strategy_id = ?
                """,
                [strategy_id.strip()],
            ).fetchone()
        if row is None:
            return None
        return _decode_strategy_row(row)

    def update_strategy(
        self,
        strategy_id: str,
        *,
        name: str | None = None,
        note: str | None = None,
    ) -> dict[str, object] | None:
        """Rename or re-note a saved strategy, leaving its request untouched."""

        self._store._require_writer()
        normalized_id = strategy_id.strip()
        if not normalized_id:
            return None
        if name is None and note is None:
            return self.read_strategy(normalized_id)
        normalized_name = name.strip() if isinstance(name, str) else None
        if normalized_name is not None:
            if not normalized_name:
                raise ValueError("策略名称不能为空")
            if len(normalized_name) > STRATEGY_NAME_MAX_LENGTH:
                raise ValueError(f"策略名称不能超过 {STRATEGY_NAME_MAX_LENGTH} 个字符")
            if self.strategy_name_taken(normalized_name, exclude_strategy_id=normalized_id):
                raise ValueError(f"已存在同名策略: {normalized_name}")
        normalized_note = note.strip() if isinstance(note, str) else None
        if normalized_note is not None and len(normalized_note) > STRATEGY_NOTE_MAX_LENGTH:
            raise ValueError(f"策略备注不能超过 {STRATEGY_NOTE_MAX_LENGTH} 个字符")
        assignments: list[str] = []
        parameters: list[object] = []
        if normalized_name is not None:
            assignments.append("name = ?")
            parameters.append(normalized_name)
        if normalized_note is not None:
            assignments.append("note = ?")
            parameters.append(normalized_note)
        assignments.append("updated_at = ?")
        parameters.append(_now())
        parameters.append(normalized_id)
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
            try:
                updated = connection.execute(
                    f"UPDATE {SAVED_STRATEGY_TABLE} SET {', '.join(assignments)} "
                    "WHERE strategy_id = ? RETURNING strategy_id",
                    parameters,
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        if updated is None:
            return None
        return self.read_strategy(normalized_id)

    def update_strategy_summary(
        self,
        strategy_id: str,
        summary: Mapping[str, object],
    ) -> dict[str, object] | None:
        """Replace the stored performance snapshot.

        追踪上线时用它把「研究段绩效」钉进策略里，之后**永不再改**——分母要是
        会动，衰减比就没有意义了。
        """

        self._store._require_writer()
        normalized_id = strategy_id.strip()
        if not normalized_id:
            return None
        with _WRITE_LOCK, self._store._connect() as connection:
            updated = connection.execute(
                f"UPDATE {SAVED_STRATEGY_TABLE} SET summary_json = ?, updated_at = ? "
                "WHERE strategy_id = ? RETURNING strategy_id",
                [
                    json.dumps(dict(summary), ensure_ascii=False, separators=(",", ":")),
                    _now(),
                    normalized_id,
                ],
            ).fetchone()
        if updated is None:
            return None
        return self.read_strategy(normalized_id)

    def delete_strategy(self, strategy_id: str) -> bool:
        """Delete one saved strategy together with its tracking observations."""

        self._store._require_writer()
        normalized_id = strategy_id.strip()
        if not normalized_id:
            return False
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
            try:
                deleted = connection.execute(
                    f"DELETE FROM {SAVED_STRATEGY_TABLE} "
                    "WHERE strategy_id = ? RETURNING strategy_id",
                    [normalized_id],
                ).fetchone()
                if deleted is not None:
                    # 追踪观察值是策略的附属品，留着会变成永远查不到的孤儿行。
                    connection.execute(
                        f"DELETE FROM {STRATEGY_TRACKING_TABLE} WHERE strategy_id = ?",
                        [normalized_id],
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return deleted is not None

    # ------------------------------------------------------------------
    # 筛选方案：公式选股页整套可复现的输入，命名保存、随时加载。和 screen_history
    # 的关系跟策略库/backtest_history 一样——历史记"跑过什么"（满了自动淘汰），
    # 方案记"还想再跑一遍"（只有显式删才消失）。payload_json 只存输入，不存扫描
    # 结果：结果依赖当时行情，存下来下次加载的人会以为它还成立。
    # ------------------------------------------------------------------

    def screen_preset_name_taken(self, name: str, *, exclude_preset_id: str = "") -> bool:
        """Whether a preset name is already used by another record."""

        normalized = name.strip()
        if not normalized:
            return False
        with self._store._connect(read_only=True) as connection:
            row = connection.execute(
                f"SELECT 1 FROM {SCREEN_PRESET_TABLE} WHERE name = ? AND preset_id <> ? LIMIT 1",
                [normalized, exclude_preset_id.strip()],
            ).fetchone()
        return row is not None

    def save_screen_preset(
        self,
        preset_id: str,
        *,
        name: str,
        payload: Mapping[str, object],
        note: str = "",
        created_at: datetime | str | None = None,
    ) -> dict[str, object]:
        """Persist one reusable screening configuration."""

        self._store._require_writer()
        normalized_id = preset_id.strip()
        if not normalized_id:
            raise ValueError("方案 ID 不能为空")
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("方案名称不能为空")
        if len(normalized_name) > PRESET_NAME_MAX_LENGTH:
            raise ValueError(f"方案名称不能超过 {PRESET_NAME_MAX_LENGTH} 个字符")
        normalized_note = note.strip()
        if len(normalized_note) > PRESET_NOTE_MAX_LENGTH:
            raise ValueError(f"方案备注不能超过 {PRESET_NOTE_MAX_LENGTH} 个字符")
        if self.screen_preset_name_taken(normalized_name, exclude_preset_id=normalized_id):
            raise ValueError(f"已存在同名方案: {normalized_name}")
        created = _normalize_timestamp(created_at, _now())
        record = {
            "preset_id": normalized_id,
            "name": normalized_name,
            "note": normalized_note,
            "created_at": created,
            "updated_at": created,
            "payload": dict(payload),
        }
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
            try:
                connection.execute(
                    f"""
                    INSERT INTO {SCREEN_PRESET_TABLE} (
                        preset_id, name, note, created_at, updated_at, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (preset_id) DO UPDATE SET
                        name = excluded.name,
                        note = excluded.note,
                        updated_at = excluded.updated_at,
                        payload_json = excluded.payload_json
                    """,
                    [
                        normalized_id,
                        normalized_name,
                        normalized_note,
                        created,
                        created,
                        json.dumps(record["payload"], ensure_ascii=False, separators=(",", ":")),
                    ],
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return _preset_record(record)

    def list_screen_presets(self) -> list[dict[str, object]]:
        """Return every saved preset, most recently updated first, without payloads."""

        with self._store._connect(read_only=True) as connection:
            rows = connection.execute(
                f"""
                SELECT {_PRESET_COLUMNS}
                FROM {SCREEN_PRESET_TABLE}
                ORDER BY updated_at DESC, preset_id DESC
                """
            ).fetchall()
        records: list[dict[str, object]] = []
        for row in rows:
            payload = _decode_preset_row(row)
            payload.pop("payload", None)
            records.append(payload)
        return records

    def read_screen_preset(self, preset_id: str) -> dict[str, object] | None:
        """Read one saved preset including its replayable payload."""

        with self._store._connect(read_only=True) as connection:
            row = connection.execute(
                f"SELECT {_PRESET_COLUMNS} FROM {SCREEN_PRESET_TABLE} WHERE preset_id = ?",
                [str(preset_id).strip()],
            ).fetchone()
        if row is None:
            return None
        return _decode_preset_row(row)

    def update_screen_preset(
        self,
        preset_id: str,
        *,
        name: str | None = None,
        note: str | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object] | None:
        """Rename, re-note or overwrite the payload of one preset.

        覆盖保存只改 payload 与 updated_at，created_at 不动——「这个方案是什么时候
        建的」是它自己的历史，不该被最后一次保存抹掉。
        """

        self._store._require_writer()
        normalized_id = preset_id.strip()
        if not normalized_id:
            return None
        if name is None and note is None and payload is None:
            return self.read_screen_preset(normalized_id)
        normalized_name = name.strip() if isinstance(name, str) else None
        if normalized_name is not None:
            if not normalized_name:
                raise ValueError("方案名称不能为空")
            if len(normalized_name) > PRESET_NAME_MAX_LENGTH:
                raise ValueError(f"方案名称不能超过 {PRESET_NAME_MAX_LENGTH} 个字符")
            if self.screen_preset_name_taken(normalized_name, exclude_preset_id=normalized_id):
                raise ValueError(f"已存在同名方案: {normalized_name}")
        normalized_note = note.strip() if isinstance(note, str) else None
        if normalized_note is not None and len(normalized_note) > PRESET_NOTE_MAX_LENGTH:
            raise ValueError(f"方案备注不能超过 {PRESET_NOTE_MAX_LENGTH} 个字符")
        assignments: list[str] = []
        parameters: list[object] = []
        if normalized_name is not None:
            assignments.append("name = ?")
            parameters.append(normalized_name)
        if normalized_note is not None:
            assignments.append("note = ?")
            parameters.append(normalized_note)
        if payload is not None:
            assignments.append("payload_json = ?")
            parameters.append(json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")))
        assignments.append("updated_at = ?")
        parameters.append(_now())
        parameters.append(normalized_id)
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
            try:
                updated = connection.execute(
                    f"UPDATE {SCREEN_PRESET_TABLE} SET {', '.join(assignments)} "
                    "WHERE preset_id = ? RETURNING preset_id",
                    parameters,
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        if updated is None:
            return None
        return self.read_screen_preset(normalized_id)

    def delete_screen_preset(self, preset_id: str) -> bool:
        """Delete one saved preset."""

        self._store._require_writer()
        normalized_id = preset_id.strip()
        if not normalized_id:
            return False
        with _WRITE_LOCK, self._store._connect() as connection:
            deleted = connection.execute(
                f"DELETE FROM {SCREEN_PRESET_TABLE} WHERE preset_id = ? RETURNING preset_id",
                [normalized_id],
            ).fetchone()
        return deleted is not None

    # ------------------------------------------------------------------
    # 候选库：条件研究挖出来的候选条件，带证据快照与采纳状态。和 backtest_history
    # 的关系跟策略库一样——历史记"跑过什么"（满了自动淘汰），候选记"我当时认不认可
    # 这条"（只有显式删才消失）。evidence_json 存的是入库那一刻的快照，不回指历史
    # 记录，否则第 21 次研究跑完，最早那批候选的证据就没了。
    # ------------------------------------------------------------------

    def find_candidate_by_source(
        self,
        source_history_id: str,
        source_candidate_id: str,
    ) -> dict[str, object] | None:
        """Look up a candidate by where it came from (入库去重用)。"""

        with self._store._connect(read_only=True) as connection:
            row = connection.execute(
                f"SELECT {_CANDIDATE_COLUMNS} FROM {CANDIDATE_LIBRARY_TABLE} "
                "WHERE source_history_id = ? AND source_candidate_id = ? LIMIT 1",
                [str(source_history_id).strip(), str(source_candidate_id).strip()],
            ).fetchone()
        if row is None:
            return None
        return _decode_candidate_row(row)

    def save_candidate(
        self,
        candidate_id: str,
        *,
        source: str,
        source_history_id: str,
        source_candidate_id: str,
        side: str,
        label: str,
        condition: Mapping[str, object],
        evidence: Mapping[str, object],
        status: str,
        note: str = "",
        published_strategy_id: str | None = None,
        created_at: datetime | str | None = None,
    ) -> dict[str, object]:
        """Persist one mined candidate together with its evidence snapshot."""

        self._store._require_writer()
        normalized_id = candidate_id.strip()
        if not normalized_id:
            raise ValueError("候选 ID 不能为空")
        if status not in _CANDIDATE_STATUSES:
            raise ValueError(f"候选状态无效: {status}")
        normalized_note = note.strip()
        if len(normalized_note) > CANDIDATE_NOTE_MAX_LENGTH:
            raise ValueError(f"候选备注不能超过 {CANDIDATE_NOTE_MAX_LENGTH} 个字符")
        created = _normalize_timestamp(created_at, _now())
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
            try:
                connection.execute(
                    f"""
                    INSERT INTO {CANDIDATE_LIBRARY_TABLE} (
                        candidate_id, source, source_history_id, source_candidate_id,
                        side, label, condition_json, evidence_json, status, note,
                        published_strategy_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (candidate_id) DO UPDATE SET
                        label = excluded.label,
                        evidence_json = excluded.evidence_json,
                        status = excluded.status,
                        note = excluded.note,
                        published_strategy_id = excluded.published_strategy_id,
                        updated_at = excluded.updated_at
                    """,
                    [
                        normalized_id,
                        source,
                        str(source_history_id).strip(),
                        str(source_candidate_id).strip(),
                        side,
                        label,
                        json.dumps(dict(condition), ensure_ascii=False, separators=(",", ":")),
                        json.dumps(dict(evidence), ensure_ascii=False, separators=(",", ":")),
                        status,
                        normalized_note,
                        published_strategy_id,
                        created,
                        created,
                    ],
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        record = self.read_candidate(normalized_id)
        if record is None:  # pragma: no cover - 刚写入的行必然读得到
            raise MarketDataStoreError("候选写入后无法读回")
        return record

    def list_candidates(self, status: str | None = None) -> list[dict[str, object]]:
        """Return stored candidates, most recently updated first."""

        normalized = status.strip() if isinstance(status, str) else ""
        if normalized and normalized not in _CANDIDATE_STATUSES:
            raise ValueError(f"候选状态无效: {normalized}")
        if normalized:
            sql = (
                f"SELECT {_CANDIDATE_COLUMNS} FROM {CANDIDATE_LIBRARY_TABLE} "
                "WHERE status = ? ORDER BY updated_at DESC, candidate_id DESC"
            )
            parameters: list[object] = [normalized]
        else:
            sql = (
                f"SELECT {_CANDIDATE_COLUMNS} FROM {CANDIDATE_LIBRARY_TABLE} "
                "ORDER BY updated_at DESC, candidate_id DESC"
            )
            parameters = []
        with self._store._connect(read_only=True) as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [_decode_candidate_row(row) for row in rows]

    def read_candidate(self, candidate_id: str) -> dict[str, object] | None:
        """Read one stored candidate."""

        with self._store._connect(read_only=True) as connection:
            row = connection.execute(
                f"SELECT {_CANDIDATE_COLUMNS} FROM {CANDIDATE_LIBRARY_TABLE} "
                "WHERE candidate_id = ?",
                [str(candidate_id).strip()],
            ).fetchone()
        if row is None:
            return None
        return _decode_candidate_row(row)

    def update_candidate(
        self,
        candidate_id: str,
        *,
        status: str | None = None,
        note: str | None = None,
        published_strategy_id: str | None = None,
        publish: bool = False,
    ) -> dict[str, object] | None:
        """Change the adoption status, the note, or the linked strategy.

        ``publish=True`` 时才写 published_strategy_id；撤回传 ``publish=True``
        且 ``published_strategy_id=None``，这样"清掉链接"和"不动链接"是两种明
        确的调用，不会靠 None 的歧义蒙混过去。
        """

        self._store._require_writer()
        normalized_id = candidate_id.strip()
        if not normalized_id:
            return None
        normalized_status = status.strip() if isinstance(status, str) else None
        if normalized_status is not None and normalized_status not in _CANDIDATE_STATUSES:
            raise ValueError(f"候选状态无效: {normalized_status}")
        normalized_note = note.strip() if isinstance(note, str) else None
        if normalized_note is not None and len(normalized_note) > CANDIDATE_NOTE_MAX_LENGTH:
            raise ValueError(f"候选备注不能超过 {CANDIDATE_NOTE_MAX_LENGTH} 个字符")
        if normalized_status is None and normalized_note is None and not publish:
            return self.read_candidate(normalized_id)
        assignments: list[str] = []
        parameters: list[object] = []
        if normalized_status is not None:
            assignments.append("status = ?")
            parameters.append(normalized_status)
        if normalized_note is not None:
            assignments.append("note = ?")
            parameters.append(normalized_note)
        if publish:
            assignments.append("published_strategy_id = ?")
            parameters.append(published_strategy_id)
        assignments.append("updated_at = ?")
        parameters.append(_now())
        parameters.append(normalized_id)
        with _WRITE_LOCK, self._store._connect() as connection:
            connection.execute("BEGIN")
            try:
                updated = connection.execute(
                    f"UPDATE {CANDIDATE_LIBRARY_TABLE} SET {', '.join(assignments)} "
                    "WHERE candidate_id = ? RETURNING candidate_id",
                    parameters,
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        if updated is None:
            return None
        return self.read_candidate(normalized_id)

    def delete_candidate(self, candidate_id: str) -> bool:
        """Delete one stored candidate."""

        self._store._require_writer()
        normalized_id = candidate_id.strip()
        if not normalized_id:
            return False
        with _WRITE_LOCK, self._store._connect() as connection:
            deleted = connection.execute(
                f"DELETE FROM {CANDIDATE_LIBRARY_TABLE} WHERE candidate_id = ? "
                "RETURNING candidate_id",
                [normalized_id],
            ).fetchone()
        return deleted is not None

    # ------------------------------------------------------------------
    # 策略追踪：上线日冻结规则，此后每个交易日收盘后重算一次，把「上线前的
    # 回测」和「上线后的模拟观察」并排——用来回答"从今天起它还灵不灵"。
    # 存储上只留每日一行净值（画曲线 + 算衰减比）。交易明细不存，也不存什么
    # "上线状态快照"——重算是全量跑，所需的全部信息就是策略自己的 request_json。
    # ------------------------------------------------------------------

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
        with _WRITE_LOCK, self._store._connect() as connection:
            updated = connection.execute(
                f"UPDATE {SAVED_STRATEGY_TABLE} SET {', '.join(assignments)} "
                "WHERE strategy_id = ? RETURNING strategy_id",
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
            records.append(payload)
        return records

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
