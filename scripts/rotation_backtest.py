"""按「指标排名 + 逐股策略信号」回放轮换组合，风控与到期退出同产品口径。

这不是产品功能，而是一次性的分析工具：用策略池里当前生效的策略与指标，回答
「按胜率 / 盈亏比 / 年化取前 N、卖出后买入排名靠前标的」这套做法历史上是什么表现。

回放直接交给产品自己的组合引擎（``PortfolioBacktestService``，与策略池的
「组合留出段回测」、逐股跟踪回放同一条路径）：每条策略带着自己的买入规则、
卖出规则和风控参数（止损 / 止盈 / 移动止盈 / ATR 周期 / 冷却 / 最长持有），
所以卖出信号、风控退出与持有到期都按产品口径发生。上一版脚本只认卖出规则、
不模拟风控与最长持有，换手与收益都不是真实策略的结果，已由本版取代。

口径与近似（必须连同结果一起读）：

- **排名**取今天的全区间指标（研究口径，日更后会刷新），历史上也沿用这份顺序——
  选样是事后指标，只有前向执行的机制是真实的。
- **建仓 / 补位**都要求当天买入信号成立；卖出按策略自己的卖出规则、三档风控与
  最长持有，信号日收盘判定、下一交易日开盘成交（引擎口径）。
- 风控卖出与规则卖出共用出口；风控冷却期内该标的不会被补位选中。
- 组合等权、固定 N 个仓位；卖出与补位在同一个开盘完成。
- 成交价与 ATR 用不复权价，除权除息由引擎调整持仓与现金；基准用分红再投资
  收盘价（不计费用）。上一版脚本不算公司行动，送转会被当成真实暴跌。
- **风控与最长持有会强制换手**：在事后排名下，每次空槽都换成当前排名头部，
  收益被系统性放大。绝对收益不能当预期，只看同一口径下的相对差异。

用法（在容器里跑，行情库与指标缓存都在那里）：

    docker cp scripts/rotation_backtest.py aquest-backend-1:/tmp/
    docker exec -e PYTHONPATH=/app -w /app aquest-backend-1 \
        python /tmp/rotation_backtest.py --metric win_rate --top-n 5 --start 2020-01-01
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, cast

import pandas as pd

from aquest.indicators.cache import IndicatorCache
from aquest.market_data.adapter import DuckDbMarketDataAdapter
from aquest.market_data.adjustment import mark_adjusted
from aquest.market_data.models import MarketCode
from aquest.market_data.store import DuckDbMarketDataStore
from aquest.portfolio_backtest.risk import _SYMBOL_RISK_FIELDS
from aquest.portfolio_backtest.service import PortfolioBacktestService
from aquest.strategy_pool.service import _active_candidate, _parse_rule, _pool_portfolio_config
from aquest.strategy_pool.store import DuckDbStrategyPoolStore
from aquest.strategy_pool.tracking_store import PoolTrackingStore

MIN_TRADES = 20


@dataclass(frozen=True)
class Candidate:
    market: str
    code: str
    symbol: str
    metric: float
    closed_trades: int
    #: 直接喂给组合引擎的逐股策略：买卖规则 + 该候选自己的风控参数。
    strategy: Mapping[str, object]


def _summaries(store: DuckDbMarketDataStore) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for version in PoolTrackingStore(store).current_versions(with_candidate=False):
        if version.get("source") != "automatic":
            continue
        summary = version.get("summary") or {}
        result[(str(version["market"]), str(version["code"]))] = summary
    return result


def _strategy_from_candidate(candidate: Mapping[str, object]) -> dict[str, object] | None:
    """把候选落成组合引擎认的逐股策略；规则不全或解析失败就跳过这只。"""

    try:
        buy_rule = _parse_rule(candidate.get("buy_rule"))
        sell_rule = _parse_rule(candidate.get("sell_rule"))
    except ValueError:
        return None
    if buy_rule is None or sell_rule is None:
        return None
    raw_config = candidate.get("config")
    config = raw_config if isinstance(raw_config, Mapping) else {}
    strategy: dict[str, object] = {
        "buy_rule": buy_rule,
        "sell_rule": sell_rule,
        "candidate_id": candidate.get("candidate_id"),
    }
    # 风控参数是每条策略自己的：止损 / 止盈 / 移动止盈 / ATR 周期 / 冷却 / 最长持有。
    # 缺字段就沿用引擎默认（即不启用），与逐股跟踪的取值方式一致。
    for key in _SYMBOL_RISK_FIELDS:
        if key in config:
            strategy[key] = config[key]
    return strategy


def _candidates(store: DuckDbMarketDataStore, *, objective: str, metric: str) -> list[Candidate]:
    summaries = _summaries(store)
    pool = DuckDbStrategyPoolStore(store)
    found: list[Candidate] = []
    for entry in pool.list_entries():
        candidate = _active_candidate(entry, objective)
        if candidate is None or candidate.get("replayable") is False:
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
        strategy = _strategy_from_candidate(candidate)
        if strategy is None:
            continue
        found.append(
            Candidate(
                market=str(entry["market"]),
                code=str(entry["code"]),
                symbol=f"{entry['market']}{entry['code']}",
                metric=float(value),
                closed_trades=int(trades or 0),
                strategy=strategy,
            )
        )
    found.sort(key=lambda item: (-item.metric, item.symbol))
    return found


def _risk_control_usage(candidates: list[Candidate]) -> dict[str, int]:
    """这一轮实际带进回放的风控参数覆盖了多少条策略。"""

    usage = {"stop_loss": 0, "take_profit": 0, "trailing_stop": 0, "atr": 0, "max_hold": 0}
    for candidate in candidates:
        strategy = candidate.strategy
        if (
            strategy.get("stop_loss_pct") is not None
            or strategy.get("stop_loss_atr_multiplier") is not None
        ):
            usage["stop_loss"] += 1
        if (
            strategy.get("take_profit_pct") is not None
            or strategy.get("take_profit_atr_multiplier") is not None
        ):
            usage["take_profit"] += 1
        if (
            strategy.get("trailing_stop_pct") is not None
            or strategy.get("trailing_stop_atr_multiplier") is not None
        ):
            usage["trailing_stop"] += 1
        if (
            strategy.get("atr_period") is not None
            or strategy.get("stop_loss_atr_multiplier") is not None
        ):
            usage["atr"] += 1
        if strategy.get("max_hold_days") is not None:
            usage["max_hold"] += 1
    return usage


def _checkpoint(
    adapter: DuckDbMarketDataAdapter, trades: list[Mapping[str, Any]]
) -> dict[str, Any]:
    """期初等权买入持有基准：与上一版脚本同口径，便于对照。"""

    buys = [
        trade
        for trade in trades
        if str(trade.get("direction")) == "BUY" and not trade.get("rejected")
    ]
    if not buys:
        return {}
    first = min(str(trade["date"]) for trade in buys)
    symbols = sorted(
        {
            (str(trade["market"]), str(trade["code"]))
            for trade in buys
            if str(trade["date"]) == first
        }
    )
    if not symbols:
        return {}
    started = time.monotonic()
    returns: list[float] = []
    for market, code in symbols:
        try:
            ref = adapter.stock_ref("", cast(MarketCode, market), code)
            actions = adapter.read_corporate_actions(ref)
            raw = adapter.read_stock(ref)
            frame = mark_adjusted(raw, tuple(actions))
        except Exception:  # noqa: BLE001 - 分析脚本：读不到就不进基准
            continue
        dates = pd.to_datetime(frame["date"])
        closes = pd.to_numeric(frame["close"], errors="coerce")
        window = closes[(dates >= pd.Timestamp(first)) & closes.notna()]
        if window.empty or float(window.iloc[0]) <= 0:
            continue
        returns.append(float(window.iloc[-1]) / float(window.iloc[0]) - 1.0)
    if not returns:
        return {}
    return {
        "initial_buy_date": first,
        "initial_symbols": [f"{market}{code}" for market, code in symbols],
        "initial_buy_hold_return": round(sum(returns) / len(returns), 4),
        "initial_read_seconds": round(time.monotonic() - started, 1),
    }


def _exit_breakdown(trades: tuple[Mapping[str, Any], ...]) -> dict[str, Any]:
    reasons: Counter[str] = Counter()
    triggers: Counter[str] = Counter()
    for trade in trades:
        if str(trade.get("direction")) != "SELL" or trade.get("rejected"):
            continue
        reasons[str(trade.get("reason") or "未知")] += 1
        trigger = trade.get("risk_trigger")
        if trigger:
            triggers[str(trigger)] += 1
    total = sum(reasons.values())
    risk_or_expiry = sum(triggers.values()) + reasons.get("持有到期", 0)
    return {
        "sell_reasons": dict(reasons.most_common()),
        "risk_triggers": dict(triggers.most_common()),
        # 旧版口径漏掉的就是这一块：风控 + 持有到期占全部平仓的比例。
        "risk_or_expiry_exits": risk_or_expiry,
        "risk_or_expiry_share": round(risk_or_expiry / total, 4) if total else 0.0,
    }


def simulate(
    *,
    store: DuckDbMarketDataStore,
    ranked: list[Candidate],
    objective: str,
    metric: str,
    top_n: int,
    start: date,
    max_rank: int = 0,
    initial_cash: float = 1_000_000.0,
) -> dict[str, object]:
    latest = max(store.read_completed_trading_dates())
    universe = ranked if max_rank <= 0 else ranked[:max_rank]
    if not universe:
        raise SystemExit("没有可用策略")
    strategies = {item.symbol: dict(item.strategy) for item in universe}
    # 优先级就是当前指标的排名：引擎每天只从有买入信号的标的里按它从高到低补位，
    # 与「从排名头部往下找第一个有信号的」等价。
    priorities = {item.symbol: float(len(universe) - index) for index, item in enumerate(universe)}

    adapter = DuckDbMarketDataAdapter(store, cache_stock_reads=True)
    cache_store = store if store.indicator_cache_backend == "sqlite" else None
    service = PortfolioBacktestService(
        adapter=adapter, indicator_cache=IndicatorCache(cache_store, max_memory_entries=64)
    )
    refs = [
        ref
        for ref in adapter.list_stock_refs("", "all", instrument_types=("stock",))
        if f"{ref.market}{ref.code}" in strategies
    ]
    if not refs:
        raise SystemExit("行情库里没有匹配的标的")
    config = _pool_portfolio_config(
        refs,
        strategies,
        priorities,
        start_date=start,
        end_date=latest,
        max_positions=top_n,
        initial_cash=initial_cash,
    )

    started = time.monotonic()
    last_report = [0.0]

    def progress(current: int, total: int) -> None:
        now = time.monotonic()
        if current == total or now - last_report[0] >= 30:
            last_report[0] = now
            print(
                f"[{now - started:6.1f}s] 准备行情 {current}/{total}",
                flush=True,
            )

    report = service.run(config, progress)
    elapsed = time.monotonic() - started
    performance = dict(report.performance)
    benchmark = report.benchmark or {}
    benchmark_performance = benchmark.get("performance") if isinstance(benchmark, Mapping) else None
    trades = tuple(report.trades)
    checkpoint = _checkpoint(adapter, list(trades))
    open_positions = []
    state = report.current_state or {}
    holdings = state.get("holdings") if isinstance(state, Mapping) else None
    for row in holdings if isinstance(holdings, (list, tuple)) else []:
        if isinstance(row, Mapping):
            open_positions.append(
                {
                    "symbol": f"{row.get('market')}{row.get('code')}",
                    "entry_date": row.get("entry_date"),
                    "entry_price": row.get("entry_price"),
                    "close": row.get("close"),
                }
            )
    closed = sum(
        1 for trade in trades if str(trade.get("direction")) == "SELL" and not trade.get("rejected")
    )
    return {
        "objective": objective,
        "metric": metric,
        "top_n": top_n,
        "max_rank": max_rank,
        "universe_ranked": len(ranked),
        "universe_used": len(universe),
        "risk_controls_in_play": _risk_control_usage(universe),
        "start": report.start_date,
        "end": report.end_date,
        "elapsed_seconds": round(elapsed, 1),
        "total_return": performance.get("total_return"),
        "annual_return": performance.get("annual_return"),
        "max_drawdown": performance.get("max_drawdown"),
        "sharpe": performance.get("sharpe"),
        "win_rate": performance.get("win_rate"),
        "payoff_ratio": performance.get("payoff_ratio"),
        "profit_factor": performance.get("profit_factor"),
        "closed_trades": closed,
        "avg_holding_days": performance.get("avg_holding_days"),
        "turnover_x": round(float(performance.get("turnover") or 0.0), 2),
        "benchmark_pool_return": (
            benchmark_performance.get("total_return")
            if isinstance(benchmark_performance, Mapping)
            else None
        ),
        **checkpoint,
        **_exit_breakdown(trades),
        "open_positions": open_positions,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metric", default="annual_return")
    parser.add_argument("--objective", default="annual_return")
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--start", default="2020-01-01")
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
        ),
        flush=True,
    )
    report = simulate(
        store=store,
        ranked=eligible,
        objective=args.objective,
        metric=args.metric,
        top_n=args.top_n,
        start=date.fromisoformat(args.start),
        max_rank=args.max_rank,
    )
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
