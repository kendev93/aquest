"""Storage port used by market-data consumers."""

from __future__ import annotations

from datetime import date
from typing import Protocol

import pandas as pd

from .models import DataStoreStatus, InstrumentBoard, InstrumentRef, InstrumentType


class MarketDataRepository(Protocol):
    """The small application-owned read/write contract for market data."""

    def list_instruments(
        self,
        *,
        market: str | None = None,
        keyword: str = "",
        include_missing: bool = False,
        instrument_types: list[InstrumentType] | None = None,
        boards: list[InstrumentBoard] | None = None,
    ) -> list[InstrumentRef]: ...

    def read_bars(
        self,
        market: str,
        code: str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        include_provisional: bool = False,
    ) -> pd.DataFrame: ...

    def read_many_bars(
        self,
        instruments: list[InstrumentRef] | None = None,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        include_provisional: bool = False,
    ) -> pd.DataFrame: ...

    def get_store_status(self) -> DataStoreStatus: ...
