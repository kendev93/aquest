"""榜单模拟盘使用固定策略版本和独立的现金、持仓与成交。"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, time
from threading import Lock
from typing import Any, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

from aquest.backtest.profiles import profile_from_config
from aquest.market_data.adapter import DuckDbMarketDataAdapter
from aquest.market_data.models import MarketCode
from aquest.market_data.pool_simulation_schema import ARCHIVE_DDL, SIMULATION_DDL
from aquest.market_data.store import DuckDbMarketDataStore
from aquest.market_data.store_base import STATE_WRITE_LOCK
from aquest.market_data.sync_session import shanghai_now
from aquest.portfolio_backtest.models import PortfolioBacktestConfig
from aquest.portfolio_backtest.risk import _risk_trigger, _risk_trigger_label
from aquest.portfolio_backtest.service import PortfolioBacktestService
from aquest.portfolio_backtest.simulation_models import _Position
from aquest.web.schemas import PortfolioBacktestRequest

from .simulation_archive import SIGNAL_METRICS, SIGNAL_RANKS
from .tracking_store import dumps

logger = logging.getLogger(__name__)
SLOTS = 5


def _symbol(row: dict[str, Any]) -> str:
    return f"{row['market']}{row['code']}"


def _score(row: dict[str, Any], field: str) -> float:
    value = row.get(field)
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else -math.inf


def rank_buys(rows: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    field = config["ranking"]
    if field == "default":
        return sorted(rows, key=lambda row: not row.get("followed"))
    if field in SIGNAL_METRICS:
        return sorted(rows, key=lambda row: (-_score(row, field), _symbol(row)))
    if field == "composite":
        complete = [
            row for row in rows if all(math.isfinite(_score(row, k)) for k in SIGNAL_METRICS)
        ]
        scores: dict[str, float] = {}
        for metric in SIGNAL_METRICS:
            ranks: dict[float, int] = {}
            for index, value in enumerate(sorted(_score(row, metric) for row in complete)):
                ranks.setdefault(value, index)
            for row in complete:
                key = _symbol(row)
                scores[key] = scores.get(key, 0) + ranks[_score(row, metric)]
        return sorted(rows, key=lambda row: (-scores.get(_symbol(row), -math.inf), _symbol(row)))
    direction = 1 if config["direction"] == "asc" else -1
    selected = [
        row
        for row in rows
        if isinstance(row.get(field), (int, float))
        and math.isfinite(row[field])
        and isinstance(row.get("postLaunchReturn"), (int, float))
        and (row.get("days") or 0) >= config["min_days"]
    ]
    return sorted(
        selected,
        key=lambda row: (direction * row[field], -(row.get("days") or 0), _symbol(row)),
    )


def _order_fees(size: float, price: float, config: PortfolioBacktestConfig, *, sell: bool) -> float:
    profile = profile_from_config(
        "stock",
        commission=config.commission,
        min_commission=config.min_commission,
        stamp_tax=config.stamp_tax,
    )
    value = size * price
    return (
        max(value * profile.commission, profile.min_commission)
        + size * config.slippage
        + (value * profile.stamp_tax if sell else 0)
    )


def execute_orders(
    state: dict[str, Any],
    buys: list[dict[str, Any]],
    signal_day: str,
    trade_day: str,
    quote: Callable[[str], tuple[float, float] | None],
    version_config: Callable[[dict[str, Any]], PortfolioBacktestConfig | None],
) -> None:
    """先卖后买；没有卖出成交的仓位不会变成买入名额。"""

    positions = state["positions"]
    initial = not state["started"]
    sold: set[str] = set()
    for symbol, order in list(state["pending"].items()):
        position = positions[symbol]
        prices = quote(symbol)
        config = version_config(position)
        if prices is None or config is None:
            continue
        price = prices[0]
        size = position["size"]
        fee = _order_fees(size, price, config, sell=True)
        proceeds = price * size - fee
        state["cash"] += proceeds
        state["trades"].append(
            {
                "symbol": symbol,
                "date": trade_day,
                "signal_date": order["signal_date"],
                "direction": "SELL",
                "price": price,
                "size": size,
                "fee": fee,
                "reason": order["reason"],
                "pnl": proceeds - position["cost"],
                "version_id": position["tracking_version_id"],
            }
        )
        state["cooldown"][symbol] = order.get("cooldown", 0) + 1
        del positions[symbol], state["pending"][symbol]
        sold.add(symbol)

    available = SLOTS if initial else len(sold)
    # 每个空位最多用当前总权益的五分之一，未成交资金继续留在现金中。
    open_equity = state["cash"] + sum(
        position["size"] * (prices[0] if (prices := quote(symbol)) else position["mark"])
        for symbol, position in positions.items()
    )
    for rank, row in enumerate(buys, start=1):
        if available <= 0:
            break
        symbol = _symbol(row)
        if symbol in positions or symbol in sold or state["cooldown"].get(symbol, 0):
            continue
        prices = quote(symbol)
        config = version_config(row) if prices else None
        if prices is None or config is None:
            continue
        price, close = prices
        budget = min(state["cash"], open_equity / SLOTS)
        lot = profile_from_config(
            "stock",
            commission=config.commission,
            min_commission=config.min_commission,
            stamp_tax=config.stamp_tax,
        ).lot_size
        size = int(budget / price / lot) * lot
        while size and size * price + _order_fees(size, price, config, sell=False) > budget:
            size -= lot
        if not size:
            continue
        fee = _order_fees(size, price, config, sell=False)
        cost = size * price + fee
        state["cash"] -= cost
        positions[symbol] = {
            "market": row["market"],
            "code": row["code"],
            "name": row.get("name"),
            "tracking_version_id": row["tracking_version_id"],
            "size": size,
            "entry_price": price,
            "highest_price": price,
            "mark": close,
            "cost": cost,
            "entry_date": trade_day,
            "applied_corporate_actions": [],
        }
        state["trades"].append(
            {
                "symbol": symbol,
                "date": trade_day,
                "signal_date": signal_day,
                "direction": "BUY",
                "price": price,
                "size": size,
                "fee": fee,
                "reason": "成立建仓" if initial else "卖出后补仓",
                "rank": rank,
                "version_id": row["tracking_version_id"],
            }
        )
        available -= 1
    state["started"] = True
    for symbol, position in positions.items():
        if prices := quote(symbol):
            position["mark"] = prices[1]
    state["cooldown"] = {symbol: days - 1 for symbol, days in state["cooldown"].items() if days > 1}


class PoolSimulations:
    def __init__(self, store: DuckDbMarketDataStore) -> None:
        self.store = store
        self.adapter = DuckDbMarketDataAdapter(store)
        self.engine = PortfolioBacktestService(adapter=self.adapter)
        self.lock = Lock()

    def initialize(self) -> None:
        with self.store.state_write_transaction() as connection:
            connection.execute(ARCHIVE_DDL)
            connection.execute(SIMULATION_DDL)

    def signal_days(self, objective: str, ranking: str = "") -> list[str]:
        with STATE_WRITE_LOCK, self.store.state_read_session() as connection:
            return [
                str(row[0])
                for row in connection.execute(
                    "SELECT trade_date FROM pool_simulation_signals WHERE objective = ? "
                    "AND (NOT ? OR json_extract_string(entries_json, '$.rank_version') = '2') "
                    "ORDER BY trade_date DESC",
                    [objective, ranking in SIGNAL_RANKS],
                ).fetchall()
            ]

    def create(self, config: dict[str, Any]) -> str:
        days = self.signal_days(config["objective"], config["ranking"])
        if not days or config["start_date"] not in days:
            raise ValueError("该日期没有可回放的完整买入信号榜")
        job_id = uuid4().hex
        state = {
            "cash": config["initial_cash"],
            "positions": {},
            "pending": {},
            "cooldown": {},
            "trades": [],
            "equity": [],
            "started": False,
            "paused": False,
            "cursor": None,
            "minimum_signal_date": config["start_date"],
            "warning": None,
        }
        with self.store.state_write_transaction() as connection:
            connection.execute(
                "INSERT INTO pool_simulations VALUES (?, ?, ?)",
                [job_id, dumps(config), dumps(state)],
            )
        return job_id

    def _read(
        self, simulation_id: str | None = None
    ) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
        with STATE_WRITE_LOCK, self.store.state_read_session() as connection:
            rows = connection.execute(
                "SELECT simulation_id, config_json, state_json FROM pool_simulations "
                "WHERE ? IS NULL OR simulation_id = ? ORDER BY simulation_id",
                [simulation_id, simulation_id],
            ).fetchall()
        return [(str(key), json.loads(config), json.loads(state)) for key, config, state in rows]

    def _save(self, key: str, state: dict[str, Any]) -> None:
        with self.store.state_write_transaction() as connection:
            connection.execute(
                "UPDATE pool_simulations SET state_json = ? WHERE simulation_id = ?",
                [dumps(state), key],
            )

    def set_paused(self, key: str, paused: bool) -> None:
        with self.lock:
            rows = self._read(key)
            if not rows:
                raise KeyError(key)
            _, config, state = rows[0]
            if state["paused"] and not paused:
                now = shanghai_now()
                latest = self.store.latest_trade_date()
                today = self.store.latest_market_trading_date(now.date())
                if today == now.date() and now.time() >= time(9, 30):
                    latest = today
                if latest is not None:
                    state["minimum_signal_date"] = max(str(latest), config["start_date"])
                state["pending"] = {}
            state["paused"] = paused
            self._save(key, state)

    def read(
        self, key: str | None = None, *, offset: int = 0, limit: int = 50
    ) -> list[dict[str, Any]]:
        items = []
        for ident, config, state in self._read(key):
            equity = state["equity"]
            high = config["initial_cash"]
            drawdown = 0.0
            for point in equity:
                high = max(high, point["value"])
                drawdown = max(drawdown, 1 - point["value"] / high)
            value = equity[-1]["value"] if equity else high
            output = {
                **config,
                "id": ident,
                "paused": state["paused"],
                "warning": state["warning"],
                "cash": state["cash"],
                "value": value,
                "total_return": value / config["initial_cash"] - 1,
                "max_drawdown": drawdown,
                "equity": equity,
                "holding_count": len(state["positions"]),
                "total_fees": sum(t["fee"] for t in state["trades"]),
                "turnover": sum(t["size"] * t["price"] for t in state["trades"])
                / config["initial_cash"],
            }
            if key is not None:
                output.update(
                    positions=list(state["positions"].values()),
                    trades_total=len(state["trades"]),
                    trades=list(reversed(state["trades"]))[offset : offset + limit],
                )
            items.append(output)
        return items

    def _version(self, row: dict[str, Any], day: date) -> PortfolioBacktestConfig | None:
        with STATE_WRITE_LOCK, self.store.state_read_session() as connection:
            found = connection.execute(
                "SELECT candidate_json FROM pool_tracking_versions "
                "WHERE version_id = ? AND market = ? AND code = ?",
                [row["tracking_version_id"], row["market"], row["code"]],
            ).fetchone()
        if found is None:
            return None
        candidate = json.loads(found[0])
        payload = dict(candidate["config"])
        payload.update(
            universe="codes",
            codes=[f"{row['market']}:{row['code']}"],
            selected_signals=[],
            ranking_value=None,
            max_positions=1,
            single_security_mode=True,
            date_mode="custom",
            start_date=row.get("entry_date", str(day)),
            end_date=str(day),
            buy_rule=candidate.get("buy_rule") or payload.get("buy_rule"),
            sell_rule=candidate.get("sell_rule") or payload.get("sell_rule"),
        )
        fields = PortfolioBacktestRequest.model_fields
        return PortfolioBacktestRequest.model_validate(
            {key: value for key, value in payload.items() if key in fields}
        ).to_config()

    def _step(
        self,
        state: dict[str, Any],
        config: dict[str, Any],
        rows: list[dict[str, Any]],
        signal_day: date,
        trade_day: date,
    ) -> None:
        quotes: dict[str, tuple[float, float] | None] = {}

        def quote(symbol: str) -> tuple[float, float] | None:
            if symbol not in quotes:
                bars = self.store.read_bars(
                    symbol[:2], symbol[2:], start_date=trade_day, end_date=trade_day
                )
                prices = (
                    (float(bars.iloc[0]["open"]), float(bars.iloc[0]["close"]))
                    if not bars.empty
                    else None
                )
                quotes[symbol] = (
                    prices if prices and all(p > 0 and math.isfinite(p) for p in prices) else None
                )
            return quotes[symbol]

        versions: dict[str, PortfolioBacktestConfig | None] = {}

        def version(row: dict[str, Any]) -> PortfolioBacktestConfig | None:
            key = row["tracking_version_id"]
            if key not in versions:
                versions[key] = self._version(row, trade_day)
            return versions[key]

        self.engine._refresh_corporate_action_cache()
        for symbol, row in state["positions"].items():
            pinned = version(row)
            if pinned is None:
                raise ValueError(f"{symbol} 缺少建仓时的策略版本")
            ref = self.adapter.stock_ref("", cast(MarketCode, row["market"]), row["code"])
            raw = self.store.read_bars(row["market"], row["code"], end_date=signal_day)
            if raw.empty:
                continue
            context = self.engine._build_context(
                ref, replace(pinned, end_date=signal_day), None, frame=raw
            )
            if context is None:
                continue
            entry = int(row["entry_date"].replace("-", ""))
            position = _Position(
                market=row["market"],
                code=row["code"],
                instrument_type="stock",
                board=context.board,
                size=row["size"],
                entry_price=row["entry_price"],
                entry_date=entry,
                highest_price=row["highest_price"],
                buy_commission=0,
                buy_slippage=0,
                applied_corporate_actions=set(row["applied_corporate_actions"]),
            )
            day_number = int(signal_day.strftime("%Y%m%d"))
            idx = context.date_to_index.get(day_number)
            state["cash"] = self.engine._apply_corporate_actions(
                day_number, {symbol: position}, {symbol: context}, state["cash"], {}
            )
            if idx is not None and symbol not in state["pending"]:
                trigger = _risk_trigger(context, idx, position, pinned)
                reason = (
                    _risk_trigger_label(trigger[0])
                    if trigger
                    else "卖出条件"
                    if bool(context.sell_mask[idx])
                    else "持有到期"
                    if pinned.max_hold_days
                    and self.engine._held_enough_bars(context, position, idx, pinned.max_hold_days)
                    else None
                )
                if reason:
                    state["pending"][symbol] = {
                        "signal_date": str(signal_day),
                        "reason": reason,
                        "cooldown": trigger[1] if trigger else 0,
                    }
            # 开盘除权只用于成交和记账，买卖决策的指标帧止于信号日。
            if quote(symbol):
                execution_day = int(trade_day.strftime("%Y%m%d"))
                action_context = replace(context, date_to_index={execution_day: 0})
                state["cash"] = self.engine._apply_corporate_actions(
                    execution_day,
                    {symbol: position},
                    {symbol: action_context},
                    state["cash"],
                    {},
                )
            row.update(
                size=position.size,
                highest_price=position.highest_price,
                entry_price=position.entry_price,
            )
            row["applied_corporate_actions"] = sorted(position.applied_corporate_actions)
        execute_orders(
            state, rank_buys(rows, config), str(signal_day), str(trade_day), quote, version
        )
        state["equity"].append(
            {
                "date": str(trade_day),
                "value": state["cash"]
                + sum(p["size"] * p["mark"] for p in state["positions"].values()),
            }
        )
        state["cursor"] = str(signal_day)

    def advance(self) -> None:
        if not self.lock.acquire(blocking=False):
            return
        try:
            accounts = [row for row in self._read() if not row[2]["paused"]]
            if not accounts:
                return
            now = shanghai_now()
            observed = self.store.read_completed_trading_dates()
            completed = {
                day
                for day in observed
                if day < now.date() or (day == now.date() and now.time() >= time(15))
            }
            days: list[date] = []
            if completed:
                earliest = min(
                    date.fromisoformat(state["minimum_signal_date"]) for _, _, state in accounts
                )
                dates = {day for day in completed if day >= earliest}
                last_day = max(completed)
                for lo, hi in self.store.read_authoritative_calendar_coverage():
                    start, end = max(lo, earliest), min(hi, last_day)
                    if start <= end:
                        dates.update(self.store.read_authoritative_trading_dates(start, end))
                days = sorted(dates)
            for key, config, state in accounts:
                try:
                    self._advance_account(key, config, state, days, completed)
                except Exception:  # noqa: BLE001 - 单个盘的错误不挡其他模拟盘推进。
                    logger.exception("榜单模拟盘推进失败: %s", key)
                    _, _, saved = self._read(key)[0]
                    saved["warning"] = "本轮推进暂不可用，已保留上次成功成交与净值"
                    self._save(key, saved)
        finally:
            self.lock.release()

    def _advance_account(
        self,
        key: str,
        config: dict[str, Any],
        state: dict[str, Any],
        days: list[date],
        completed: set[date],
    ) -> None:
        with STATE_WRITE_LOCK, self.store.state_read_session() as connection:
            rows = connection.execute(
                "SELECT trade_date, entries_json, archived_at FROM pool_simulation_signals "
                "WHERE objective = ? AND trade_date >= ? AND trade_date > ?",
                [
                    config["objective"],
                    state["minimum_signal_date"],
                    state["cursor"] or "1900-01-01",
                ],
            ).fetchall()
        archives = {day: (entries, saved_at) for day, entries, saved_at in rows}
        processed = 0
        for signal_day, trade_day in zip(days, days[1:]):
            if str(signal_day) < state["minimum_signal_date"] or str(signal_day) <= (
                state["cursor"] or ""
            ):
                continue
            if trade_day not in completed:
                state["warning"] = f"等待 {trade_day} 的完整行情，尚未模拟成交"
                self._save(key, state)
                return
            if signal_day not in archives:
                state["warning"] = f"缺少 {signal_day} 的完整历史信号，等待补齐后继续"
                self._save(key, state)
                return
            entries, saved_at = archives[signal_day]
            opening = datetime.combine(trade_day, time(9, 30), ZoneInfo("Asia/Shanghai"))
            if saved_at.replace(tzinfo=ZoneInfo("Asia/Shanghai")) >= opening:
                state["warning"] = f"{signal_day} 的信号在 {trade_day} 开盘后生成，已跳过该批次"
                state["cursor"] = str(signal_day)
                self._save(key, state)
                continue
            data = json.loads(entries)
            if config["ranking"] in SIGNAL_RANKS and data.get("rank_version") != 2:
                state["warning"] = f"{signal_day} 的旧归档没有信号榜排序指标，无法推进"
                self._save(key, state)
                return
            self._step(state, config, data["buys"], signal_day, trade_day)
            state["warning"] = None
            self._save(key, state)
            processed += 1
            if processed >= 20:
                break

    async def run(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.advance)
            except Exception:  # noqa: BLE001 - 保留上次已提交的模拟盘状态，下一轮重试。
                logger.exception("榜单模拟盘推进失败")
            await asyncio.sleep(60)
