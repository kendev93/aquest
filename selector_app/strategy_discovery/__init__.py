"""Automatic, reproducible strategy discovery for local portfolio backtests."""

from .candidates import build_random_candidate, candidate_hash, normalize_candidate_config
from .catalog import IndicatorCatalog, default_indicator_catalog
from .models import CandidateEvaluation, DiscoveryRequest, DiscoveryRun, StrategyCandidate
from .replay import (
    PeriodWindow,
    StrategyPeriodMetrics,
    StrategyReplayCandidate,
    StrategyReplayJobState,
    StrategyReplayResult,
)
from .search import HybridSearchEngine, SearchResult

__all__ = [
    "CandidateEvaluation",
    "DiscoveryRequest",
    "DiscoveryRun",
    "HybridSearchEngine",
    "IndicatorCatalog",
    "SearchResult",
    "StrategyCandidate",
    "PeriodWindow",
    "StrategyPeriodMetrics",
    "StrategyReplayCandidate",
    "StrategyReplayJobState",
    "StrategyReplayResult",
    "build_random_candidate",
    "candidate_hash",
    "normalize_candidate_config",
    "default_indicator_catalog",
]
