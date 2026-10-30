"""按「指标排名前 N + 卖出当日收盘判定、次日开盘补位」模拟轮换组合。

这不是产品功能，而是一次性的分析工具：用策略池里当前生效的策略与指标，回答
「按胜率 / 盈亏比 / 年化取前 N、卖出后买入排名靠前标的」这套做法历史上是什么表现。

口径与近似（必须连同结果一起读）：

- **排名**取今天的全区间指标（研究口径，日更后会刷新），历史上也是这份顺序——
  也就是说选样用的是「事后」指标，只有前向执行的机制是真实的。
- **卖出**只用策略自己的卖出规则（与入场无关的规则事实）；风控止损 / 移动止盈 /
  最长持有依赖入场价与持仓天数，本脚本**不模拟**，所以持仓可能比真实策略拿得久。
- **成交**沿用引擎假设：信号日收盘判定、下一交易日开盘价成交；买卖各计佣金
  (万分之一、最低 5 元)，卖出加千分之一印花税。
- 组合等权、固定 N 个仓位；被选中的标的当天缺价（停牌）就顺延到下一个有价日。

用法（在容器里跑，行情库与指标缓存都在那里）：

    docker cp scripts/rotation_backtest.py aquest-backend-1:/tmp/
    docker exec aquest-backend-1 python /tmp/rotation_backtest.py \
        --metric annual_return --top-n 5 --start 2024-01-01
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd

from aquest.backtest.service import BacktestService
from aquest.formulas.registry import FORMULA_REGISTRY
from aquest.market_data.adapter import DuckDbMarketDataAdapter
from aquest.market_data.store import DuckDbMarketDataStore
from aquest.screening.engine import evaluate_rule_group
from aquest.strategy_pool.service import _active_candidate, _feature_ids, _parse_rule, _union_rule
from aquest.strategy_pool.store import DuckDbStrategyPoolStore
from aquest.strategy_pool.tracking_store import PoolTrackingStore

COMMISSION_RATE = 0.0001
MIN_COMMISSION = 5.0
STAMP_TAX_RATE = 0.001
MIN_TRADES = 20


@dataclass(frozen=True)
class Candidate:
    market: str
    code: str
    symbol: str
    metric: float
    closed_trades: int


def _summaries(store: DuckDbMarketDataStore) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for version in PoolTrackingStore(store).current_versions(with_candidate=False):
        if version.get("source") != "automatic":
            continue
        summary = version.get("summary") or {}
        result[(str(version["market"]), str(version["code"]))] = summary
    return result


def _candidates(store: DuckDbMarketDataStore, *, objective: str, metric: str) -> list[Candidate]:
    summaries = _summaries(store)
    pool = DuckDbStrategyPoolStore(store)
    found: list[Candidate] = []
    for entry in pool.list_entries():
        candidate = _active_candidate(entry, objective)
        if candidate is None:
            continue
        summary = summaries.get((str(entry["market"]), str(entry["code"]))) or {}
        full = summary.get("full_period")
        if not isinstance(full, Mapping):
            metrics = candidate.get("metrics")
            full = metrics.get("full_period") if isinstance(metrics, Mapping) else None
        if not isinstance(full, Mapping):
            continue
        value = full.get(metric)
        trades = full.get("closed_trades")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        found.append(
            Candidate(
                market=str(entry["market"]),
                code=str(entry["code"]),
                symbol=f"{entry['market']}{entry['code']}",
                metric=float(value),
                closed_trades=int(trades or 0),
            )
        )
    found.sort(key=lambda item: (-item.metric, item.symbol))
    return found


Feed = tuple[dict[date, float], dict[date, bool], dict[date, bool], dict[date, float]]


class SymbolFeed:
    """一只标的的日线开盘价与卖出规则掩码（按需准备，带缓存）。"""

    def __init__(self, service: BacktestService, store: DuckDbMarketDataStore) -> None:
        self._service = service
        self._store = store
        self._cache: dict[tuple[str, str], Feed] = {}

    def load(self, market: str, code: str) -> Feed:
        key = (market, code)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        pool = DuckDbStrategyPoolStore(self._store)
        entry = pool.get(market, code)
        candidate = _active_candidate(entry, "annual_return")
        if candidate is None:
            raise ValueError(f"{market}{code} 没有生效策略")
        buy_rule = _parse_rule(candidate.get("buy_rule"))
        sell_rule = _parse_rule(candidate.get("sell_rule"))
        if buy_rule is None or sell_rule is None:
            raise ValueError(f"{market}{code} 策略规则不完整")
        ref = self._service._adapter.stock_ref("", market, code)  # noqa: SLF001 - 分析脚本
        from aquest.backtest.models import BacktestConfig

        config = BacktestConfig(
            market=ref.market,
            code=ref.code,
            vipdoc_path="",
            buy_rule=_union_rule(buy_rule, sell_rule),
            sell_rule=_union_rule(buy_rule, sell_rule),
            always_in_market=False,
            close_at_end=False,
            instrument_type="stock",
            board=ref.board,
        )
        prepared = self._service.prepare_for_ref(
            config, ref, feature_ids=_feature_ids(buy_rule, sell_rule)
        )
        frame = prepared.frame
        dates = pd.to_datetime(frame["date"]).dt.date
        sell_mask = evaluate_rule_group(sell_rule, prepared.formula_results, FORMULA_REGISTRY)
        buy_mask = evaluate_rule_group(buy_rule, prepared.formula_results, FORMULA_REGISTRY)
        opens = {dates.iloc[index]: float(frame["open"].iloc[index]) for index in range(len(frame))}
        closes = {
            dates.iloc[index]: float(frame["close"].iloc[index]) for index in range(len(frame))
        }
        sells = {dates.iloc[index]: bool(sell_mask[index]) for index in range(len(frame))}
        buys = {dates.iloc[index]: bool(buy_mask[index]) for index in range(len(frame))}
        self._cache[key] = (opens, sells, buys, closes)
        return self._cache[key]


def simulate(
    *,
    store: DuckDbMarketDataStore,
    ranked: list[Candidate],
    metric: str,
    top_n: int,
    start: date,
    max_rank: int = 50,
    initial_cash: float = 1_000_000.0,
) -> dict[str, object]:
    adapter = DuckDbMarketDataAdapter(store, cache_stock_reads=True)
    service = BacktestService(adapter=adapter)
    feeds = SymbolFeed(service, store)
    calendar = [item for item in store.read_completed_trading_dates() if item >= start]
    if not calendar:
        raise SystemExit("交易日历为空")

    cash = initial_cash
    positions: dict[str, dict[str, float]] = {}
    pending_buys: list[str] = []
    pending_sells: list[str] = []
    equity_curve: list[tuple[date, float]] = []
    round_trips: list[float] = []
    traded = 0.0
    last_close: dict[str, float] = {}

    def try_prepare(symbol: str) -> bool:
        market, code = symbol[:2], symbol[2:]
        try:
            feeds.load(market, code)
            return True
        except Exception:  # noqa: BLE001 - 分析脚本：准备失败就跳过这只
            return False

    def buy(symbol: str, day: date) -> None:
        nonlocal cash, traded
        market, code = symbol[:2], symbol[2:]
        opens, _sells, _buys, _closes = feeds.load(market, code)
        price = opens.get(day)
        if not price:
            pending_buys.append(symbol)
            return
        budget = min(cash, equity_value(day) / max(top_n, 1))
        shares = math.floor(budget / price / 100) * 100
        if shares <= 0:
            return
        cost = shares * price
        fee = max(cost * COMMISSION_RATE, MIN_COMMISSION)
        if cost + fee > cash:
            shares -= 100
            if shares <= 0:
                return
            cost = shares * price
            fee = max(cost * COMMISSION_RATE, MIN_COMMISSION)
        cash -= cost + fee
        traded += cost
        positions[symbol] = {"shares": float(shares), "cost": cost, "entry": price}

    def sell(symbol: str, day: date) -> None:
        nonlocal cash, traded
        position = positions.get(symbol)
        if position is None:
            return
        market, code = symbol[:2], symbol[2:]
        opens, _sells, _buys, _closes = feeds.load(market, code)
        price = opens.get(day)
        if not price:
            pending_sells.append(symbol)
            return
        proceeds = position["shares"] * price
        fee = max(proceeds * COMMISSION_RATE, MIN_COMMISSION) + proceeds * STAMP_TAX_RATE
        cash += proceeds - fee
        traded += proceeds
        round_trips.append(proceeds / position["cost"] - 1.0)
        positions.pop(symbol, None)

    def equity_value(day: date) -> float:
        total = cash
        for symbol, position in positions.items():
            market, code = symbol[:2], symbol[2:]
            try:
                _opens, _sells, _buys, closes = feeds.load(market, code)
            except Exception:  # noqa: BLE001
                total += position["shares"] * position["entry"]
                continue
            price = closes.get(day) or last_close.get(symbol) or position["entry"]
            total += position["shares"] * price
        return total

    prepared_set: set[str] = set()
    universe = ranked if max_rank <= 0 else ranked[:max_rank]

    def buy_signal_candidates(day: date, limit: int) -> list[str]:
        """当天买入信号成立、且没有持仓的标的，按排名从前往后取 ``limit`` 只。"""

        picked: list[str] = []
        for candidate in universe:
            symbol = candidate.symbol
            if symbol in positions or symbol in pending_buys or symbol in picked:
                continue
            market, code = symbol[:2], symbol[2:]
            try:
                _opens, _sells, buys, _closes = feeds.load(market, code)
            except Exception:  # noqa: BLE001
                continue
            if buys.get(day):
                picked.append(symbol)
                if len(picked) >= limit:
                    break
        return picked

    # 建仓：开仓日当天触发买入信号的标的里，按排名取前 N 只。
    for symbol in buy_signal_candidates(calendar[0], top_n):
        prepared_set.add(symbol)
        buy(symbol, calendar[0])
    initial_symbols = list(positions)

    for day in calendar:
        for symbol in list(pending_sells):
            pending_sells.remove(symbol)
            sell(symbol, day)
        for symbol in list(pending_buys):
            pending_buys.remove(symbol)
            buy(symbol, day)
        # 收盘判定：持有个股的卖出规则触发 → 次日开盘卖出。
        for symbol in list(positions):
            market, code = symbol[:2], symbol[2:]
            try:
                _opens, sells, _buys, closes = feeds.load(market, code)
            except Exception:  # noqa: BLE001
                continue
            if closes.get(day):
                last_close[symbol] = closes[day]
            if sells.get(day):
                pending_sells.append(symbol)
        # 空出的仓位补位（次日开盘买入）：当天从排名头部往下找第一个有买入信号、
        # 且未持仓的标的；一只都没有就让仓位空着，下一天再看。
        while len(positions) + len(pending_buys) < top_n:
            filled = buy_signal_candidates(day, 1)
            if not filled:
                break
            prepared_set.add(filled[0])
            pending_buys.append(filled[0])
        equity_curve.append((day, equity_value(day)))

    values = pd.Series({day: value for day, value in equity_curve})
    returns = values.pct_change().dropna()
    total_return = float(values.iloc[-1] / initial_cash - 1.0)
    days = max((calendar[-1] - calendar[0]).days, 1)
    annual = float((1 + total_return) ** (365.0 / days) - 1.0) if total_return > -1 else -1.0
    peak = values.cummax()
    drawdown = float(((values - peak) / peak).min())
    sharpe = 0.0
    if len(returns) > 1 and float(returns.std()):
        sharpe = float(returns.mean() / returns.std() * math.sqrt(252))
    wins = [item for item in round_trips if item > 0]
    losses = [item for item in round_trips if item < 0]
    win_rate = len(wins) / len(round_trips) if round_trips else 0.0
    payoff = (
        (sum(wins) / len(wins)) / abs(sum(losses) / len(losses))
        if wins and losses and losses
        else None
    )
    bench = equal_weight_benchmark(feeds, initial_symbols, calendar, initial_cash)
    return {
        "metric": metric,
        "top_n": top_n,
        "max_rank": max_rank,
        "start": calendar[0].isoformat(),
        "end": calendar[-1].isoformat(),
        "total_return": round(total_return, 4),
        "annual_return": round(annual, 4),
        "max_drawdown": round(drawdown, 4),
        "sharpe": round(sharpe, 3),
        "round_trips": len(round_trips),
        "win_rate": round(win_rate, 4),
        "payoff_ratio": round(payoff, 3) if payoff is not None else None,
        "symbols_used": len(prepared_set),
        "turnover_x": round(traded / initial_cash, 2),
        "benchmark_return": round(bench, 4) if bench is not None else None,
        "open_positions": sorted(positions),
    }


def equal_weight_benchmark(
    feeds: SymbolFeed,
    symbols: list[str],
    calendar: list[date],
    initial_cash: float,
) -> float | None:
    """期初等权买入持有（同一批初始标的）作对照。"""

    if not symbols:
        return None
    per = initial_cash / len(symbols)
    total = 0.0
    for symbol in symbols:
        market, code = symbol[:2], symbol[2:]
        try:
            opens, _sells, _buys, closes = feeds.load(market, code)
        except Exception:  # noqa: BLE001
            continue
        entry = opens.get(calendar[0]) or next(
            (opens[day] for day in calendar if opens.get(day)), None
        )
        exit_price = closes.get(calendar[-1]) or next(
            (closes[day] for day in reversed(calendar) if closes.get(day)), None
        )
        if entry and exit_price:
            total += per * exit_price / entry
        else:
            total += per
    return total / initial_cash - 1.0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metric", default="annual_return")
    parser.add_argument("--objective", default="annual_return")
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--min-trades", type=int, default=MIN_TRADES)
    parser.add_argument("--max-rank", type=int, default=0)
    parser.add_argument("--database", default="/data/market/market.duckdb")
    args = parser.parse_args()

    store = DuckDbMarketDataStore(args.database, read_only=True)
    ranked = _candidates(store, objective=args.objective, metric=args.metric)
    eligible = [item for item in ranked if item.closed_trades >= args.min_trades]
    print(
        json.dumps(
            {
                "universe": len(ranked),
                "eligible_min_trades": len(eligible),
                "top10": [
                    (item.symbol, round(item.metric, 3), item.closed_trades)
                    for item in eligible[:10]
                ],
            },
            ensure_ascii=False,
        )
    )
    report = simulate(
        store=store,
        ranked=eligible,
        metric=args.metric,
        top_n=args.top_n,
        start=date.fromisoformat(args.start),
        max_rank=args.max_rank,
    )
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
