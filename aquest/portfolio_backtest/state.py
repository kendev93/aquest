"""Shared types for portfolio backtest service mixins."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

import pandas as pd

from aquest.formulas.registry import FormulaRegistry
from aquest.indicators.cache import IndicatorCache
from aquest.market_data.adjustment import CorporateAction
from aquest.market_data.models import (
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
    #: 公司行动缓存。每一轮 run 开头清空：补录分红事件后，长驻进程里旧缓存会让复权
    #: 漏事件，且 ``adjust_version`` 跟着错，指标缓存 key 也失效不了。
    _corporate_action_cache: dict[tuple[str, str], tuple[CorporateAction, ...]]
