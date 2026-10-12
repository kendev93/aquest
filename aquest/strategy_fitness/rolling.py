"""Causal, expanding-history fitness decisions for portfolio candidates."""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

import numpy as np


@dataclass(frozen=True)
class RollingFitnessDecision:
    """Causal suitability snapshot for one candidate on one signal date."""

    score: float
    trades: int
    expectancy: float | None
    profit_factor: float | None
    total_return: float | None
    max_drawdown: float | None
    note: str


@dataclass(frozen=True)
class RollingFitnessHistory:
    trade_dates: tuple[int, ...]
    trade_returns: tuple[float, ...]
    trade_pnls: tuple[float, ...]
    trade_count_prefix: tuple[int, ...]
    trade_return_prefix: tuple[float, ...]
    trade_profit_prefix: tuple[float, ...]
    trade_loss_prefix: tuple[float, ...]
    trade_win_prefix: tuple[int, ...]
    equity_dates: tuple[int, ...]
    equity_totals: tuple[float, ...]
    equity_drawdowns: tuple[float, ...]
    equity_max_drawdown_prefix: tuple[float, ...]

    @classmethod
    def from_records(
        cls,
        *,
        trades: Sequence[Mapping[str, object]],
        equity: Sequence[Mapping[str, object]],
    ) -> RollingFitnessHistory:
        trade_rows: list[tuple[int, float, float]] = []
        for trade in trades:
            if trade.get("direction") != "SELL" or bool(trade.get("rejected", False)):
                continue
            try:
                trade_date = _date_int(trade["date"])
                pnl = float(cast(float, trade["pnl"]))
                cost_basis = float(cast(float, trade["cost_basis"]))
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if not np.isfinite(pnl) or not np.isfinite(cost_basis) or cost_basis <= 0:
                continue
            trade_rows.append((trade_date, pnl / cost_basis, pnl))
        trade_rows.sort(key=lambda row: row[0])

        equity_rows: list[tuple[int, float, float | None]] = []
        for point in equity:
            try:
                point_date = _date_int(point["date"])
                total = float(cast(float, point["total"]))
                raw_drawdown = point.get("drawdown_pct")
                drawdown = float(cast(float, raw_drawdown)) if raw_drawdown is not None else None
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if np.isfinite(total) and (drawdown is None or np.isfinite(drawdown)):
                equity_rows.append((point_date, total, drawdown))
        equity_rows.sort(key=lambda row: row[0])

        trade_count_prefix = [0]
        trade_return_prefix = [0.0]
        trade_profit_prefix = [0.0]
        trade_loss_prefix = [0.0]
        trade_win_prefix = [0]
        for _, trade_return, pnl in trade_rows:
            trade_count_prefix.append(trade_count_prefix[-1] + 1)
            trade_return_prefix.append(trade_return_prefix[-1] + trade_return)
            trade_profit_prefix.append(trade_profit_prefix[-1] + (pnl if pnl > 0 else 0.0))
            trade_loss_prefix.append(trade_loss_prefix[-1] + (pnl if pnl < 0 else 0.0))
            trade_win_prefix.append(trade_win_prefix[-1] + (1 if pnl > 0 else 0))

        equity_max_drawdown_prefix = [0.0]
        peak = -np.inf
        for _, total, raw_drawdown in equity_rows:
            peak = max(peak, total)
            derived_drawdown = (peak - total) / peak if peak != 0 else 0.0
            drawdown = raw_drawdown if raw_drawdown is not None else derived_drawdown
            equity_max_drawdown_prefix.append(max(equity_max_drawdown_prefix[-1], drawdown))

        return cls(
            trade_dates=tuple(row[0] for row in trade_rows),
            trade_returns=tuple(row[1] for row in trade_rows),
            trade_pnls=tuple(row[2] for row in trade_rows),
            trade_count_prefix=tuple(trade_count_prefix),
            trade_return_prefix=tuple(trade_return_prefix),
            trade_profit_prefix=tuple(trade_profit_prefix),
            trade_loss_prefix=tuple(trade_loss_prefix),
            trade_win_prefix=tuple(trade_win_prefix),
            equity_dates=tuple(row[0] for row in equity_rows),
            equity_totals=tuple(row[1] for row in equity_rows),
            equity_drawdowns=tuple(row[2] if row[2] is not None else 0.0 for row in equity_rows),
            equity_max_drawdown_prefix=tuple(equity_max_drawdown_prefix),
        )


class RollingFitnessFilter:
    """Decide candidate eligibility using only records before a signal date."""

    def __init__(
        self,
        histories: Mapping[str, RollingFitnessHistory],
        *,
        min_trades: int,
        max_drawdown: float,
    ) -> None:
        self._histories = histories
        self._min_trades = min_trades
        self._max_drawdown = max_drawdown

    def decide(self, symbol: str, current_date: int) -> RollingFitnessDecision:
        history = self._histories.get(symbol)
        if history is None:
            return _no_history("没有历史评估数据")

        trade_end = bisect_left(history.trade_dates, current_date)
        equity_end = bisect_left(history.equity_dates, current_date)
        trades = history.trade_count_prefix[trade_end]
        if equity_end == 0:
            return _no_history(f"历史净值不足（{trades}/{self._min_trades} 笔成交）", trades=trades)

        expectancy = history.trade_return_prefix[trade_end] / trades if trades else None
        profit = history.trade_profit_prefix[trade_end]
        loss = history.trade_loss_prefix[trade_end]
        if profit > 0 and loss < 0:
            profit_factor = profit / abs(loss)
        elif profit > 0:
            profit_factor = 999.0
        else:
            profit_factor = 0.0
        first_total = history.equity_totals[0]
        latest_total = history.equity_totals[equity_end - 1]
        total_return = (latest_total / first_total - 1) if first_total != 0 else None
        max_drawdown = history.equity_max_drawdown_prefix[equity_end]
        checks = (
            trades >= self._min_trades,
            expectancy is not None and expectancy > 0,
            profit_factor > 1,
            total_return is not None and total_return > 0,
            max_drawdown <= self._max_drawdown,
        )
        score = round(sum(checks) / len(checks) * 100, 2)
        note = (
            f"历史成交不足（{trades}/{self._min_trades}）"
            if trades < self._min_trades
            else f"历史成交 {trades} 笔"
        )
        return RollingFitnessDecision(
            score=score,
            trades=trades,
            expectancy=expectancy,
            profit_factor=profit_factor,
            total_return=total_return,
            max_drawdown=max_drawdown,
            note=note,
        )


def _no_history(note: str, *, trades: int = 0) -> RollingFitnessDecision:
    return RollingFitnessDecision(
        score=0.0,
        trades=trades,
        expectancy=None,
        profit_factor=None,
        total_return=None,
        max_drawdown=None,
        note=note,
    )


def _date_int(value: object) -> int:
    return int(str(value).replace("-", ""))
