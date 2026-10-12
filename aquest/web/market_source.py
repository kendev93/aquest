"""Resolve the authoritative market-data status for a request.

AQuest now reads bars only from its own DuckDB database, but endpoints
that derive a default date window must still ask the *same* object that will
serve those bars — the adapter — rather than assembling bounds from somewhere
else.  Keeping that question in one module is what made the previous
two-source setup safe to remove.
"""

from __future__ import annotations

from typing import cast

from fastapi import Request

from aquest.market_data.models import DataStoreStatus
from aquest.market_data.store import DuckDbMarketDataStore


def resolve_market_status(request: Request) -> DataStoreStatus:
    """Return the status of the data source this request will actually read.

    The adapter wins when it can report a status; the store is the fallback for
    adapters that cannot.
    """

    adapter = getattr(request.app.state, "market_data_adapter", None)
    status_reader = getattr(adapter, "status", None)
    if callable(status_reader):
        return cast(DataStoreStatus, status_reader())
    return cast(DuckDbMarketDataStore, request.app.state.market_data_store).status()


__all__ = ["resolve_market_status"]
