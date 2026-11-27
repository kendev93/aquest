"""供榜单模拟盘回放使用的轻量信号归档。"""

from collections.abc import Mapping
from datetime import date, time
from typing import Any

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
    if rank_version not in (1, 2) or result.get("failed_symbols"):
        return
    as_of = payload.get("as_of_date")
    if not isinstance(as_of, str):
        return
    try:
        day = date.fromisoformat(as_of)
    except ValueError:
        return
    archived_at = shanghai_now().replace(tzinfo=None)
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
                dumps({"buys": entries, "exits": exits, "rank_version": rank_version}),
                archived_at,
            ],
        )
