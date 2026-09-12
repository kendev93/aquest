"""Compatibility exports for application-owned market-data adapters."""

from selector_app.market_data.local_adapter import (
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
    TdxMarketSync,
)

__all__ = [
    "MarketSyncConfig",
    "MarketSyncReport",
    "InstrumentDiscoveryReport",
    "StockRef",
    "LocalDayMarketDataAdapter",
    "is_supported_a_stock",
    "suggested_vipdoc_path",
    "TdxMarketSync",
    "MarketDataSync",
]
