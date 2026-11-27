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
from time import monotonic
from typing import Any, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

from aquest.backtest.profiles import profile_from_config
from aquest.market_data.adapter import DuckDbMarketDataAdapter
from aquest.market_data.benchmark import (
    BENCHMARK_LABEL,
    BENCHMARK_START_DATE,
    BENCHMARK_SYMBOL,
)
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

from .holdings_benchmark import holdings_benchmark
from .simulation_archive import SIGNAL_METRICS, required_rank_version
from .simulation_metrics import day_return, detail_metrics, series_return
from .tracking_store import dumps

logger = logging.getLogger(__name__)
DEFAULT_SLOTS = 5
#: 单只仓位权重偏离目标（1/持仓上限）多少个百分点时触发再平衡；0 表示不再平衡。
DEFAULT_REBALANCE_BAND_PCT = 5.0


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


def _lot_size(config: PortfolioBacktestConfig) -> int:
    return profile_from_config(
        "stock",
        commission=config.commission,
        min_commission=config.min_commission,
        stamp_tax=config.stamp_tax,
    ).lot_size


def rebalance_targets(state: dict[str, Any], slots: int, band: float) -> dict[str, float]:
    """按信号日收盘权重挑出偏离目标超过阈值的仓位，返回统一的调仓目标市值。

    即将执行卖出的仓位（``pending``）不参与：它当天开盘就清仓，再平衡没有意义。
    目标很小、5 个百分点超过目标一半时（持仓上限较大），下限收到目标的一半。
    """

    if band <= 0 or slots <= 0:
        return {}
    eligible = {
        symbol: position
        for symbol, position in state["positions"].items()
        if symbol not in state["pending"]
    }
    if not eligible:
        return {}
    equity = state["cash"] + sum(
        position["size"] * position["mark"] for position in state["positions"].values()
    )
    if equity <= 0:
        return {}
    target = 1 / slots
    upper = target + band
    lower = max(target - min(band, target / 2), 0.0)
    per_position = equity * target
    return {
        symbol: per_position
        for symbol, position in eligible.items()
        if not lower <= position["size"] * position["mark"] / equity <= upper
    }


def execute_orders(
    state: dict[str, Any],
    buys: list[dict[str, Any]],
    signal_day: str,
    trade_day: str,
    quote: Callable[[str], tuple[float, float] | None],
    version_config: Callable[[dict[str, Any]], PortfolioBacktestConfig | None],
    *,
    slots: int = DEFAULT_SLOTS,
    rebalance: dict[str, float] | None = None,
) -> None:
    """先卖后买；没有卖出成交的仓位不会变成买入名额。再平衡与卖出同一次开盘成交。

    这是「信号榜最理想执行」的模拟盘：按开盘价成交，不计佣金、印花税与滑点。
    """

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
        proceeds = price * size
        state["cash"] += proceeds
        state["trades"].append(
            {
                "symbol": symbol,
                "date": trade_day,
                "signal_date": order["signal_date"],
                "direction": "SELL",
                "price": price,
                "size": size,
                "reason": order["reason"],
                "pnl": proceeds - position["cost"],
                "version_id": position["tracking_version_id"],
            }
        )
        state["cooldown"][symbol] = order.get("cooldown", 0) + 1
        del positions[symbol], state["pending"][symbol]
        sold.add(symbol)

    # 再平衡减仓：与卖出同一次开盘成交；部分卖出后仓位保留，也不进冷却。
    for symbol, target in (rebalance or {}).items():
        position = positions.get(symbol)
        if position is None or symbol in state["pending"]:
            continue
        prices = quote(symbol)
        config = version_config(position)
        if prices is None or config is None:
            continue
        price = prices[0]
        size = position["size"]
        excess = size * price - target
        lot = _lot_size(config)
        trim = min(int(excess / price / lot) * lot, size) if excess > 0 else 0
        if not trim:
            continue
        proceeds = price * trim
        cost_part = position["cost"] * trim / size
        state["cash"] += proceeds
        position["size"] = size - trim
        position["cost"] -= cost_part
        state["trades"].append(
            {
                "symbol": symbol,
                "date": trade_day,
                "signal_date": signal_day,
                "direction": "SELL",
                "price": price,
                "size": trim,
                "reason": "再平衡减仓",
                "pnl": proceeds - cost_part,
                "version_id": position["tracking_version_id"],
            }
        )

    available = min(slots - len(positions), slots if initial else len(sold))
    # 每个空位最多用当前总权益的一个目标仓位的额度，未成交资金继续留在现金中。
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
        budget = min(state["cash"], open_equity / slots)
        lot = _lot_size(config)
        size = int(budget / price / lot) * lot
        if not size:
            continue
        cost = size * price
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
                "reason": "成立建仓" if initial else "卖出后补仓",
                "rank": rank,
                "version_id": row["tracking_version_id"],
            }
        )
        available -= 1

    # 再平衡补仓：新开仓先用钱，剩下的现金补低配；缺口大的先补。
    planned = [symbol for symbol in (rebalance or {}) if symbol in positions]
    for symbol in sorted(planned, key=lambda key: positions[key]["size"] * positions[key]["mark"]):
        position = positions[symbol]
        target = (rebalance or {})[symbol]
        prices = quote(symbol)
        config = version_config(position)
        if prices is None or config is None:
            continue
        price, _close = prices
        budget = min(state["cash"], target - position["size"] * price)
        if budget <= 0:
            continue
        lot = _lot_size(config)
        size = int(budget / price / lot) * lot
        if not size:
            continue
        cost = size * price
        state["cash"] -= cost
        position["size"] += size
        position["cost"] += cost
        position["highest_price"] = max(position["highest_price"], price)
        state["trades"].append(
            {
                "symbol": symbol,
                "date": trade_day,
                "signal_date": signal_day,
                "direction": "BUY",
                "price": price,
                "size": size,
                "reason": "再平衡补仓",
                "version_id": position["tracking_version_id"],
            }
        )

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
        # 持仓等权基准要反推成员、读整段收盘价；列表每 30 秒轮询一次，按净值日期缓存。
        self._holdings_cache: dict[tuple[str, str, str], tuple[float, dict[str, Any] | None]] = {}

    def initialize(self) -> None:
        with self.store.state_write_transaction() as connection:
            connection.execute(ARCHIVE_DDL)
            connection.execute(SIMULATION_DDL)

    def signal_days(self, objective: str, ranking: str = "") -> list[str]:
        """返回该目标、该榜单下可回放的信号日。

        默认顺序不依赖排序指标，旧版完整扫描的基础归档也能选；指标榜单
        只开放带对应归档版本的交易日，避免用事后指标排序历史信号。

        Args:
            objective: 策略目标，``annual_return`` 或 ``sharpe``。
            ranking: 榜单键；空字符串表示不过滤归档版本。

        Returns:
            按交易日倒序排列的可回放日期列表。
        """

        required = required_rank_version(ranking)
        with STATE_WRITE_LOCK, self.store.state_read_session() as connection:
            return [
                str(row[0])
                for row in connection.execute(
                    "SELECT trade_date FROM pool_simulation_signals WHERE objective = ? "
                    "AND COALESCE(TRY_CAST("
                    "json_extract_string(entries_json, '$.rank_version') AS INTEGER), 0) >= ? "
                    "ORDER BY trade_date DESC",
                    [objective, required],
                ).fetchall()
            ]

    def create(self, config: dict[str, Any]) -> str:
        config = {"max_positions": DEFAULT_SLOTS, **config}
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
        # 新建后立刻回放一次：不让新盘干等到下一轮 60 秒调度才有净值。
        self._advance_new(job_id)
        return job_id

    def _advance_new(self, key: str) -> None:
        """新建盘的首次回放；失败不影响创建，后台循环会继续重试。"""

        if not self.lock.acquire(blocking=False):
            return
        try:
            rows = self._read(key)
            if not rows:
                return
            _, config, state = rows[0]
            now = shanghai_now()
            completed = {
                day
                for day in self.store.read_completed_trading_dates()
                if day < now.date() or (day == now.date() and now.time() >= time(15))
            }
            days = self._replay_days(date.fromisoformat(state["minimum_signal_date"]), completed)
            if len(days) >= 2:
                self._advance_account(key, config, state, days, completed)
        except Exception:  # noqa: BLE001 - 首次回放失败交给后台循环
            logger.exception("新建模拟盘首次回放失败: %s", key)
        finally:
            self.lock.release()

    def _replay_days(self, earliest: date, completed: set[date]) -> list[date]:
        if not completed:
            return []
        dates = {day for day in completed if day >= earliest}
        last_day = max(completed)
        for lo, hi in self.store.read_authoritative_calendar_coverage():
            start, end = max(lo, earliest), min(hi, last_day)
            if start <= end:
                dates.update(self.store.read_authoritative_trading_dates(start, end))
        return sorted(dates)

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
                # 只有真正跑过的盘才把信号起点对齐到最新交易日；从没起步的盘
                # 暂停再恢复不该跳掉它的首日信号。
                if state["started"]:
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

    def rename(self, key: str, name: str) -> None:
        with self.lock:
            rows = self._read(key)
            if not rows:
                raise KeyError(key)
            _, config, _ = rows[0]
            config["name"] = name
            with self.store.state_write_transaction() as connection:
                connection.execute(
                    "UPDATE pool_simulations SET config_json = ? WHERE simulation_id = ?",
                    [dumps(config), key],
                )

    def delete(self, key: str) -> None:
        with self.lock:
            if not self._read(key):
                raise KeyError(key)
            with self.store.state_write_transaction() as connection:
                connection.execute("DELETE FROM pool_simulations WHERE simulation_id = ?", [key])

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
            initial = float(config["initial_cash"])
            total_return = value / initial - 1
            positions = list(state["positions"].values())
            benchmark = self._benchmark(state)
            holdings = self._holdings_benchmark(config, state)
            benchmark_return = series_return(benchmark)
            holdings_return = series_return(holdings)
            output = {
                **config,
                "id": ident,
                "paused": state["paused"],
                "warning": state["warning"],
                "cash": state["cash"],
                "value": value,
                "total_return": total_return,
                "max_drawdown": drawdown,
                "equity": equity,
                "holding_count": len(positions),
                "max_positions": config.get("max_positions", DEFAULT_SLOTS),
                "turnover": sum(t["size"] * t["price"] for t in state["trades"]) / initial,
                "day_return": day_return(equity, initial),
                "benchmark_return": benchmark_return,
                "holdings_return": holdings_return,
                "excess_benchmark": total_return - benchmark_return
                if benchmark_return is not None
                else None,
                "excess_holdings": total_return - holdings_return
                if holdings_return is not None
                else None,
            }
            if key is not None:
                output.update(
                    positions=positions,
                    trades_total=len(state["trades"]),
                    trades=list(reversed(state["trades"]))[offset : offset + limit],
                    all_trades=list(state["trades"]),
                    benchmark=benchmark,
                    holdings_benchmark=holdings,
                    next_actions=self._next_actions_safe(config, state),
                    metrics=detail_metrics(
                        equity=equity,
                        initial_cash=initial,
                        cash=float(state["cash"]),
                        trades=state["trades"],
                        positions=positions,
                        benchmark=benchmark,
                        holdings=holdings,
                        start_date=str(config.get("start_date") or "") or None,
                    ),
                )
            items.append(output)
        return items

    def _benchmark(self, state: dict[str, Any]) -> dict[str, Any] | None:
        """从成立日到最新净值日的 510300 前复权收盘；没同步过基准时返回 None。

        期初取首个净值日之前最近一根收盘：模拟盘首日是开盘买入，基准按前收
        持有，首个交易日的收益才能和策略放在同一窗口里比。
        """

        equity = state["equity"]
        if not equity:
            return None
        first = date.fromisoformat(equity[0]["date"])
        rows = self.store.read_benchmark_bars(
            BENCHMARK_SYMBOL,
            start_date=BENCHMARK_START_DATE,
            end_date=date.fromisoformat(equity[-1]["date"]),
        )
        if not rows:
            return None
        index = next((i for i, (day, _) in enumerate(rows) if day >= first), len(rows))
        if index >= len(rows):
            return None
        if index > 0 and (first - rows[index - 1][0]).days <= 10:
            index -= 1
        rows = rows[index:]
        return {
            "label": BENCHMARK_LABEL,
            "dates": [day.isoformat() for day, _ in rows],
            "totals": [close for _, close in rows],
        }

    def _holdings_benchmark(
        self, config: dict[str, Any], state: dict[str, Any]
    ) -> dict[str, object] | None:
        """同目标的「持仓中」全集等权线；只做参考，算不出来不挡详情。"""

        equity = state["equity"]
        if not equity:
            return None
        key = (str(config["objective"]), str(equity[0]["date"]), str(equity[-1]["date"]))
        now = monotonic()
        cached = self._holdings_cache.get(key)
        if cached is not None and now - cached[0] < 300:
            return cached[1]
        try:
            value = holdings_benchmark(
                self.store,
                objective=str(config["objective"]),
                equity_dates=[point["date"] for point in equity],
                start_date=str(config.get("start_date") or "") or None,
            )
        except Exception:  # noqa: BLE001 - 参考线失败不影响持仓与成交
            logger.exception("持仓等权基准计算失败")
            value = None
        if len(self._holdings_cache) >= 32:
            self._holdings_cache.clear()
        self._holdings_cache[key] = (now, value)
        return value

    def _next_actions_safe(
        self, config: dict[str, Any], state: dict[str, Any]
    ) -> dict[str, object] | None:
        try:
            return self._next_actions(config, state)
        except Exception:  # noqa: BLE001 - 操作预览失败不影响持仓与成交
            logger.exception("下个交易日操作预览失败")
            return None

    def _next_actions(
        self, config: dict[str, Any], state: dict[str, Any]
    ) -> dict[str, object] | None:
        """基于下一份还没消化的收盘信号，列出下一交易日开盘会执行的操作。

        与 ``_step`` 用同一套离场判定和榜单排序；只做预览，不改任何状态。
        """

        with STATE_WRITE_LOCK, self.store.state_read_session() as connection:
            row = connection.execute(
                "SELECT trade_date, entries_json FROM pool_simulation_signals "
                "WHERE objective = ? AND trade_date >= ? AND trade_date > ? "
                "ORDER BY trade_date ASC LIMIT 1",
                [
                    config["objective"],
                    state["minimum_signal_date"],
                    state.get("cursor") or "1900-01-01",
                ],
            ).fetchone()
        if row is None:
            return None
        signal_day = row[0] if isinstance(row[0], date) else date.fromisoformat(str(row[0])[:10])
        try:
            data = json.loads(row[1])
        except (TypeError, ValueError):
            return None
        if int(data.get("rank_version") or 0) < required_rank_version(config["ranking"]):
            return {
                "signal_date": signal_day.isoformat(),
                "actions": [],
                "warning": "该日的信号归档缺少当前排序所需的指标，暂时无法推进",
            }
        slots = int(config.get("max_positions", DEFAULT_SLOTS))
        self.engine._refresh_corporate_action_cache()
        actions: list[dict[str, object]] = []
        remaining: dict[str, dict[str, Any]] = {}
        sold: set[str] = set()
        for symbol, position in state["positions"].items():
            reason = self._plan_exit(position, signal_day)
            if reason is None:
                remaining[symbol] = position
                continue
            actions.append(
                {
                    "direction": "SELL",
                    "symbol": symbol,
                    "name": position.get("name"),
                    "reason": reason,
                    "kind": "exit",
                }
            )
            sold.add(symbol)
        available = min(slots - len(remaining), slots if not state["started"] else len(sold))
        held = set(remaining)
        for rank, candidate in enumerate(rank_buys(data.get("buys") or [], config), start=1):
            if available <= 0:
                break
            symbol = _symbol(candidate)
            if symbol in held or symbol in sold or state["cooldown"].get(symbol, 0):
                continue
            actions.append(
                {
                    "direction": "BUY",
                    "symbol": symbol,
                    "name": candidate.get("name"),
                    "reason": f"买入榜第 {rank} 名",
                    "kind": "entry",
                }
            )
            available -= 1
        # 再平衡也用信号日收盘价重算权重，和执行时的判定同一口径：已排队卖出的仓位
        # 仍留在总权益里，只是不参与再平衡（直接把它的市值从分母拿掉会误判其余超配）。
        plan_positions = {symbol: dict(position) for symbol, position in state["positions"].items()}
        for symbol, position in plan_positions.items():
            bars = self.store.read_bars(
                position["market"], position["code"], start_date=signal_day, end_date=signal_day
            )
            if not bars.empty:
                position["mark"] = float(bars.iloc[0]["close"])
        targets = rebalance_targets(
            {
                "positions": plan_positions,
                "pending": dict.fromkeys(sold, {}),
                "cash": state["cash"],
            },
            slots,
            float(config.get("rebalance_band_pct", DEFAULT_REBALANCE_BAND_PCT)) / 100,
        )
        for symbol, target in targets.items():
            position = plan_positions[symbol]
            overweight = position["size"] * position["mark"] > target
            actions.append(
                {
                    "direction": "SELL" if overweight else "BUY",
                    "symbol": symbol,
                    "name": position.get("name"),
                    "reason": "再平衡减仓" if overweight else "再平衡补仓",
                    "kind": "rebalance",
                }
            )
        return {
            "signal_date": signal_day.isoformat(),
            "actions": actions,
            "warning": "模拟盘已暂停，恢复后才会执行" if state["paused"] else None,
        }

    def _plan_exit(self, row: dict[str, Any], signal_day: date) -> str | None:
        pinned = self._version(row, signal_day)
        if pinned is None:
            return None
        ref = self.adapter.stock_ref("", cast(MarketCode, row["market"]), row["code"])
        raw = self.store.read_bars(row["market"], row["code"], end_date=signal_day)
        if raw.empty:
            return None
        context = self.engine._build_context(
            ref, replace(pinned, end_date=signal_day), None, frame=raw
        )
        if context is None:
            return None
        idx = context.date_to_index.get(int(signal_day.strftime("%Y%m%d")))
        if idx is None:
            return None
        position = _Position(
            market=row["market"],
            code=row["code"],
            instrument_type="stock",
            board=context.board,
            size=row["size"],
            entry_price=row["entry_price"],
            entry_date=int(str(row["entry_date"]).replace("-", "")),
            highest_price=row["highest_price"],
            buy_commission=0,
            buy_slippage=0,
            applied_corporate_actions=set(row["applied_corporate_actions"]),
        )
        trigger = (
            None
            if str(row.get("entry_date")) == str(signal_day)
            else _risk_trigger(context, idx, position, pinned)
        )
        if trigger:
            return _risk_trigger_label(trigger[0])
        if bool(context.sell_mask[idx]):
            return "卖出条件"
        if pinned.max_hold_days and self.engine._held_enough_bars(
            context, position, idx, pinned.max_hold_days
        ):
            return "持有到期"
        return None

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
                # 与跟踪回放引擎一致：当天开盘买入的仓位当天不做风控判定（引擎先判
                # 风控、再执行买入）；规则卖出与持有到期照常按收盘判定。
                trigger = (
                    None
                    if str(row["entry_date"]) == str(signal_day)
                    else _risk_trigger(context, idx, position, pinned)
                )
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
            # 暂停恢复时 mark 可能停在暂停前；用信号日最后一根已完成收盘校正权重口径。
            row["mark"] = float(raw.iloc[-1]["close"])
            row.update(
                size=position.size,
                highest_price=position.highest_price,
                entry_price=position.entry_price,
            )
            row["applied_corporate_actions"] = sorted(position.applied_corporate_actions)
        slots = config.get("max_positions", DEFAULT_SLOTS)
        execute_orders(
            state,
            rank_buys(rows, config),
            str(signal_day),
            str(trade_day),
            quote,
            version,
            slots=slots,
            rebalance=rebalance_targets(
                state,
                slots,
                float(config.get("rebalance_band_pct", DEFAULT_REBALANCE_BAND_PCT)) / 100,
            ),
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
                days = self._replay_days(earliest, completed)
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
            data = json.loads(entries)
            opening = datetime.combine(trade_day, time(9, 30), ZoneInfo("Asia/Shanghai"))
            # 事后按当日行情回放出来的归档（replayed）没有「次日开盘前生成」的自然时间，
            # 但它只用了当日及之前的数据，口径上等价于当日收盘信号。
            if (
                not data.get("replayed")
                and saved_at.replace(tzinfo=ZoneInfo("Asia/Shanghai")) >= opening
            ):
                state["warning"] = f"{signal_day} 的信号在 {trade_day} 开盘后生成，已跳过该批次"
                state["cursor"] = str(signal_day)
                self._save(key, state)
                continue
            if int(data.get("rank_version") or 0) < required_rank_version(config["ranking"]):
                state["warning"] = f"{signal_day} 的归档缺少该排序所需指标，无法推进"
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
