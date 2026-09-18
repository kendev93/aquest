"""Shared types for portfolio backtest service mixins."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

import pandas as pd

from selector_app.formulas.registry import FormulaRegistry
from selector_app.indicators.cache import IndicatorCache
from selector_app.market_data.models import (
    InstrumentBoard,
    InstrumentType,
    MarketCode,
    StockRef,
)


class PortfolioMarketDataAdapter(Protocol):
    def list_stock_refs(
        self,
        vipdoc_path: str | Path,
        universe: str,
        universe_file: str | Path | None = None,
        instrument_types: tuple[InstrumentType, ...] | None = None,
        boards: tuple[InstrumentBoard, ...] | None = None,
    ) -> list[StockRef]: ...

    def stock_ref(self, vipdoc_path: str | Path, market: MarketCode, code: str) -> StockRef: ...

    def read_stock(self, ref: StockRef) -> pd.DataFrame: ...


class _PortfolioServiceState:
    """Attributes shared by the context, simulation, and report mixins."""

    _adapter: PortfolioMarketDataAdapter
    _registry: FormulaRegistry
    _indicator_cache: IndicatorCache | None
