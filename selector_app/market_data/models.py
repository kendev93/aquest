"""Application-owned market-data contracts and reports."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Literal

MarketCode = Literal["SH", "SZ"]
InstrumentType = Literal["stock", "fund", "index", "bond"]
InstrumentBoard = Literal["main", "star", "chinext", "b_share", "fund", "index", "bond"]
DataSource = Literal["local", "online"]
MarketDataProvider = Literal["akshare", "tdx"]
ResolvedMarketDataProvider = Literal["akshare", "tdx", "mixed"]
BarStatus = Literal["completed", "provisional"]


@dataclass(frozen=True)
class InstrumentRef:
    """Stable identity for one instrument in the local data store."""

    market: MarketCode
    code: str
    instrument_type: InstrumentType
    source_path: Path | None = None
    board: InstrumentBoard = "main"
    name: str | None = None
    provider: str | None = None

    @property
    def symbol(self) -> str:
        return f"{self.market}{self.code}"

    @property
    def path(self) -> Path:
        """Compatibility path for services that only need an identity object."""

        return self.source_path or Path()


@dataclass(frozen=True)
class StockRef:
    """Compatibility identity used by the original service protocols."""

    market: MarketCode
    code: str
    path: Path
    instrument_type: InstrumentType = "stock"
    board: InstrumentBoard = "main"
    name: str | None = None
    provider: str | None = None

    @property
    def symbol(self) -> str:
        return f"{self.market}{self.code}"


@dataclass(frozen=True)
class DataStoreStatus:
    """Small status snapshot safe to expose from the local API."""

    database_path: str
    schema_version: int
    instrument_count: int
    bar_count: int
    data_start: str | None
    data_end: str | None
    last_local_import_at: str | None = None
    last_online_sync_at: str | None = None
    last_online_provider: str | None = None
    last_online_provider_version: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "database_path": self.database_path,
            "schema_version": self.schema_version,
            "instrument_count": self.instrument_count,
            "bar_count": self.bar_count,
            "data_start": self.data_start,
            "data_end": self.data_end,
            "last_local_import_at": self.last_local_import_at,
            "last_online_sync_at": self.last_online_sync_at,
            "last_online_provider": self.last_online_provider,
            "last_online_provider_version": self.last_online_provider_version,
        }


@dataclass(frozen=True)
class ImportReport:
    """Result of one local or online market-data ingestion run."""

    source: DataSource
    discovered_files: int = 0
    imported_files: int = 0
    updated_files: int = 0
    unchanged_files: int = 0
    skipped_files: int = 0
    filtered_files: int = 0
    missing_files: int = 0
    imported_instruments: int = 0
    replaced_instruments: int = 0
    imported_bars: int = 0
    provisional_bars: int = 0
    errors: int = 0
    failure_reasons: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "failure_reasons", MappingProxyType(dict(self.failure_reasons)))

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "discovered_files": self.discovered_files,
            "imported_files": self.imported_files,
            "updated_files": self.updated_files,
            "unchanged_files": self.unchanged_files,
            "skipped_files": self.skipped_files,
            "filtered_files": self.filtered_files,
            "missing_files": self.missing_files,
            "imported_instruments": self.imported_instruments,
            "replaced_instruments": self.replaced_instruments,
            "imported_bars": self.imported_bars,
            "provisional_bars": self.provisional_bars,
            "errors": self.errors,
            "failure_reasons": dict(self.failure_reasons),
        }
