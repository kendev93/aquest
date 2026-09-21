"""Cross-sectional factor testing on forward returns.

Condition research scores a ranking value on the days a rule already fired,
and swing research scores one against reversal events. This module asks the
plain question behind both of them: does the value rank stocks by the return
they are about to earn, on every day and for every stock?

The panel is unconditional - no rule has to fire first - which is what makes
it a factor test rather than a search for a good trade.
"""

from __future__ import annotations

from .metrics import (
    FactorEvidence,
    LayerStat,
    LongShortStat,
    benjamini_hochberg,
    evaluate_factor,
)
from .models import DEFAULT_HORIZON, MIN_CROSS_SECTION, FactorTestConfig
from .service import FactorTestService

__all__ = [
    "DEFAULT_HORIZON",
    "FactorEvidence",
    "FactorTestConfig",
    "FactorTestService",
    "LayerStat",
    "LongShortStat",
    "MIN_CROSS_SECTION",
    "benjamini_hochberg",
    "evaluate_factor",
]
