"""Virtual trading profiles for the supported daily-bar instrument types."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

InstrumentType = Literal["stock", "fund", "index", "bond"]


@dataclass(frozen=True)
class TradeProfile:
    instrument_type: InstrumentType
    lot_size: int
    commission: float
    min_commission: float
    stamp_tax: float
    virtual: bool = True


_DEFAULT_COMMISSION = 0.0001
_DEFAULT_MIN_COMMISSION = 0.1
_STOCK = TradeProfile("stock", 100, _DEFAULT_COMMISSION, _DEFAULT_MIN_COMMISSION, 0.001)
_FUND_LIKE = TradeProfile("fund", 100, _DEFAULT_COMMISSION, _DEFAULT_MIN_COMMISSION, 0.0)
STOCK_DEFAULT_COMMISSION = _STOCK.commission
STOCK_DEFAULT_MIN_COMMISSION = _STOCK.min_commission
STOCK_DEFAULT_STAMP_TAX = _STOCK.stamp_tax


def profile_for(
    instrument_type: InstrumentType,
    *,
    commission: float | None = None,
    min_commission: float | None = None,
    stamp_tax: float | None = None,
) -> TradeProfile:
    """Return a profile, applying explicit cost overrides when provided."""

    base = (
        _STOCK
        if instrument_type == "stock"
        else replace(_FUND_LIKE, instrument_type=instrument_type)
    )
    return replace(
        base,
        commission=base.commission if commission is None else commission,
        min_commission=base.min_commission if min_commission is None else min_commission,
        stamp_tax=base.stamp_tax if stamp_tax is None else stamp_tax,
    )


def profile_from_config(
    instrument_type: InstrumentType,
    *,
    commission: float,
    min_commission: float,
    stamp_tax: float,
) -> TradeProfile:
    """Resolve legacy request defaults into the selected type profile.

    Existing callers that explicitly pass non-default costs keep those costs.
    A non-stock request that carries the old stock defaults receives the
    fund-like defaults selected by the application policy.
    """

    if (
        instrument_type != "stock"
        and commission == STOCK_DEFAULT_COMMISSION
        and min_commission == STOCK_DEFAULT_MIN_COMMISSION
        and stamp_tax == STOCK_DEFAULT_STAMP_TAX
    ):
        return profile_for(instrument_type)
    return profile_for(
        instrument_type,
        commission=commission,
        min_commission=min_commission,
        stamp_tax=stamp_tax,
    )
