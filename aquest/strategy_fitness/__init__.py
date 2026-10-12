"""Batch time-split strategy fitness evaluation."""

from typing import TYPE_CHECKING

from .models import (
    FitnessPhaseMetrics,
    StrategyFitnessConfig,
    StrategyFitnessReport,
    StrategyFitnessResult,
)

if TYPE_CHECKING:
    from .service import StrategyFitnessService


def __getattr__(name: str) -> object:
    if name == "StrategyFitnessService":
        from .service import StrategyFitnessService

        return StrategyFitnessService
    raise AttributeError(name)


__all__ = [
    "FitnessPhaseMetrics",
    "StrategyFitnessConfig",
    "StrategyFitnessReport",
    "StrategyFitnessResult",
    "StrategyFitnessService",
]
