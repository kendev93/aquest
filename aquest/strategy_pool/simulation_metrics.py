"""模拟盘的展示指标：列表的同期对照、详情的交易 / 风险 / 区间 / 阶段收益。

纯函数，输入读接口已经拿到的净值、基准序列、成交与持仓；不读数据库，
避免同一口径在展示层再分叉。基准序列允许带一个早于首个净值日的期初点，
区间与阶段收益都从同一窗口起点取基准，保证「同期」是同一个窗口。
"""

from __future__ import annotations

import calendar
import math
import statistics
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

#: 年化指标至少需要的净值点数；样本更短时只给回撤类，不拿噪声冒充年化。
RISK_SAMPLE_MIN_DAYS = 20
TRADING_DAYS_PER_YEAR = 250

RANGE_SPECS: tuple[tuple[str, str, int | None], ...] = (
    ("1m", "近 1 月", 1),
    ("3m", "近 3 月", 3),
    ("6m", "近半年", 6),
    ("1y", "近 1 年", 12),
    ("ytd", "今年以来", None),
    ("all", "成立以来", None),
)
STAGE_SPECS: tuple[tuple[str, str, int], ...] = (
    ("5d", "近 5 日", 5),
    ("20d", "近 20 日", 20),
    ("60d", "近 60 日", 60),
)


def series_return(series: Mapping[str, Any] | None) -> float | None:
    """基准序列首尾收益；序列不完整时返回 None。"""

    if not series:
        return None
    totals = series.get("totals")
    if not isinstance(totals, list) or len(totals) < 2:
        return None
    first, last = float(totals[0]), float(totals[-1])
    return last / first - 1 if first > 0 else None


def day_return(equity: Sequence[Mapping[str, Any]], initial_cash: float) -> float | None:
    """最新净值日的收益；只有一天净值时相对初始资金（首日开盘买入、收盘计值）。"""

    if not equity:
        return None
    if len(equity) == 1:
        return float(equity[0]["value"]) / initial_cash - 1 if initial_cash > 0 else None
    previous = float(equity[-2]["value"])
    return float(equity[-1]["value"]) / previous - 1 if previous > 0 else None


def detail_metrics(
    *,
    equity: Sequence[Mapping[str, Any]],
    initial_cash: float,
    trades: Sequence[Mapping[str, Any]],
    positions: Sequence[Mapping[str, Any]],
    benchmark: Mapping[str, Any] | None,
    holdings: Mapping[str, Any] | None,
) -> dict[str, object]:
    days = [date.fromisoformat(str(point["date"])) for point in equity]
    values = [float(point["value"]) for point in equity]
    return {
        "trading": _trading(trades, positions),
        "risk": _risk(days, values, initial_cash),
        "ranges": _ranges(days, values, benchmark, holdings),
        "stages": _stages(days, values, benchmark, holdings),
    }


def _trading(
    trades: Sequence[Mapping[str, Any]],
    positions: Sequence[Mapping[str, Any]],
) -> dict[str, object]:
    sells = [trade for trade in trades if trade.get("direction") == "SELL"]
    pnls = [float(trade.get("pnl") or 0.0) for trade in sells]
    wins = [pnl for pnl in pnls if pnl > 0]
    losses = [pnl for pnl in pnls if pnl < 0]
    avg_win = statistics.fmean(wins) if wins else None
    avg_loss = abs(statistics.fmean(losses)) if losses else None
    ranks = [
        float(trade["rank"])
        for trade in trades
        if trade.get("direction") == "BUY" and isinstance(trade.get("rank"), (int, float))
    ]
    return {
        "closed": len(sells),
        "win_rate": len(wins) / len(sells) if sells else None,
        "payoff_ratio": avg_win / avg_loss if avg_win is not None and avg_loss else None,
        "realized_pnl": sum(pnls),
        "unrealized_pnl": sum(
            float(position["size"]) * float(position["mark"]) - float(position["cost"])
            for position in positions
        ),
        "avg_rank": statistics.fmean(ranks) if ranks else None,
    }


def _risk(
    days: Sequence[date],
    values: Sequence[float],
    initial_cash: float,
) -> dict[str, object]:
    high = initial_cash
    drawdown = 0.0
    peak_day = days[0] if days else None
    max_days = 0
    for day, value in zip(days, values, strict=True):
        if value >= high:
            high = value
            peak_day = day
        elif peak_day is not None:
            max_days = max(max_days, (day - peak_day).days)
        drawdown = max(drawdown, 1 - value / high if high > 0 else 0.0)
    current = 1 - values[-1] / high if values and high > 0 else 0.0
    returns: list[float] = []
    previous = initial_cash
    for value in values:
        if previous > 0:
            returns.append(value / previous - 1)
        previous = value
    volatility = sharpe = None
    if len(values) >= RISK_SAMPLE_MIN_DAYS and len(returns) >= 2:
        deviation = statistics.stdev(returns)
        if deviation > 0:
            volatility = deviation * math.sqrt(TRADING_DAYS_PER_YEAR)
            sharpe = statistics.fmean(returns) / deviation * math.sqrt(TRADING_DAYS_PER_YEAR)
    return {
        "max_drawdown": drawdown,
        "max_drawdown_days": max_days,
        "current_drawdown": current,
        "volatility": volatility,
        "sharpe": sharpe,
        "sample_days": len(values),
    }


def _ranges(
    days: Sequence[date],
    values: Sequence[float],
    benchmark: Mapping[str, Any] | None,
    holdings: Mapping[str, Any] | None,
) -> list[dict[str, object]]:
    if not days:
        return []
    end = days[-1]
    benchmark_days, benchmark_values = _series(benchmark)
    holdings_days, holdings_values = _series(holdings)
    rows: list[dict[str, object]] = []
    for key, label, months in RANGE_SPECS:
        start = _range_start(key, end, months)
        base = _series_base(days, values, start)
        strategy = _window_return(days, values, start)
        benchmark_return = _window_return(benchmark_days, benchmark_values, base)
        holdings_return = _window_return(holdings_days, holdings_values, base)
        rows.append(
            {
                "key": key,
                "label": label,
                "start": base.isoformat() if base else None,
                "strategy": strategy,
                "benchmark": benchmark_return,
                "holdings": holdings_return,
                "excess_benchmark": _excess(strategy, benchmark_return),
                "excess_holdings": _excess(strategy, holdings_return),
            }
        )
    return rows


def _stages(
    days: Sequence[date],
    values: Sequence[float],
    benchmark: Mapping[str, Any] | None,
    holdings: Mapping[str, Any] | None,
) -> list[dict[str, object]]:
    if not days:
        return []
    benchmark_days, benchmark_values = _series(benchmark)
    holdings_days, holdings_values = _series(holdings)
    rows: list[dict[str, object]] = []
    for key, label, span in STAGE_SPECS:
        index = max(0, len(days) - 1 - span)
        start = days[index]
        strategy = _window_return(days, values, start)
        benchmark_return = _window_return(benchmark_days, benchmark_values, start)
        holdings_return = _window_return(holdings_days, holdings_values, start)
        rows.append(
            {
                "key": key,
                "label": label,
                "days": len(days) - 1 - index,
                "start": start.isoformat(),
                "strategy": strategy,
                "benchmark": benchmark_return,
                "holdings": holdings_return,
                "excess_benchmark": _excess(strategy, benchmark_return),
                "excess_holdings": _excess(strategy, holdings_return),
            }
        )
    return rows


def _excess(strategy: float | None, other: float | None) -> float | None:
    return strategy - other if strategy is not None and other is not None else None


def _series(raw: Mapping[str, Any] | None) -> tuple[list[date], list[float]]:
    if not raw:
        return [], []
    dates = raw.get("dates")
    totals = raw.get("totals")
    if not isinstance(dates, list) or not isinstance(totals, list):
        return [], []
    pairs = [
        (date.fromisoformat(str(item)[:10]), float(value))
        for item, value in zip(dates, totals, strict=False)
        if value is not None and math.isfinite(float(value))
    ]
    return [pair[0] for pair in pairs], [pair[1] for pair in pairs]


def _range_start(key: str, end: date, months: int | None) -> date | None:
    if key == "all":
        return None
    if key == "ytd":
        return date(end.year, 1, 1)
    assert months is not None
    year, month = end.year, end.month - months
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, min(end.day, calendar.monthrange(year, month)[1]))


def _series_base(
    days: Sequence[date],
    values: Sequence[float],
    start: date | None,
) -> date | None:
    if not days:
        return None
    if start is None:
        return days[0]
    return next((day for day in days if day >= start), None)


def _window_return(
    days: Sequence[date],
    values: Sequence[float],
    start: date | None,
) -> float | None:
    """窗口收益：从窗口内第一个净值点到最后一个点；不足两点返回 None。"""

    if len(days) < 2 or len(days) != len(values):
        return None
    index = 0
    if start is not None:
        index = next((i for i, day in enumerate(days) if day >= start), len(days))
        if index >= len(days):
            return None
    if index == len(days) - 1:
        return 0.0
    base = values[index]
    return values[-1] / base - 1 if base > 0 else None
