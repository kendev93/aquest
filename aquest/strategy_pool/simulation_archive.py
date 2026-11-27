"""供榜单模拟盘回放使用的轻量信号归档。"""

import json
from collections.abc import Mapping
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import duckdb

from aquest.market_data.pool_simulation_schema import ARCHIVE_DDL
from aquest.market_data.sync_session import shanghai_now

from .tracking_store import dumps

SIGNAL_METRICS = ("annual_return", "sharpe", "win_rate", "payoff_ratio")
SIGNAL_RANKS = (*SIGNAL_METRICS, "default", "composite")
RANK_FIELDS = ("postLaunchReturn", "benchmark", "excess", "holdingReturn", "createdReturn")


def archive_simulation_signals(
    connection: duckdb.DuckDBPyConnection,
    job_id: str,
    request: Mapping[str, Any],
    result: Mapping[str, object],
    *,
    finished_at: datetime | None = None,
) -> None:
    payload = request.get("payload")
    boards = result.get("results_by_objective")
    if (
        not isinstance(payload, Mapping)
        or payload.get("mode") != "signals"
        or payload.get("market")
        or not isinstance(boards, Mapping)
    ):
        return
    # 个股补跑和旧版没有排名字段的结果都不能代替当日完整榜单。
    rank_version = result.get("simulation_rank_version")
    if (rank_version not in (1, 2) and finished_at is None) or result.get("failed_symbols"):
        return
    as_of = payload.get("as_of_date")
    if not isinstance(as_of, str):
        return
    try:
        day = date.fromisoformat(as_of)
    except ValueError:
        return
    archived_at = (shanghai_now(finished_at) if finished_at else shanghai_now()).replace(
        tzinfo=None
    )
    if archived_at.date() < day or (archived_at.date() == day and archived_at.time() < time(15)):
        return
    connection.execute(ARCHIVE_DDL)
    follow_keys = set()
    if rank_version == 2:
        follow_keys = {
            (market, code)
            for market, code in connection.execute(
                "SELECT market, code FROM strategy_pool_focus "
                "UNION SELECT market, code FROM strategy_pool_focus_members"
            ).fetchall()
        }
    for objective in ("annual_return", "sharpe"):
        board = boards.get(objective)
        if not isinstance(board, Mapping):
            continue
        buys = board.get("buy_signals")
        sells = board.get("sell_signals")
        if not isinstance(buys, list) or not isinstance(sells, list):
            continue
        model = board.get("model_portfolio")
        holdings = model.get("holdings", []) if isinstance(model, Mapping) else []
        excluded = {(r.get("market"), r.get("code")) for r in sells if isinstance(r, Mapping)}
        excluded.update(
            (r.get("market"), r.get("code"))
            for r in holdings
            if isinstance(r, Mapping) and r.get("pending_exit_signal_date") == as_of
        )
        entries = [
            {
                key: row.get(key)
                for key in (
                    "market",
                    "code",
                    "name",
                    "tracking_version_id",
                    "days",
                    *RANK_FIELDS,
                    *SIGNAL_METRICS,
                )
            }
            for row in buys
            if isinstance(row, Mapping)
            and row.get("tracking_version_id")
            and (row.get("market"), row.get("code")) not in excluded
        ]
        if finished_at is not None and rank_version != 2:
            # 只能用信号当日的跟踪指标，不能用后来算出的表现回填历史排名。
            versions = [entry["tracking_version_id"] for entry in entries]
            snapshots = {}
            if versions:
                snapshots = dict(
                    connection.execute(
                        "SELECT version_id, summary_json FROM pool_tracking_versions "
                        "WHERE version_id IN (SELECT unnest(?))",
                        [versions],
                    ).fetchall()
                )
            reconstructed = True
            for entry in entries:
                summary = json.loads(snapshots.get(entry["tracking_version_id"]) or "{}")
                full = summary.get("full_period")
                if summary.get("as_of_date") != as_of or not isinstance(full, Mapping):
                    reconstructed = False
                    break
                entry.update({key: full.get(key) for key in SIGNAL_METRICS})
            if not reconstructed:
                continue
            archive_version = 2
        else:
            archive_version = 2 if rank_version == 2 else 1
            reconstructed = False
        # 保留首份已发布的完整信号，重复扫描不能改写历史模拟成交的依据。
        for entry in entries:
            entry["followed"] = (entry["market"], entry["code"]) in follow_keys
        exits = [
            {
                "market": row["market"],
                "code": row["code"],
                "tracking_version_id": row.get("tracking_version_id"),
                "reason": row.get("reason") or "卖出条件",
            }
            for row in sells
            if isinstance(row, Mapping) and "market" in row and "code" in row
        ]
        exits.extend(
            {
                "market": row.get("market"),
                "code": row.get("code"),
                "reason": row.get("pending_exit_reason"),
            }
            for row in holdings
            if isinstance(row, Mapping) and row.get("pending_exit_signal_date") == as_of
        )
        connection.execute(
            "INSERT INTO pool_simulation_signals VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            [
                day,
                objective,
                job_id,
                dumps(
                    {
                        "buys": entries,
                        "exits": exits,
                        "rank_version": archive_version,
                        "reconstructed": reconstructed,
                    }
                ),
                archived_at,
            ],
        )


def backfill_simulation_signals(connection: duckdb.DuckDBPyConnection) -> None:
    """启动时尝试恢复仍有完整结果和同日跟踪摘要的最近信号。"""

    rows = connection.execute(
        """
        WITH recent AS (
            SELECT job_id, TRY_CAST(json_extract_string(request_json, '$.payload.as_of_date')
                   AS DATE) AS signal_date
            FROM strategy_jobs
            WHERE operation = 'market_strategy_pool_tracking' AND status = 'completed'
              AND has_result AND json_extract_string(request_json, '$.payload.mode') = 'signals'
              AND json_extract_string(request_json, '$.payload.market') IS NULL
            QUALIFY row_number() OVER (PARTITION BY signal_date ORDER BY updated_at DESC) = 1
        )
        SELECT j.job_id, j.request_json, j.state_json, j.result_json
        FROM (
            SELECT job_id FROM recent r WHERE signal_date IS NOT NULL
              AND (SELECT count(*) FROM pool_simulation_signals s
                   WHERE s.trade_date = r.signal_date) < 2
            ORDER BY signal_date DESC LIMIT 10
        ) r JOIN strategy_jobs j USING (job_id)
        """
    ).fetchall()
    for job_id, request_json, state_json, result_json in rows:
        finished = json.loads(state_json).get("finished_at")
        if not isinstance(finished, (int, float)):
            continue
        archive_simulation_signals(
            connection,
            job_id,
            json.loads(request_json),
            json.loads(result_json),
            finished_at=datetime.fromtimestamp(finished, ZoneInfo("Asia/Shanghai")),
        )
