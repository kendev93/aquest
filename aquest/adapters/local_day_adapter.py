"""Compatibility exports for the project-owned local market-data adapter."""

from aquest.market_data.adapter import MarketDataAdapter
from aquest.market_data.local_adapter import (
    LocalDayMarketDataAdapter,
    MarketCode,
    StockRef,
    is_supported_a_stock,
    is_supported_instrument,
    suggested_vipdoc_path,
)

__all__ = [
    "LocalDayMarketDataAdapter",
    "MarketCode",
    "MarketDataAdapter",
    "StockRef",
    "is_supported_a_stock",
    "is_supported_instrument",
    "suggested_vipdoc_path",
]
