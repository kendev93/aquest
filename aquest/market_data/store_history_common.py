"""Repository operations for screening, discovery, replay, and backtest history."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timezone
from typing import cast

import duckdb

from .store_base import (
    _BACKTEST_HISTORY_TYPES,
    MarketDataStoreError,
    _clean_name,
    _now,
)

# 用户资产表：记录的是用户自己的研究成果，丢了无法重建，所以支持导出导入。
# 行情与指标缓存刻意不在其中——它们可以从数据源重新同步/重算。
ASSET_TABLES: tuple[str, ...] = (
    "backtest_history",
    "screen_history",
    "saved_strategies",
    "strategy_tracking",
    "strategy_tracking_trades",
    "strategy_tracking_plan",
    "strategy_discovery_history",
    "strategy_discovery_replay_history",
    "strategy_pool_entries",
    "strategy_pool_focus",
    "screen_presets",
    "research_presets",
    "candidate_library",
    "stock_groups",
    "stock_group_criteria",
    # 试验台账跟着走：它是累计计数，导出导入丢一次就等于把跨运行的校正清零。
    "condition_research_trials",
)
SAVED_STRATEGY_TABLE = "saved_strategies"
STRATEGY_TRACKING_TABLE = "strategy_tracking"
STRATEGY_TRACKING_TRADES_TABLE = "strategy_tracking_trades"
STRATEGY_TRACKING_PLAN_TABLE = "strategy_tracking_plan"
#: 成交明细归属哪一段。上线日（含）之后成交的算上线后——和净值曲线的分段口径
#: 一致，两边都用"成交日 >= 上线日"判定，否则同一笔交易在两张表上会落在不同段。
TRACKING_PHASE_PRE = "pre"
TRACKING_PHASE_POST = "post"
STRATEGY_NAME_MAX_LENGTH = 60
STRATEGY_NOTE_MAX_LENGTH = 500
SCREEN_PRESET_TABLE = "screen_presets"
RESEARCH_PRESET_TABLE = "research_presets"
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
#: 走不通（见 docs/archive/strategy-tracking-plan.md §2.1 的行情指纹校验），改全量重跑后
#: 这个字段就没用了——重算所需的全部信息就是 request_json 本身。
_STRATEGY_COLUMNS = (
    "strategy_id, name, note, source_history_id, created_at, updated_at, "
    "request_json, summary_json, tracking_status, launched_at, last_tracked_date, last_error, "
    "tracking_state_json"
)
_PRESET_COLUMNS = "preset_id, name, note, created_at, updated_at, payload_json"
_CANDIDATE_COLUMNS = (
    "candidate_id, source, source_history_id, source_candidate_id, side, label, "
    "condition_json, evidence_json, status, note, published_strategy_id, created_at, updated_at"
)
STOCK_GROUP_TABLE = "stock_groups"
STOCK_GROUP_CRITERIA_TABLE = "stock_group_criteria"
STOCK_GROUP_NAME_MAX_LENGTH = 60
STOCK_GROUP_NOTE_MAX_LENGTH = 300
#: 筛选条件快照里那句人话摘要的上限。摘要是给人一眼看的，写长了折叠态放不下。
STOCK_GROUP_CRITERIA_SUMMARY_MAX_LENGTH = 300
#: 快照 payload 序列化后的上限。只存输入（公式/信号/参数），不存扫描结果。
STOCK_GROUP_CRITERIA_PAYLOAD_MAX_LENGTH = 20000
#: 一组最多放多少只。和组合回测 codes 的上限（200）取同一个数：组最终就是要拿去
#: 观察表现的，超过这个数后面那步会直接被拒，不如在存的时候就拦住。
STOCK_GROUP_MAX_ITEMS = 200
#: 来源。手动勾的和从选股结果整批存的用同一个结构，来源只用于展示「这组是怎么来的」。
STOCK_GROUP_SOURCE_MANUAL = "manual"
STOCK_GROUP_SOURCE_SCREEN = "screen"
_STOCK_GROUP_COLUMNS = (
    "group_id, name, note, source, source_history_id, created_at, updated_at, items_json"
)
_STOCK_GROUP_CRITERIA_COLUMNS = "group_id, criteria_json, created_at, updated_at"


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
    if backtest_type == "individual_research":
        return _individual_research_summary(result)
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


def _individual_research_summary(result: Mapping[str, object]) -> dict[str, object]:
    summary: dict[str, object] = {
        key: result[key]
        for key in ("market", "code", "name", "start_date", "end_date", "candidate_count")
        if key in result
    }
    range_value = result.get("date_range")
    if isinstance(range_value, Mapping):
        summary["date_range"] = dict(range_value)
    for board_key, label in (("results_by_sharpe", "sharpe"), ("results_by_return", "return")):
        rows = result.get(board_key)
        if not isinstance(rows, (list, tuple)) or not rows:
            continue
        first = rows[0]
        if not isinstance(first, Mapping):
            continue
        metrics = first.get("metrics")
        validation = metrics.get("validation") if isinstance(metrics, Mapping) else None
        if isinstance(validation, Mapping):
            for key in ("sharpe", "annual_return", "win_rate", "payoff_ratio", "max_drawdown"):
                value = _finite_or_none(validation.get(key))
                if value is not None:
                    summary[f"top_{label}_{key}"] = value

    full_backtests = result.get("full_backtests")
    best_annual_return: float | None = None
    best_backtest: Mapping[str, object] | None = None
    best_performance: Mapping[str, object] | None = None
    if isinstance(full_backtests, Mapping):
        for backtest in full_backtests.values():
            if not isinstance(backtest, Mapping):
                continue
            performance = backtest.get("performance")
            if not isinstance(performance, Mapping):
                continue
            annual_return = _finite_or_none(performance.get("annual_return"))
            if annual_return is None:
                continue
            if best_annual_return is None or annual_return > best_annual_return:
                best_annual_return = annual_return
                best_backtest = backtest
                best_performance = performance
    if (
        best_annual_return is not None
        and best_backtest is not None
        and best_performance is not None
    ):
        summary["best_full_annual_return"] = best_annual_return
        for summary_key, source, key in (
            ("best_full_excess_return", best_backtest, "excess_return"),
            ("best_full_sharpe", best_performance, "sharpe"),
            ("best_full_max_drawdown", best_performance, "max_drawdown"),
        ):
            value = _finite_or_none(source.get(key))
            if value is not None:
                summary[summary_key] = value
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
    raw_state = row[12] if len(row) > 12 else None
    if raw_state is None:
        tracking["tracking_state"] = None
    else:
        try:
            state = json.loads(str(raw_state))
        except json.JSONDecodeError as exc:
            raise MarketDataStoreError("策略追踪快照损坏") from exc
        tracking["tracking_state"] = state if isinstance(state, dict) else None
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


def _decode_stock_group_row(row: tuple[object, ...]) -> dict[str, object]:
    """Decode one ``stock_groups`` row into an API-ready dict."""

    try:
        items = json.loads(str(row[7]))
    except json.JSONDecodeError as exc:
        raise MarketDataStoreError("分组内容损坏") from exc
    if not isinstance(items, list):
        raise MarketDataStoreError("分组内容无效")
    source_history = row[4]
    return {
        "group_id": str(row[0]),
        "name": str(row[1]),
        "note": str(row[2]),
        "source": str(row[3]),
        "source_history_id": None if source_history is None else str(source_history),
        "created_at": str(row[5]),
        "updated_at": str(row[6]),
        "items": items,
    }


def _stock_group_record(record: Mapping[str, object]) -> dict[str, object]:
    """Render an in-memory group record (datetimes) into its API shape."""

    items = cast(Sequence[Mapping[str, object]], record["items"])
    return {
        "group_id": str(record["group_id"]),
        "name": str(record["name"]),
        "note": str(record["note"]),
        "source": str(record["source"]),
        "source_history_id": record.get("source_history_id"),
        "created_at": str(record["created_at"]),
        "updated_at": str(record["updated_at"]),
        "items": [dict(item) for item in items],
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
        "tracking_state": (
            dict(cast(Mapping[str, object], payload["tracking_state"]))
            if isinstance(payload.get("tracking_state"), Mapping)
            else None
        ),
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


def _optional_tracking_date(value: object) -> int | None:
    if value is None or value == "":
        return None
    normalized = str(value).strip().replace("-", "")
    return _tracking_date(normalized)


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


def _write_tracking_trades(
    connection: duckdb.DuckDBPyConnection,
    strategy_id: str,
    rows: Sequence[Mapping[str, object]],
) -> int:
    """Replace one strategy's trades inside an open transaction."""

    connection.execute(
        f"DELETE FROM {STRATEGY_TRACKING_TRADES_TABLE} WHERE strategy_id = ?",
        [strategy_id],
    )
    if rows:
        connection.executemany(
            f"""
            INSERT INTO {STRATEGY_TRACKING_TRADES_TABLE} (
                strategy_id, seq, trade_date, phase, direction,
                market, code, name, size, price, pnl, reason, signal_date,
                commission, stamp_tax, slippage, cost_basis
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                [
                    strategy_id,
                    int(index),
                    int(cast(int, row["trade_date"])),
                    str(row["phase"]),
                    str(row["direction"]),
                    str(row["market"]),
                    str(row["code"]),
                    str(row.get("name") or ""),
                    float(cast(float, row["size"])),
                    float(cast(float, row["price"])),
                    float(cast(float, row.get("pnl") or 0.0)),
                    str(row.get("reason") or ""),
                    _optional_tracking_date(row.get("signal_date")),
                    _finite_or_none(row.get("commission")),
                    _finite_or_none(row.get("stamp_tax")),
                    _finite_or_none(row.get("slippage")),
                    _finite_or_none(row.get("cost_basis")),
                ]
                for index, row in enumerate(rows)
            ],
        )
    return len(rows)


def _write_tracking_plan(
    connection: duckdb.DuckDBPyConnection,
    strategy_id: str,
    payload: Mapping[str, object] | None,
) -> None:
    """Replace or clear a strategy's next-session plan inside a transaction."""

    if payload is None:
        connection.execute(
            f"DELETE FROM {STRATEGY_TRACKING_PLAN_TABLE} WHERE strategy_id = ?",
            [strategy_id],
        )
        return
    encoded = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":"))
    raw_as_of = str(payload.get("as_of") or "").replace("-", "").strip()
    try:
        as_of = _tracking_date(raw_as_of)
    except ValueError:
        # 缺少基准日不应让已经生成的观察曲线和成交记录一起回滚。
        as_of = 0
    connection.execute(
        f"""
        INSERT INTO {STRATEGY_TRACKING_PLAN_TABLE} (
            strategy_id, as_of, payload_json, updated_at
        ) VALUES (?, ?, ?, ?)
        ON CONFLICT (strategy_id) DO UPDATE SET
            as_of = excluded.as_of,
            payload_json = excluded.payload_json,
            updated_at = excluded.updated_at
        """,
        [strategy_id, as_of, encoded, _now()],
    )


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
