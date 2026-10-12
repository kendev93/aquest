"""Compatibility exports for application-owned market-data adapters."""

from aquest.market_data.local_adapter import (
    LocalDayMarketDataAdapter,
    StockRef,
    is_supported_a_stock,
    suggested_vipdoc_path,
)

from .market_data_sync import MarketDataSync
from .market_sync import (
    InstrumentDiscoveryReport,
    MarketSyncConfig,
    MarketSyncReport,
)

__all__ = [
    "MarketSyncConfig",
    "MarketSyncReport",
    "InstrumentDiscoveryReport",
    "StockRef",
    "LocalDayMarketDataAdapter",
    "is_supported_a_stock",
    "suggested_vipdoc_path",
    "MarketDataSync",
]
