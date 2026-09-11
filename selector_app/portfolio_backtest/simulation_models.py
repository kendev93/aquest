"""Mutable and pending state used by the portfolio simulator."""

from __future__ import annotations

from dataclasses import dataclass

from selector_app.market_data.models import InstrumentBoard, InstrumentType


@dataclass
class _Position:
    market: str
    code: str
    instrument_type: InstrumentType
    board: InstrumentBoard
    size: float
    entry_price: float
    entry_date: int
    highest_price: float
    buy_commission: float
    buy_slippage: float
    name: str | None = None

    @property
    def symbol(self) -> str:
        return f"{self.market}{self.code}"


@dataclass(frozen=True)
class _PendingSell:
    signal_date: int
    reason: str


@dataclass(frozen=True)
class _PendingBuy:
    symbol: str
    signal_date: int
    budget: float
