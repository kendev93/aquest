"""Cross-stock trade statistics without constructing a portfolio curve."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import cast

import numpy as np

from aquest.backtest.models import BacktestReport

_Report = Mapping[str, object] | BacktestReport


def summarize_backtests(
    reports: Sequence[_Report],
    *,
    baseline_reports: Sequence[_Report] | None = None,
) -> dict[str, object]:
    """Pool closed trades and report per-stock metric distributions."""

    normalized = [_report_dict(report) for report in reports]
    baseline_records = [_report_dict(item) for item in (baseline_reports or ())]
    baselines = {_stock_key(report): report for report in baseline_records}
    stock_rows: list[dict[str, object]] = []
    all_trades: list[dict[str, float]] = []
    for report in normalized:
        performance = _mapping(report.get("performance"))
        trades = _closed_trades(report.get("trades"))
        all_trades.extend(trades)
        win_rate = _ratio(
            sum(1 for trade in trades if trade["pnl"] > 0),
            len(trades),
        )
        returns = [trade["return"] for trade in trades]
        wins = [trade for trade in trades if trade["pnl"] > 0]
        losses = [trade for trade in trades if trade["pnl"] <= 0]
        win_returns = [trade["return"] for trade in wins]
        loss_returns = [trade["return"] for trade in losses]
        payoff_ratio = _ratio(
            float(np.mean(win_returns)) if win_returns else None,
            abs(float(np.mean(loss_returns))) if loss_returns else None,
        )
        profit_factor = _ratio(
            sum(trade["pnl"] for trade in wins),
            abs(sum(trade["pnl"] for trade in losses)),
        )
        row: dict[str, object] = {
            "market": report.get("market"),
            "code": report.get("code"),
            "name": report.get("name"),
            "closed_trades": len(trades),
            "win_rate": win_rate,
            "payoff_ratio": payoff_ratio,
            "profit_factor": profit_factor,
            "mean_trade_return": _mean(returns),
            "median_trade_return": _median(returns),
            "total_return": _optional_number(performance.get("total_return")),
            "annual_return": _optional_number(performance.get("annual_return")),
            "max_drawdown": _optional_number(performance.get("max_drawdown")),
            "sharpe": _optional_number(performance.get("sharpe")),
        }
        baseline = baselines.get(_stock_key(report))
        if baseline is not None:
            baseline_rate = _ratio(
                sum(1 for trade in _closed_trades(baseline.get("trades")) if trade["pnl"] > 0),
                len(_closed_trades(baseline.get("trades"))),
            )
            row["baseline_win_rate"] = baseline_rate
            row["improved_win_rate"] = (
                bool(win_rate > baseline_rate)
                if win_rate is not None and baseline_rate is not None
                else None
            )
        else:
            row["baseline_win_rate"] = None
            row["improved_win_rate"] = None
        stock_rows.append(row)

    all_wins = [trade for trade in all_trades if trade["pnl"] > 0]
    all_losses = [trade for trade in all_trades if trade["pnl"] <= 0]
    all_returns = [trade["return"] for trade in all_trades]
    pooled = {
        "closed_trades": len(all_trades),
        "wins": len(all_wins),
        "losses": len(all_losses),
        "win_rate": _ratio(len(all_wins), len(all_trades)),
        "payoff_ratio": _ratio(
            _mean([trade["return"] for trade in all_wins]),
            abs(cast(float, _mean([trade["return"] for trade in all_losses])))
            if all_losses
            else None,
        ),
        "profit_factor": _ratio(
            sum(trade["pnl"] for trade in all_wins),
            abs(sum(trade["pnl"] for trade in all_losses)),
        ),
        "mean_trade_return": _mean(all_returns),
        "median_trade_return": _median(all_returns),
        "stocks_with_trades": sum(
            1 for row in stock_rows if (_optional_number(row.get("closed_trades")) or 0.0) > 0
        ),
    }
    win_rates = [cast(float, row["win_rate"]) for row in stock_rows if row["win_rate"] is not None]
    improvement_rows = [row for row in stock_rows if isinstance(row.get("improved_win_rate"), bool)]
    stock_summary = {
        "stock_count": len(stock_rows),
        "median_win_rate": _median(win_rates),
        "median_payoff_ratio": _median(
            [
                cast(float, row["payoff_ratio"])
                for row in stock_rows
                if row["payoff_ratio"] is not None
            ]
        ),
        "median_total_return": _median(
            [
                cast(float, row["total_return"])
                for row in stock_rows
                if row["total_return"] is not None
            ]
        ),
        "median_annual_return": _median(
            [
                cast(float, row["annual_return"])
                for row in stock_rows
                if row["annual_return"] is not None
            ]
        ),
        "median_max_drawdown": _median(
            [
                cast(float, row["max_drawdown"])
                for row in stock_rows
                if row["max_drawdown"] is not None
            ]
        ),
        "median_sharpe": _median(
            [cast(float, row["sharpe"]) for row in stock_rows if row["sharpe"] is not None]
        ),
        "compared_stock_count": len(improvement_rows),
        "improved_stock_count": sum(
            1 for row in improvement_rows if row["improved_win_rate"] is True
        ),
        "improved_stock_ratio": _ratio(
            sum(1 for row in improvement_rows if row["improved_win_rate"] is True),
            len(improvement_rows),
        ),
    }
    return {"pooled_trades": pooled, "per_stock": stock_summary, "stocks": stock_rows}


def _report_dict(report: _Report) -> dict[str, object]:
    return report.to_dict() if isinstance(report, BacktestReport) else dict(report)


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _closed_trades(value: object) -> list[dict[str, float]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    trades: list[dict[str, float]] = []
    for item in value:
        if not isinstance(item, Mapping) or item.get("direction") != "SELL":
            continue
        if bool(item.get("rejected", False)):
            continue
        pnl = _optional_number(item.get("pnl"))
        cost_basis = _optional_number(item.get("cost_basis"))
        if pnl is None or cost_basis is None or cost_basis <= 0:
            continue
        trades.append({"pnl": pnl, "return": pnl / cost_basis})
    return trades


def _stock_key(report: Mapping[str, object]) -> tuple[str, str]:
    return str(report.get("market") or ""), str(report.get("code") or "")


def _optional_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    resolved = float(value)
    return resolved if math.isfinite(resolved) else None


def _ratio(numerator: float | int | None, denominator: float | int | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return float(numerator / denominator)


def _mean(values: Sequence[float]) -> float | None:
    return float(np.mean(values)) if values else None


def _median(values: Sequence[float]) -> float | None:
    return float(np.median(values)) if values else None
