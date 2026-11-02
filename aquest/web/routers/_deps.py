"""Typed accessors for application services stored on ``app.state``."""

from __future__ import annotations

from typing import cast

from fastapi import Request

from aquest.market_data.store import DuckDbMarketDataStore
from aquest.screening.jobs import ScreenJobRunner


def market_store(request: Request) -> DuckDbMarketDataStore:
    return cast(DuckDbMarketDataStore, request.app.state.market_data_store)


def job_runner(request: Request) -> ScreenJobRunner:
    return cast(ScreenJobRunner, request.app.state.screen_job_runner)
