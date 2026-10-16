"""Full-range strategy replay and period-performance models."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

import pandas as pd

from aquest.backtest.performance import PerformanceAnalyzer
from aquest.strategy_discovery.fingerprints import trade_fingerprint

ReplayLeaderboard = Literal["sharpe", "return"]
ReplayJobStatus = Literal["queued", "running", "completed", "failed", "cancelled"]


@dataclass(frozen=True)
class PeriodWindow:
    key: str
    label: str
    start_date: date
    end_date: date
    partial: bool = False


@dataclass(frozen=True)
class StrategyPeriodMetrics:
    key: str
    label: str
    start_date: str
    end_date: str
    bars: int
    total_return: float | None
    annual_return: float | None
    max_drawdown: float | None
    sharpe: float | None
    total_trades: int
    win_rate: float | None
    diagnostic: str | None = None
    partial: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "bars": self.bars,
            "total_return": self.total_return,
            "annual_return": self.annual_return,
            "max_drawdown": self.max_drawdown,
            "sharpe": self.sharpe,
            "total_trades": self.total_trades,
            "win_rate": self.win_rate,
            "diagnostic": self.diagnostic,
            "partial": self.partial,
        }


@dataclass(frozen=True)
class StrategyReplayCandidate:
    rank: int
    candidate_hash: str
    human_readable_rules: str
    config: Mapping[str, object]
    leaderboard_metrics: Mapping[str, object]
    overall_metrics: Mapping[str, object]
    periods: Mapping[str, StrategyPeriodMetrics]
    annual_metrics: tuple[StrategyPeriodMetrics, ...]
    backtest: Mapping[str, object]
    data_fingerprint: str
    diagnostic: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "rank": self.rank,
            "candidate_hash": self.candidate_hash,
            "human_readable_rules": self.human_readable_rules,
            "config": dict(self.config),
            "leaderboard_metrics": dict(self.leaderboard_metrics),
            "overall_metrics": dict(self.overall_metrics),
            "periods": {key: metrics.to_dict() for key, metrics in self.periods.items()},
            "annual_metrics": [metrics.to_dict() for metrics in self.annual_metrics],
            "backtest": dict(self.backtest),
            "data_fingerprint": self.data_fingerprint,
            "diagnostic": self.diagnostic,
        }


@dataclass(frozen=True)
class StrategyReplayResult:
    experiment_id: str
    leaderboard: ReplayLeaderboard
    data_start: str
    data_end: str
    data_fingerprint: str
    source_data_fingerprint: str
    data_changed: bool
    candidate_count: int
    candidates: tuple[StrategyReplayCandidate, ...]
    diagnostics: Mapping[str, str] = field(default_factory=dict)
    replay_id: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "experiment_id": self.experiment_id,
            "leaderboard": self.leaderboard,
            "data_start": self.data_start,
            "data_end": self.data_end,
            "data_fingerprint": self.data_fingerprint,
            "source_data_fingerprint": self.source_data_fingerprint,
            "data_changed": self.data_changed,
            "candidate_count": self.candidate_count,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "diagnostics": dict(self.diagnostics),
            "replay_id": self.replay_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
        }


@dataclass(frozen=True)
class StrategyReplayJobState:
    """Serializable status snapshot for one asynchronous replay job."""

    job_id: str
    status: ReplayJobStatus
    progress: float
    total_candidates: int
    total_scanned: int
    errors: int
    skipped: int
    error: str | None = None
    result: Mapping[str, object] | None = None
    phase: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "progress": self.progress,
            "total_candidates": self.total_candidates,
            "total_scanned": self.total_scanned,
            "errors": self.errors,
            "skipped": self.skipped,
            "error": self.error,
            "result": dict(self.result) if self.result is not None else None,
            "phase": self.phase,
            "metadata": dict(self.metadata),
        }


def build_period_windows(start_date: date, end_date: date) -> tuple[PeriodWindow, ...]:
    """Build YTD, natural rolling-year, and calendar-year windows."""

    if start_date > end_date:
        raise ValueError("全量回测开始日期不能晚于结束日期")
    ytd_start = max(start_date, date(end_date.year, 1, 1))
    one_year_start = max(start_date, _subtract_years(end_date, 1))
    two_year_start = max(start_date, _subtract_years(end_date, 2))
    windows = [
        PeriodWindow(
            "ytd",
            "今年以来",
            ytd_start,
            end_date,
            ytd_start != date(end_date.year, 1, 1) or end_date != date(end_date.year, 12, 31),
        ),
        PeriodWindow(
            "one_year", "最近一年", one_year_start, end_date, one_year_start == start_date
        ),
        PeriodWindow(
            "two_year", "最近两年", two_year_start, end_date, two_year_start == start_date
        ),
    ]
    for year in range(start_date.year, end_date.year + 1):
        year_start = max(start_date, date(year, 1, 1))
        year_end = min(end_date, date(year, 12, 31))
        windows.append(
            PeriodWindow(
                f"year_{year}",
                str(year),
                year_start,
                year_end,
                year_start != date(year, 1, 1) or year_end != date(year, 12, 31),
            )
        )
    return tuple(windows)


def calculate_period_metrics(
    equity_curve: Sequence[Mapping[str, object]],
    trades: Sequence[Mapping[str, object]],
    *,
    key: str,
    label: str,
    start_date: date,
    end_date: date,
    partial: bool = False,
) -> StrategyPeriodMetrics:
    """Calculate one period from a continuous full-range equity curve."""

    equity_rows = _equity_rows(equity_curve, start_date, end_date)
    trade_rows = _trade_rows(trades, start_date, end_date)
    actual_start = _date_text(equity_rows[0]["date"]) if equity_rows else ""
    actual_end = _date_text(equity_rows[-1]["date"]) if equity_rows else ""
    if len(equity_rows) < 2:
        return StrategyPeriodMetrics(
            key=key,
            label=label,
            start_date=actual_start,
            end_date=actual_end,
            bars=len(equity_rows),
            total_return=None,
            annual_return=None,
            max_drawdown=None,
            sharpe=None,
            total_trades=_sell_count(trade_rows),
            win_rate=None,
            diagnostic="样本不足：至少需要 2 个资金曲线点",
            partial=partial,
        )

    equity_frame = pd.DataFrame(equity_rows)
    trade_frame = pd.DataFrame(trade_rows)
    if not trade_frame.empty:
        trade_frame["datetime"] = pd.to_datetime(trade_frame["date"], errors="coerce")
    performance = PerformanceAnalyzer(equity_frame, trade_frame).compute()
    return StrategyPeriodMetrics(
        key=key,
        label=label,
        start_date=actual_start,
        end_date=actual_end,
        bars=len(equity_rows),
        total_return=_finite(performance.get("total_return")),
        annual_return=_finite(performance.get("annual_return")),
        max_drawdown=_finite(performance.get("max_drawdown")),
        sharpe=_finite(performance.get("sharpe")),
        total_trades=int(round(performance.get("total_trades", 0.0))),
        win_rate=_finite(performance.get("win_rate")),
        partial=partial,
    )


def build_replay_candidate(
    source: Mapping[str, object],
    full_result: Mapping[str, object],
    *,
    rank: int,
    start_date: date,
    end_date: date,
) -> StrategyReplayCandidate:
    """Combine a leaderboard candidate with its full-range report."""

    source_config = source.get("config")
    config = dict(source_config) if isinstance(source_config, Mapping) else {}
    config["start_date"] = start_date.isoformat()
    config["end_date"] = end_date.isoformat()
    source_metrics = source.get("metrics")
    leaderboard_metrics: Mapping[str, object] = {}
    if isinstance(source_metrics, Mapping):
        raw = source_metrics.get("validation")
        if isinstance(raw, Mapping):
            leaderboard_metrics = dict(raw)
    raw_performance = full_result.get("performance")
    overall_metrics: Mapping[str, object] = (
        dict(raw_performance) if isinstance(raw_performance, Mapping) else {}
    )
    raw_equity = full_result.get("equity_curve")
    equity_curve = raw_equity if isinstance(raw_equity, (list, tuple)) else []
    raw_trades = full_result.get("trades")
    trades = raw_trades if isinstance(raw_trades, (list, tuple)) else []
    overall_metrics = {
        **dict(overall_metrics),
        "turnover": _turnover(trades, config, overall_metrics),
        "trade_fingerprint": trade_fingerprint(trades),
    }
    periods = build_period_windows(start_date, end_date)
    period_metrics = {
        window.key: calculate_period_metrics(
            equity_curve,
            trades,
            key=window.key,
            label=window.label,
            start_date=window.start_date,
            end_date=window.end_date,
            partial=window.partial,
        )
        for window in periods
    }
    annual_metrics = tuple(
        period_metrics[window.key] for window in periods if window.key.startswith("year_")
    )
    diagnostic = full_result.get("diagnostic")
    backtest = dict(full_result)
    backtest["performance"] = dict(overall_metrics)
    return StrategyReplayCandidate(
        rank=rank,
        candidate_hash=str(source.get("candidate_hash") or ""),
        human_readable_rules=str(source.get("human_readable_rules") or ""),
        config=config,
        leaderboard_metrics=leaderboard_metrics,
        overall_metrics=overall_metrics,
        periods={key: period_metrics[key] for key in ("ytd", "one_year", "two_year")},
        annual_metrics=annual_metrics,
        backtest=backtest,
        data_fingerprint=str(full_result.get("data_fingerprint") or ""),
        diagnostic=str(diagnostic) if diagnostic else None,
    )


def _equity_rows(
    rows: Sequence[Mapping[str, object]], start_date: date, end_date: date
) -> list[dict[str, object]]:
    selected: list[dict[str, object]] = []
    for row in rows:
        current = _as_date(row.get("date"))
        total = _finite(row.get("total"))
        if current is None or total is None or not start_date <= current <= end_date:
            continue
        selected.append({**dict(row), "date": current.isoformat(), "total": total})
    selected.sort(key=lambda item: str(item["date"]))
    return selected


def _trade_rows(
    rows: Sequence[Mapping[str, object]], start_date: date, end_date: date
) -> list[dict[str, object]]:
    selected: list[dict[str, object]] = []
    for row in rows:
        current = _as_date(row.get("date"))
        if current is not None and start_date <= current <= end_date:
            selected.append({**dict(row), "date": current.isoformat()})
    return selected


def _sell_count(rows: Sequence[Mapping[str, object]]) -> int:
    return sum(1 for row in rows if str(row.get("direction") or "") == "SELL")


def _finite(value: object) -> float | None:
    if value is None:
        return None
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if "-" in text:
            return date.fromisoformat(text[:10])
        return datetime.strptime(text[:8], "%Y%m%d").date()
    except (TypeError, ValueError, OverflowError):
        return None


def _date_text(value: object) -> str:
    current = _as_date(value)
    return current.isoformat() if current is not None else ""


def _subtract_years(value: date, years: int) -> date:
    target_year = value.year - years
    try:
        return value.replace(year=target_year)
    except ValueError:
        return value.replace(year=target_year, day=28)


def _turnover(
    trades: Sequence[object],
    config: Mapping[str, object],
    performance: Mapping[str, object],
) -> float:
    initial_cash = _finite(config.get("initial_cash"))
    if initial_cash is None:
        initial_cash = _finite(performance.get("start_cash"))
    if initial_cash is None or initial_cash <= 0:
        return 0.0
    total_notional = 0.0
    for trade in trades:
        if not isinstance(trade, Mapping):
            continue
        size = _finite(trade.get("size")) or 0.0
        price = _finite(trade.get("price")) or 0.0
        total_notional += size * price
    return total_notional / initial_cash
