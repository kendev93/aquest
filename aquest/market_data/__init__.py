"""Local market-data browsing and DuckDB storage services.

The package exposes a flat compatibility facade for embedders, but imports are
resolved lazily: importing ``aquest.market_data`` for one symbol must not pull
in the maintenance scheduler, local import service and every adapter.
"""

from __future__ import annotations

from importlib import import_module

_EXPORTS: dict[str, str] = {
    "BatchMarketDataAdapter": "aquest.market_data.adapter",
    "DuckDbMarketDataAdapter": "aquest.market_data.adapter",
    "MarketDataAdapter": "aquest.market_data.adapter",
    "DayFileError": "aquest.market_data.day_format",
    "DayFileResult": "aquest.market_data.day_format",
    "classify_board": "aquest.market_data.day_format",
    "classify_instrument": "aquest.market_data.day_format",
    "discover_day_files": "aquest.market_data.day_format",
    "read_day_file": "aquest.market_data.day_format",
    "LocalDayMarketDataAdapter": "aquest.market_data.local_adapter",
    "suggested_vipdoc_path": "aquest.market_data.local_adapter",
    "LocalDayImportCancelled": "aquest.market_data.local_import",
    "LocalDayImportError": "aquest.market_data.local_import",
    "LocalDayImportService": "aquest.market_data.local_import",
    "BACKUP_RETENTION_DAYS": "aquest.market_data.maintenance",
    "COMPACTION_GROWTH_RATIO": "aquest.market_data.maintenance",
    "COMPACTION_MIN_SIZE_BYTES": "aquest.market_data.maintenance",
    "DatabaseMaintenanceScheduler": "aquest.market_data.maintenance",
    "DatabaseMaintenanceService": "aquest.market_data.maintenance",
    "MaintenanceResult": "aquest.market_data.maintenance",
    "DataStoreStatus": "aquest.market_data.models",
    "InstrumentBoard": "aquest.market_data.models",
    "InstrumentRef": "aquest.market_data.models",
    "InstrumentType": "aquest.market_data.models",
    "MarketCode": "aquest.market_data.models",
    "MarketDataProvider": "aquest.market_data.models",
    "ResolvedMarketDataProvider": "aquest.market_data.models",
    "StockRef": "aquest.market_data.models",
    "MarketDataRepository": "aquest.market_data.repository",
    "InstrumentScope": "aquest.market_data.scope",
    "ChartPeriod": "aquest.market_data.service",
    "LocalInstrumentPage": "aquest.market_data.service",
    "LocalMarketChart": "aquest.market_data.service",
    "LocalMarketDataService": "aquest.market_data.service",
    "DuckDbMarketDataStore": "aquest.market_data.store",
    "MarketDataStoreError": "aquest.market_data.store",
    "default_data_dir": "aquest.market_data.store",
    "default_database_path": "aquest.market_data.store",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> object:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
