"""Immutable models for time-split strategy suitability reports."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from selector_app.portfolio_backtest.models import PortfolioBacktestConfig

FitnessLabel = Literal["strong", "watch", "weak", "insufficient"]


@dataclass(frozen=True)
class StrategyFitnessConfig:
    """Configuration for a single-stock, chronological suitability evaluation."""

    strategy: PortfolioBacktestConfig
    train_ratio: float = 0.6
    validation_ratio: float = 0.2
    min_trades: int = 5
    max_test_drawdown: float = 0.3

    def __post_init__(self) -> None:
        if self.strategy.max_positions != 1:
            raise ValueError("策略适配性评估必须使用单股票单槽位")
        if not math.isfinite(self.train_ratio) or self.train_ratio <= 0:
            raise ValueError("训练数据比例必须大于 0")
        if not math.isfinite(self.validation_ratio) or self.validation_ratio <= 0:
            raise ValueError("验证数据比例必须大于 0")
        if self.train_ratio + self.validation_ratio >= 1:
            raise ValueError("训练和验证比例之和必须小于 1")
        if isinstance(self.min_trades, bool) or not 1 <= self.min_trades <= 10_000:
            raise ValueError("最少成交笔数必须在 1 到 10000 之间")
        if (
            not math.isfinite(self.max_test_drawdown)
            or self.max_test_drawdown < 0
            or self.max_test_drawdown > 1
        ):
            raise ValueError("测试期最大回撤阈值必须在 0 到 1 之间")


@dataclass(frozen=True)
class FitnessPhaseMetrics:
    """Metrics for one chronological evaluation window."""

    name: str
    start_date: str
    end_date: str
    bars: int
    total_trades: int
    win_rate: float | None
    total_return: float | None
    annual_return: float | None
    max_drawdown: float | None
    sharpe: float | None
    profit_factor: float | None
    expectancy: float | None
    avg_holding_days: float | None
    diagnostic: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "bars": self.bars,
            "total_trades": self.total_trades,
            "win_rate": self.win_rate,
            "total_return": self.total_return,
            "annual_return": self.annual_return,
            "max_drawdown": self.max_drawdown,
            "sharpe": self.sharpe,
            "profit_factor": self.profit_factor,
            "expectancy": self.expectancy,
            "avg_holding_days": self.avg_holding_days,
            "diagnostic": self.diagnostic,
        }


@dataclass(frozen=True)
class StrategyFitnessResult:
    """Suitability result for one stock."""

    market: str
    code: str
    bars: int
    data_start: str
    data_end: str
    suitability_score: float
    passed: bool
    label: FitnessLabel
    passed_checks: int
    total_checks: int
    positive_periods: int
    checks: tuple[Mapping[str, object], ...]
    train: FitnessPhaseMetrics
    validation: FitnessPhaseMetrics
    test: FitnessPhaseMetrics
    failure_reason: str | None = None
    data_fingerprint: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "checks", tuple(MappingProxyType(dict(check)) for check in self.checks)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "market": self.market,
            "code": self.code,
            "bars": self.bars,
            "data_start": self.data_start,
            "data_end": self.data_end,
            "suitability_score": self.suitability_score,
            "passed": self.passed,
            "label": self.label,
            "passed_checks": self.passed_checks,
            "total_checks": self.total_checks,
            "positive_periods": self.positive_periods,
            "checks": [dict(check) for check in self.checks],
            "train": self.train.to_dict(),
            "validation": self.validation.to_dict(),
            "test": self.test.to_dict(),
            "failure_reason": self.failure_reason,
            "data_fingerprint": self.data_fingerprint,
        }


@dataclass(frozen=True)
class StrategyFitnessReport:
    """JSON-ready batch suitability report."""

    universe: str
    total_candidates: int
    processed: int
    skipped: int
    errors: int
    bars: int
    start_date: str
    end_date: str
    train_end_date: str
    validation_end_date: str
    ranking_value: str
    train_ratio: float
    validation_ratio: float
    min_trades: int
    max_test_drawdown: float
    results: tuple[StrategyFitnessResult, ...]
    failure_reasons: Mapping[str, int]
    diagnostic: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "failure_reasons", MappingProxyType(dict(self.failure_reasons)))

    def to_dict(self) -> dict[str, object]:
        return {
            "universe": self.universe,
            "total_candidates": self.total_candidates,
            "processed": self.processed,
            "skipped": self.skipped,
            "errors": self.errors,
            "bars": self.bars,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "train_end_date": self.train_end_date,
            "validation_end_date": self.validation_end_date,
            "ranking_value": self.ranking_value,
            "train_ratio": self.train_ratio,
            "validation_ratio": self.validation_ratio,
            "min_trades": self.min_trades,
            "max_test_drawdown": self.max_test_drawdown,
            "results": [result.to_dict() for result in self.results],
            "failure_reasons": dict(self.failure_reasons),
            "diagnostic": self.diagnostic,
        }
