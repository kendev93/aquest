"""Trade attribution: which indicators separated the winners from the losers.

Unlike the factor test (#5), which asks whether a value predicts forward
returns across the market, this study starts from the trades one backtest
already made and asks a narrower, post-hoc question: on the day the entry
signal fired, what did the trades that made money look like compared with the
ones that lost? It explains; it does not forecast.
"""

from .models import AttributionFactor, FactorAttributionConfig
from .service import BOUNDARIES, TRADABLE_TYPES, FactorAttributionService
from .trades import RoundTrip, TradeSample, pair_round_trips

__all__ = [
    "AttributionFactor",
    "BOUNDARIES",
    "FactorAttributionConfig",
    "FactorAttributionService",
    "RoundTrip",
    "TRADABLE_TYPES",
    "TradeSample",
    "pair_round_trips",
]
