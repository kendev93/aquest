"""Application-owned market-data contracts and reports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

MarketCode = Literal["SH", "SZ"]
InstrumentType = Literal["stock", "fund", "index", "bond"]
InstrumentBoard = Literal["main", "star", "chinext", "b_share", "fund", "index", "bond"]
DataSource = Literal["local", "online"]
# "tencent" / "baostock" are upstream channel ids used for per-source rate-limit
# accounting and catalog provenance. They are not accepted as
# ``MarketSyncConfig.provider`` — the online sync channel is still configured as a
# whole and picks among these upstreams internally (see
# docs/datasource-survey-2026-09-20.md section VII).
MarketDataProvider = Literal["eastmoney", "tdx", "tencent", "baostock"]
ResolvedMarketDataProvider = Literal["eastmoney", "tdx", "tencent", "baostock", "mixed"]
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
    bar_count: int | None = None
    first_date: date | None = None
    last_date: date | None = None
    last_close: float | None = None

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
    bar_count: int | None = None
    first_date: date | None = None
    last_date: date | None = None
    last_close: float | None = None

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
            "last_online_sync_at": self.last_online_sync_at,
            "last_online_provider": self.last_online_provider,
            "last_online_provider_version": self.last_online_provider_version,
        }
