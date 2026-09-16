"""Local market-data browsing and DuckDB storage services."""

from .adapter import BatchMarketDataAdapter, DuckDbMarketDataAdapter, MarketDataAdapter
from .day_format import (
    DayFileError,
    DayFileResult,
    classify_board,
    classify_instrument,
    discover_day_files,
    read_day_file,
)
from .local_adapter import LocalDayMarketDataAdapter, suggested_vipdoc_path
from .maintenance import (
    BACKUP_RETENTION_DAYS,
    COMPACTION_GROWTH_RATIO,
    COMPACTION_MIN_SIZE_BYTES,
    DatabaseMaintenanceScheduler,
    DatabaseMaintenanceService,
    MaintenanceResult,
)
from .models import (
    DataStoreStatus,
    InstrumentBoard,
    InstrumentRef,
    InstrumentType,
    MarketCode,
    MarketDataProvider,
    ResolvedMarketDataProvider,
    StockRef,
)
from .repository import MarketDataRepository
from .scope import InstrumentScope
from .service import (
    ChartPeriod,
    LocalInstrumentPage,
    LocalMarketChart,
    LocalMarketDataService,
)
from .store import (
    DuckDbMarketDataStore,
    MarketDataStoreError,
    default_data_dir,
    default_database_path,
)

__all__ = [
    "ChartPeriod",
    "LocalInstrumentPage",
    "LocalMarketChart",
    "LocalMarketDataService",
    "DataStoreStatus",
    "InstrumentRef",
    "StockRef",
    "InstrumentType",
    "InstrumentBoard",
    "MarketCode",
    "MarketDataProvider",
    "ResolvedMarketDataProvider",
    "DayFileError",
    "DayFileResult",
    "classify_instrument",
    "classify_board",
    "discover_day_files",
    "read_day_file",
    "DuckDbMarketDataAdapter",
    "BatchMarketDataAdapter",
    "MarketDataAdapter",
    "LocalDayMarketDataAdapter",
    "suggested_vipdoc_path",
    "BACKUP_RETENTION_DAYS",
    "COMPACTION_GROWTH_RATIO",
    "COMPACTION_MIN_SIZE_BYTES",
    "DatabaseMaintenanceScheduler",
    "DatabaseMaintenanceService",
    "MaintenanceResult",
    "DuckDbMarketDataStore",
    "MarketDataStoreError",
    "MarketDataRepository",
    "InstrumentScope",
    "default_data_dir",
    "default_database_path",
]
