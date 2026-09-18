"""Swing (trend leg) segmentation and its distribution study."""

from __future__ import annotations

from .models import SwingOverviewConfig
from .overview import SwingAccumulator, SwingDistribution, SwingSummary
from .segments import SwingDirection, SwingSegment, atr_percent, split_swings
from .service import SwingResearchService

__all__ = [
    "SwingAccumulator",
    "SwingDirection",
    "SwingDistribution",
    "SwingOverviewConfig",
    "SwingResearchService",
    "SwingSegment",
    "SwingSummary",
    "atr_percent",
    "split_swings",
]
