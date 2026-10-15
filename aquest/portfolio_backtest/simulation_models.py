"""Mutable and pending state used by the portfolio simulator."""

from __future__ import annotations

from dataclasses import dataclass, field

from aquest.market_data.models import InstrumentBoard, InstrumentType


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
    applied_corporate_actions: set[str] = field(default_factory=set)

    @property
    def symbol(self) -> str:
        return f"{self.market}{self.code}"


@dataclass(frozen=True)
class _PendingSell:
    signal_date: int
    reason: str
    # 风控卖单与规则卖单同一套 T+1 撮合，但要留下出处：成交日才能补设冷却、
    # 交易行才能标 risk_trigger（规则卖出为 None）。
    risk_trigger: str | None = None
    cooldown_days: int = 0


@dataclass(frozen=True)
class _PendingBuy:
    symbol: str
    signal_date: int
    budget: float
