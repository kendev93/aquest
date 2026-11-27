"""模拟盘的展示指标：列表的同期对照、详情的交易 / 风险 / 相对基准 / 区间 / 阶段收益。

纯函数，输入读接口已经拿到的净值、基准序列、成交与持仓；不读数据库，
避免同一口径在展示层再分叉。基准序列允许带一个早于首个净值日的期初点，
区间与阶段收益都从同一窗口起点取基准，保证「同期」是同一个窗口。

年化与比率类指标（年化收益、波动、夏普、Sortino、Calmar、Beta、信息比率、
捕获率）至少需要 20 个净值日样本，样本更短时返回 None，展示层只显示回撤类
与原始分布，不拿几天的噪声冒充统计结论。
"""

from __future__ import annotations

import calendar
import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from typing import Any

#: 年化 / 比率类指标至少需要的净值点数。
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
    cash: float,
    trades: Sequence[Mapping[str, Any]],
    positions: Sequence[Mapping[str, Any]],
    benchmark: Mapping[str, Any] | None,
    holdings: Mapping[str, Any] | None,
) -> dict[str, object]:
    days = [date.fromisoformat(str(point["date"])) for point in equity]
    values = [float(point["value"]) for point in equity]
    returns = _daily_returns(values, initial_cash)
    benchmark_days, benchmark_values = _series(benchmark)
    return {
        "trading": _trading(trades, positions),
        "risk": _risk(days, values, returns, initial_cash),
        "relative": _relative(days, returns, benchmark_days, benchmark_values),
        "concentration": _concentration(values, cash, positions),
        "ranges": _ranges(days, values, benchmark, holdings),
        "stages": _stages(days, values, benchmark, holdings),
    }


def _daily_returns(values: Sequence[float], initial_cash: float) -> list[float]:
    returns: list[float] = []
    previous = initial_cash
    for value in values:
        if previous > 0:
            returns.append(value / previous - 1)
        previous = value
    return returns


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
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    ranks = [
        float(trade["rank"])
        for trade in trades
        if trade.get("direction") == "BUY" and isinstance(trade.get("rank"), (int, float))
    ]
    return {
        "closed": len(sells),
        "win_rate": len(wins) / len(sells) if sells else None,
        "payoff_ratio": avg_win / avg_loss if avg_win is not None and avg_loss else None,
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else None,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "best_trade": max(pnls) if pnls else None,
        "worst_trade": min(pnls) if pnls else None,
        "expectancy": statistics.fmean(pnls) if pnls else None,
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
    returns: Sequence[float],
    initial_cash: float,
) -> dict[str, object]:
    sample = len(values)
    high = initial_cash
    drawdown = 0.0
    drawdowns: list[float] = []
    peak_day = days[0] if days else None
    max_days = 0
    for day, value in zip(days, values, strict=True):
        if value >= high:
            high = value
            peak_day = day
        elif peak_day is not None:
            max_days = max(max_days, (day - peak_day).days)
        current = 1 - value / high if high > 0 else 0.0
        drawdown = max(drawdown, current)
        drawdowns.append(current)
    last = values[-1] if values else initial_cash
    current = 1 - last / high if high > 0 else 0.0
    total_return = last / initial_cash - 1 if initial_cash > 0 else 0.0
    gated = sample >= RISK_SAMPLE_MIN_DAYS
    annual_return = None
    if gated and sample > 0 and total_return > -1:
        annual_return = (1 + total_return) ** (TRADING_DAYS_PER_YEAR / sample) - 1
    volatility = sharpe = sortino = calmar = var95 = downside_volatility = None
    if gated and len(returns) >= 2:
        deviation = statistics.stdev(returns)
        if deviation > 0:
            volatility = deviation * math.sqrt(TRADING_DAYS_PER_YEAR)
            sharpe = statistics.fmean(returns) / deviation * math.sqrt(TRADING_DAYS_PER_YEAR)
            downside = math.sqrt(statistics.fmean(min(value, 0.0) ** 2 for value in returns))
            downside_volatility = downside * math.sqrt(TRADING_DAYS_PER_YEAR)
            if downside > 0:
                sortino = statistics.fmean(returns) / downside * math.sqrt(TRADING_DAYS_PER_YEAR)
        if annual_return is not None and drawdown > 0:
            calmar = annual_return / drawdown
        var95 = sorted(returns)[max(0, math.floor(0.05 * (len(returns) - 1)))]
    up_streak = down_streak = max_up = max_down = 0
    for value in returns:
        if value > 0:
            up_streak += 1
            down_streak = 0
        elif value < 0:
            down_streak += 1
            up_streak = 0
        else:
            up_streak = down_streak = 0
        max_up = max(max_up, up_streak)
        max_down = max(max_down, down_streak)
    return {
        "max_drawdown": drawdown,
        "max_drawdown_days": max_days,
        "avg_drawdown": statistics.fmean(drawdowns) if drawdowns else None,
        "current_drawdown": current,
        "underwater_ratio": (
            sum(1 for value in drawdowns if value > 0) / len(drawdowns) if drawdowns else None
        ),
        "sample_days": sample,
        "annual_return": annual_return,
        "volatility": volatility,
        "downside_volatility": downside_volatility,
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": calmar,
        "var95": var95,
        "max_daily_gain": max(returns) if returns else None,
        "max_daily_loss": min(returns) if returns else None,
        "daily_win_rate": (
            sum(1 for value in returns if value > 0) / len(returns) if returns else None
        ),
        "avg_daily_return": statistics.fmean(returns) if returns else None,
        "max_up_streak": max_up,
        "max_down_streak": max_down,
    }


def _relative(
    days: Sequence[date],
    returns: Sequence[float],
    benchmark_days: Sequence[date],
    benchmark_values: Sequence[float],
) -> dict[str, object]:
    benchmark_returns: dict[date, float] = {}
    for index in range(1, len(benchmark_days)):
        previous = benchmark_values[index - 1]
        if previous > 0:
            benchmark_returns[benchmark_days[index]] = benchmark_values[index] / previous - 1
    pairs = [
        (value, benchmark_returns[day])
        for day, value in zip(days, returns, strict=True)
        if day in benchmark_returns
    ]
    empty: dict[str, object] = {
        "days": len(pairs),
        "beta": None,
        "correlation": None,
        "tracking_error": None,
        "information_ratio": None,
        "up_capture": None,
        "down_capture": None,
        "excess_max_drawdown": None,
    }
    if len(pairs) < 2:
        return empty
    strategy = [pair[0] for pair in pairs]
    bench = [pair[1] for pair in pairs]
    mean_strategy = statistics.fmean(strategy)
    mean_bench = statistics.fmean(bench)
    variance_bench = statistics.variance(bench)
    covariance = sum(
        (left - mean_strategy) * (right - mean_bench)
        for left, right in zip(strategy, bench, strict=True)
    ) / (len(pairs) - 1)
    beta = covariance / variance_bench if variance_bench > 0 else None
    correlation = None
    strategy_variance = statistics.variance(strategy)
    if strategy_variance > 0 and variance_bench > 0:
        correlation = covariance / math.sqrt(strategy_variance * variance_bench)
    excess = [left - right for left, right in zip(strategy, bench, strict=True)]
    value = 1.0
    high = 1.0
    excess_drawdown = 0.0
    for item in excess:
        value *= 1 + item
        high = max(high, value)
        excess_drawdown = max(excess_drawdown, 1 - value / high if high > 0 else 0.0)
    gated = len(pairs) >= RISK_SAMPLE_MIN_DAYS
    tracking_error = information_ratio = up_capture = down_capture = None
    if gated:
        deviation = statistics.stdev(excess)
        tracking_error = deviation * math.sqrt(TRADING_DAYS_PER_YEAR)
        if deviation > 0:
            information_ratio = (
                statistics.fmean(excess) / deviation * math.sqrt(TRADING_DAYS_PER_YEAR)
            )
        up_strategy = _compound(strategy[index] for index, item in enumerate(bench) if item > 0)
        up_bench = _compound(bench[index] for index, item in enumerate(bench) if item > 0)
        down_strategy = _compound(strategy[index] for index, item in enumerate(bench) if item < 0)
        down_bench = _compound(bench[index] for index, item in enumerate(bench) if item < 0)
        up_capture = (
            up_strategy / up_bench
            if up_strategy is not None and up_bench is not None and up_bench != 0
            else None
        )
        down_capture = (
            down_strategy / down_bench
            if down_strategy is not None and down_bench is not None and down_bench != 0
            else None
        )
    return {
        "days": len(pairs),
        "beta": beta,
        "correlation": correlation,
        "tracking_error": tracking_error,
        "information_ratio": information_ratio,
        "up_capture": up_capture,
        "down_capture": down_capture,
        "excess_max_drawdown": excess_drawdown,
    }


def _compound(values: Iterable[float]) -> float | None:
    total = 1.0
    counted = 0
    for value in values:
        total *= 1 + value
        counted += 1
    return total - 1 if counted else None


def _concentration(
    values: Sequence[float],
    cash: float,
    positions: Sequence[Mapping[str, Any]],
) -> dict[str, object]:
    equity = values[-1] if values else 0.0
    if equity <= 0:
        return {"max_weight": None, "top3_weight": None, "hhi": None, "cash_ratio": None}
    weights = sorted(
        (float(position["size"]) * float(position["mark"]) / equity for position in positions),
        reverse=True,
    )
    return {
        "max_weight": weights[0] if weights else 0.0,
        "top3_weight": sum(weights[:3]),
        "hhi": sum(weight * weight for weight in weights),
        "cash_ratio": cash / equity,
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
