"""Report assembly and portfolio-level metrics."""

from __future__ import annotations

import hashlib
from collections import deque
from collections.abc import Mapping, Sequence
from datetime import date
from typing import cast

import numpy as np
import pandas as pd

from selector_app.backtest.performance import PerformanceAnalyzer

from .benchmark import equal_weight_buy_hold
from .context import _ResolvedRefs, _StockSeries
from .models import PortfolioBacktestConfig, PortfolioBacktestReport, PortfolioJsonValue
from .risk import _risk_controls
from .state import _PortfolioServiceState


class PortfolioReportMixin(_PortfolioServiceState):
    """Turn simulation rows into the public immutable portfolio report."""

    @staticmethod
    def _build_search_summary(
        config: PortfolioBacktestConfig,
        simulation: Mapping[str, object],
        data_fingerprint: str,
    ) -> dict[str, object]:
        """Calculate only the metrics consumed by candidate search.

        Candidate evaluations do not need the public report's full equity,
        holdings, benchmark, and diagnostic tables. Keep the canonical
        PerformanceAnalyzer formulas while constructing only its minimal input
        columns; the normal backtest still uses ``_build_report`` unchanged.
        """

        equity = cast(list[dict[str, PortfolioJsonValue]], simulation["equity"])
        trades = cast(list[dict[str, PortfolioJsonValue]], simulation["trades"])
        equity_frame = pd.DataFrame(
            {"total": [float(cast(float, row["total"])) for row in equity]}
        )
        trade_frame = pd.DataFrame(
            [
                {
                    "direction": trade.get("direction"),
                    "pnl": trade.get("pnl"),
                    "cost_basis": trade.get("cost_basis"),
                    "rejected": trade.get("rejected", False),
                }
                for trade in trades
            ],
            columns=["direction", "pnl", "cost_basis", "rejected"],
        )
        raw_performance = PerformanceAnalyzer(equity_frame, trade_frame).compute()
        performance: dict[str, float | None] = {
            key: _finite_number(raw_performance.get(key, 0.0))
            for key in (
                "total_return",
                "annual_return",
                "sharpe",
                "max_drawdown",
                "total_trades",
                "win_rate",
                "volatility",
            )
        }
        turnover = _portfolio_turnover(trades, config.initial_cash)
        return {
            "performance": performance,
            "data_fingerprint": data_fingerprint,
            "turnover": turnover,
            "trades": trades,
            "diagnostic": simulation.get("diagnostic"),
        }

    @staticmethod
    def _search_summary_from_report(report: PortfolioBacktestReport) -> dict[str, object]:
        """Project an empty edge-case report onto the candidate-search contract."""

        return {
            "performance": {
                key: report.performance.get(key)
                for key in (
                    "total_return",
                    "annual_return",
                    "sharpe",
                    "max_drawdown",
                    "total_trades",
                    "win_rate",
                    "volatility",
                )
            },
            "data_fingerprint": report.data_fingerprint,
            "turnover": report.performance.get("turnover", 0.0),
            "trades": report.trades,
            "diagnostic": report.diagnostic,
        }

    def _build_report(
        self,
        config: PortfolioBacktestConfig,
        dates: list[int],
        total_candidates: int,
        processed: int,
        skipped: int,
        failure_reasons: Mapping[str, int],
        simulation: Mapping[str, object],
        data_fingerprint: str = "",
        resolved: _ResolvedRefs | None = None,
        benchmark_contexts: Sequence[_StockSeries] | None = None,
    ) -> PortfolioBacktestReport:
        equity = cast(list[dict[str, PortfolioJsonValue]], simulation["equity"])
        trades = cast(list[dict[str, PortfolioJsonValue]], simulation["trades"])
        states = cast(list[dict[str, PortfolioJsonValue]], simulation["states"])
        ranking_events = cast(list[dict[str, PortfolioJsonValue]], simulation["ranking_events"])
        equity_frame = pd.DataFrame(equity)
        trade_frame = pd.DataFrame(
            [
                {
                    "datetime": _date_int(cast(str, trade["date"])),
                    "direction": trade["direction"],
                    "size": trade["size"],
                    "price": trade["price"],
                    "commission": float(cast(float, trade["commission"]))
                    + float(cast(float, trade["stamp_tax"])),
                    "slippage": trade["slippage"],
                    "pnl": trade["pnl"],
                    "cost_basis": trade["cost_basis"],
                    "rejected": trade["rejected"],
                }
                for trade in trades
            ],
            columns=[
                "datetime",
                "direction",
                "size",
                "price",
                "commission",
                "slippage",
                "pnl",
                "cost_basis",
                "rejected",
            ],
        )
        diagnostic: str | None
        if equity_frame.empty:
            performance: dict[str, float | None] = {}
            diagnostic = "没有可用于回测的组合净值数据"
        else:
            peak = equity_frame["total"].cummax()
            equity_frame["drawdown"] = peak - equity_frame["total"]
            equity_frame["drawdown_pct"] = np.divide(
                equity_frame["drawdown"],
                peak,
                out=np.zeros(len(equity_frame), dtype=float),
                where=peak.to_numpy() != 0,
            )
            analyzer = PerformanceAnalyzer(equity_frame, trade_frame)
            performance = {
                name: _finite_number(value) for name, value in analyzer.compute().items()
            }
            performance["avg_holding_days"] = _average_holding_days(trades)
            performance["turnover"] = _portfolio_turnover(trades, config.initial_cash)
            simulation_diagnostic = simulation.get("diagnostic")
            diagnostic = (
                str(simulation_diagnostic)
                if isinstance(simulation_diagnostic, str) and simulation_diagnostic
                else analyzer.diagnostic
            )
        serialized_equity = tuple(
            {
                **record,
                "drawdown": float(equity_frame.iloc[index]["drawdown"]),
                "drawdown_pct": float(equity_frame.iloc[index]["drawdown_pct"]),
            }
            for index, record in enumerate(equity)
        )
        return PortfolioBacktestReport(
            universe=config.universe,
            total_candidates=total_candidates,
            processed=processed,
            skipped=skipped,
            errors=sum(failure_reasons.values()),
            bars=len(dates),
            start_date=_date_text(dates[0]),
            end_date=_date_text(dates[-1]),
            max_positions=config.max_positions,
            ranking_value=config.ranking_value,
            rank_order=config.rank_order,
            fitness_rank_weight=config.fitness_rank_weight,
            fitness_min_trades=config.fitness_min_trades,
            fitness_max_drawdown=config.fitness_max_drawdown,
            performance=performance,
            equity_curve=serialized_equity,
            trades=tuple(trades),
            states=tuple(states),
            ranking_events=tuple(ranking_events),
            failure_reasons=failure_reasons,
            diagnostic=diagnostic,
            data_fingerprint=data_fingerprint,
            risk_controls=_risk_controls(config),
            missing_codes=resolved.missing if resolved is not None else (),
            filtered_codes=resolved.filtered if resolved is not None else (),
            excluded_st_codes=resolved.excluded_st if resolved is not None else (),
            excluded_st_count=len(resolved.excluded_st) if resolved is not None else 0,
            benchmark=_benchmark_block(benchmark_contexts, dates, config),
            next_plan=_next_plan_block(simulation),
        )

    @staticmethod
    def _empty_report(
        config: PortfolioBacktestConfig,
        total_candidates: int,
        failure_reasons: Mapping[str, int],
        resolved: _ResolvedRefs | None = None,
    ) -> PortfolioBacktestReport:
        return PortfolioBacktestReport(
            universe=config.universe,
            total_candidates=total_candidates,
            processed=0,
            skipped=total_candidates,
            errors=sum(failure_reasons.values()),
            bars=0,
            start_date=config.start_date.isoformat() if config.start_date else "",
            end_date=config.end_date.isoformat() if config.end_date else "",
            max_positions=config.max_positions,
            ranking_value=config.ranking_value,
            rank_order=config.rank_order,
            fitness_rank_weight=config.fitness_rank_weight,
            fitness_min_trades=config.fitness_min_trades,
            fitness_max_drawdown=config.fitness_max_drawdown,
            performance={},
            equity_curve=(),
            trades=(),
            states=(),
            ranking_events=(),
            failure_reasons=failure_reasons,
            diagnostic="没有可用于回测的股票数据",
            risk_controls=_risk_controls(config),
            missing_codes=resolved.missing if resolved is not None else (),
            filtered_codes=resolved.filtered if resolved is not None else (),
            excluded_st_codes=resolved.excluded_st if resolved is not None else (),
            excluded_st_count=len(resolved.excluded_st) if resolved is not None else 0,
        )


def _next_plan_block(
    simulation: Mapping[str, object],
) -> dict[str, PortfolioJsonValue]:
    """把模拟收尾时排定的明日计划透传出来。没有就给空，不算错误。"""

    plan = simulation.get("next_plan")
    if not isinstance(plan, Mapping):
        return {}
    return {str(key): value for key, value in plan.items()}


def _benchmark_block(
    contexts: Sequence[_StockSeries] | None,
    dates: list[int],
    config: PortfolioBacktestConfig,
) -> dict[str, object] | None:
    """基准对照。算不出来就返回 None，报告其余部分照常。"""

    if not contexts:
        return None
    curve = equal_weight_buy_hold(contexts, dates, config.initial_cash)
    return curve.to_dict() if curve is not None else None


def _date_text(value: int) -> str:
    return str(pd.to_datetime(str(value), format="%Y%m%d").strftime("%Y-%m-%d"))


def _portfolio_fingerprint(contexts: list[_StockSeries]) -> str:
    digest = hashlib.sha256()
    for context in sorted(contexts, key=lambda item: item.symbol):
        digest.update(f"{context.symbol}:{context.data_fingerprint}\n".encode("ascii"))
    return digest.hexdigest()


def _portfolio_turnover(
    trades: Sequence[Mapping[str, PortfolioJsonValue]], initial_cash: float
) -> float:
    if initial_cash <= 0:
        return 0.0
    total_notional = sum(
        float(cast(float, trade.get("size", 0))) * float(cast(float, trade.get("price", 0)))
        for trade in trades
    )
    return total_notional / initial_cash


def _date_int(value: str) -> int:
    return int(value.replace("-", ""))


def _finite_number(value: object) -> float | None:
    number = float(cast(float, value))
    return number if np.isfinite(number) else None


def _average_holding_days(trades: list[dict[str, PortfolioJsonValue]]) -> float:
    """Pair buys and sells with FIFO queues kept separately for each stock."""

    buy_queues: dict[str, deque[tuple[date, float]]] = {}
    total_days = 0.0
    total_size = 0.0
    for trade in trades:
        if bool(trade.get("rejected", False)):
            continue
        try:
            timestamp = pd.Timestamp(trade["date"])
            if pd.isna(timestamp):
                continue
            trade_date = timestamp.date()
            size = float(cast(float, trade["size"]))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if size <= 0:
            continue
        symbol = f"{trade.get('market', '')}{trade.get('code', '')}"
        queue = buy_queues.setdefault(symbol, deque())
        if trade.get("direction") == "BUY":
            queue.append((trade_date, size))
            continue
        if trade.get("direction") != "SELL":
            continue
        remaining = size
        while remaining > 0 and queue:
            buy_date, buy_size = queue[0]
            consumed = min(remaining, buy_size)
            total_days += max((trade_date - buy_date).days, 0) * consumed
            total_size += consumed
            remaining -= consumed
            buy_size -= consumed
            if buy_size <= 0:
                queue.popleft()
            else:
                queue[0] = (buy_date, buy_size)
    return total_days / total_size if total_size else 0.0


__all__ = ["PortfolioReportMixin"]
