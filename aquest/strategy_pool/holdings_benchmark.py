"""榜单模拟盘的第二条参考线：策略池「持仓中」全集的等权净值。

成员口径：与模拟盘同一 objective 的持仓中名单，按**当日快照**变化，不用最新
名单回填历史。历史名单没有整表落盘，这里以最近一次完整信号结果里的持仓快照
为锚点，用每日归档的买入 / 离场名单逐日反推：成员变化只发生在买卖次日开盘，
所以 当日收盘持仓 = 次日收盘持仓 − 次日买入 + 次日离场。

收益口径与股票组一致（``stock_groups.performance``）：复权收盘、等权、每日
再平衡、满仓、不计费用。它回答「榜单前几相对系统平均持仓有没有超额」，不是
跑赢大盘的证明。
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any, cast

from aquest.market_data.models import InstrumentRef, MarketCode
from aquest.market_data.store import DuckDbMarketDataStore
from aquest.stock_groups.performance import adjusted_close_matrix

#: 日再平衡的等权持仓线；图例与悬浮说明都用这一句。
HOLDINGS_BENCHMARK_LABEL = "持仓等权 · 每日再平衡"


def holdings_benchmark(
    store: DuckDbMarketDataStore,
    *,
    objective: str,
    equity_dates: Sequence[str],
    start_date: str | None = None,
) -> dict[str, object] | None:
    """按模拟盘的净值日期返回持仓等权曲线；推不出成员时返回 None。

    ``start_date`` 是首批信号日：给曲线补一个信号日收盘的期初点，
    首个交易日的收益才能和模拟盘按同一窗口比较。
    """

    days = [date.fromisoformat(text) for text in equity_dates]
    if not days:
        return None
    base = date.fromisoformat(start_date) if start_date else None
    if base is not None and base < days[0]:
        days.insert(0, base)
    if len(days) < 2:
        return None
    anchor = _latest_holdings(store, objective)
    if anchor is None:
        return None
    anchor_day, members = anchor
    if not members:
        return None
    history = _membership_history(store, objective, anchor_day, members, earliest=days[0])
    if base is not None and days[0] == base and base.isoformat() not in history:
        # 期初那天没有归档就退回从第一个净值日起算，别整条基准一起丢掉。
        days = days[1:]
        if len(days) < 2:
            return None
    instruments = sorted({key for holding in history.values() for key in holding})
    if not instruments:
        return None
    refs = [
        InstrumentRef(
            market=cast(MarketCode, key.split(":", 1)[0]),
            code=key.split(":", 1)[1],
            instrument_type="stock",
        )
        for key in instruments
    ]
    prices = adjusted_close_matrix(
        store,
        store.read_close_matrix(refs, start_date=days[0], end_date=days[-1]),
    )
    if prices.empty:
        return None
    # 停牌日沿用前收盘：持有者当天没有价格变化，不是收益为 0 也不是跳过。
    prices = prices.ffill()
    totals = [1.0]
    series = [days[0]]
    value = 1.0
    for previous, current in zip(days, days[1:], strict=False):
        holding = history.get(previous.isoformat())
        if holding is None:
            break
        before, after = _day_key(previous), _day_key(current)
        if before not in prices.index or after not in prices.index:
            break
        changes = [
            change
            for key in holding
            if (change := _close_return(prices, before, after, key)) is not None
        ]
        value *= 1 + (sum(changes) / len(changes) if changes else 0.0)
        series.append(current)
        totals.append(value)
    if len(series) < 2:
        return None
    return {
        "label": HOLDINGS_BENCHMARK_LABEL,
        "dates": [day.isoformat() for day in series],
        "totals": totals,
    }


def _latest_holdings(
    store: DuckDbMarketDataStore,
    objective: str,
) -> tuple[date, set[str]] | None:
    """最近一次完整信号扫描里的持仓中名单；只保留最近若干天，够当反推锚点。"""

    with store.state_read_session() as connection:
        row = connection.execute(
            """
            SELECT result_json FROM strategy_jobs
            WHERE operation = 'market_strategy_pool_tracking'
              AND status = 'completed' AND result_json IS NOT NULL
              AND json_extract_string(request_json, '$.payload.mode') = 'signals'
              AND json_extract_string(request_json, '$.payload.market') IS NULL
            ORDER BY updated_at DESC LIMIT 1
            """
        ).fetchone()
    if row is None or row[0] is None:
        return None
    try:
        result = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    boards = result.get("results_by_objective")
    board = boards.get(objective) if isinstance(boards, Mapping) else None
    if not isinstance(board, Mapping):
        return None
    model = board.get("model_portfolio")
    if not isinstance(model, Mapping) or model.get("available") is False:
        return None
    holdings = model.get("holdings")
    as_of = board.get("as_of_date") or result.get("as_of_date")
    if not isinstance(holdings, list) or not isinstance(as_of, str):
        return None
    try:
        anchor_day = date.fromisoformat(as_of)
    except ValueError:
        return None
    members = {key for row in holdings if isinstance(row, Mapping) and (key := _member_key(row))}
    return anchor_day, members


def _membership_history(
    store: DuckDbMarketDataStore,
    objective: str,
    anchor_day: date,
    anchor_members: set[str],
    *,
    earliest: date,
) -> dict[str, set[str]]:
    """从锚点持仓逐日反推每日收盘成员；缺归档的那几天按成员不变处理。"""

    with store.state_read_session() as connection:
        rows = connection.execute(
            """
            SELECT trade_date, entries_json FROM pool_simulation_signals
            WHERE objective = ? AND trade_date <= ? AND trade_date >= ?
            ORDER BY trade_date DESC
            """,
            [objective, anchor_day, earliest],
        ).fetchall()
    history: dict[str, set[str]] = {}
    current = set(anchor_members)
    anchored = False
    for raw_day, entries_json in rows:
        day = raw_day if isinstance(raw_day, date) else date.fromisoformat(str(raw_day)[:10])
        if not anchored:
            # 锚点日（或锚点之前最近一份归档）的收盘成员就是当前持仓快照；
            # 中间缺归档的交易无法反推，按没有变化处理。
            history[day.isoformat()] = set(current)
            anchored = True
            continue
        try:
            data = json.loads(entries_json)
        except (TypeError, ValueError):
            history[day.isoformat()] = set(current)
            continue
        buys = {key for row in _rows(data.get("buys")) if (key := _member_key(row))}
        exits = {key for row in _rows(data.get("exits")) if (key := _member_key(row))}
        current = (current - buys) | exits
        history[day.isoformat()] = set(current)
    return history


def _rows(value: object) -> list[Mapping[str, Any]]:
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def _member_key(row: Mapping[str, Any]) -> str | None:
    market = row.get("market")
    code = row.get("code")
    if not isinstance(market, str) or not isinstance(code, str) or not market or not code:
        return None
    return f"{market.upper()}:{code}"


def _day_key(day: date) -> int:
    return int(day.strftime("%Y%m%d"))


def _close_return(
    prices: Any,
    before: int,
    after: int,
    key: str,
) -> float | None:
    if key not in prices.columns:
        return None
    try:
        first = float(prices.at[before, key])
        last = float(prices.at[after, key])
    except (KeyError, TypeError, ValueError):
        return None
    if not (math.isfinite(first) and math.isfinite(last)) or first <= 0 or last <= 0:
        return None
    return last / first - 1
