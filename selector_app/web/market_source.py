"""Resolve the authoritative market-data status for a request.

Indicator Lab can read bars from two places: the project-owned DuckDB sidecar
(``market.duckdb``) or the shared ETFQuest database.  Only one of them holds the
bars that a request will actually read, so any endpoint that derives a default
date window must ask the *same* source it is about to read from.

Reading the sidecar while the bars come from ETFQuest silently produces a stale
window: the sidecar is a legacy copy that stops advancing once shared mode is
enabled, so "最近五年" would be computed against an old ``data_end``.
"""

from __future__ import annotations

from typing import cast

from fastapi import Request

from selector_app.market_data.models import DataStoreStatus
from selector_app.market_data.store import DuckDbMarketDataStore


def resolve_market_status(request: Request) -> DataStoreStatus:
    """Return the status of the data source this request will actually read.

    The market-data adapter wins when it can report a status; only then does the
    local sidecar act as the fallback for deployments that still read it.
    """

    adapter = getattr(request.app.state, "market_data_adapter", None)
    status_reader = getattr(adapter, "status", None)
    if callable(status_reader):
        return cast(DataStoreStatus, status_reader())
    return cast(DuckDbMarketDataStore, request.app.state.market_data_store).status()


__all__ = ["resolve_market_status"]
