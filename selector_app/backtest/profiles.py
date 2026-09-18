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


_STOCK_COMMISSION = 0.0001
_FUND_COMMISSION = 0.00005
_DEFAULT_MIN_COMMISSION = 0.1
_STOCK = TradeProfile("stock", 100, _STOCK_COMMISSION, _DEFAULT_MIN_COMMISSION, 0.001)
_FUND_LIKE = TradeProfile("fund", 100, _FUND_COMMISSION, _DEFAULT_MIN_COMMISSION, 0.0)
STOCK_DEFAULT_COMMISSION = _STOCK.commission
STOCK_DEFAULT_MIN_COMMISSION = _STOCK.min_commission
STOCK_DEFAULT_STAMP_TAX = _STOCK.stamp_tax
FUND_DEFAULT_COMMISSION = _FUND_LIKE.commission
FUND_DEFAULT_MIN_COMMISSION = _FUND_LIKE.min_commission
FUND_DEFAULT_STAMP_TAX = _FUND_LIKE.stamp_tax


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

    Kept as the single entry point for callers, and now identical to
    :func:`profile_from_overrides`: costs equal to the legacy stock default are
    treated as untouched and fall back to the instrument default **per field**.
    The old behaviour required the whole stock default triple to be present,
    so an ETF run that only changed the commission kept the 0.1% stock stamp
    tax - a 20x overcharge on every exit, and inconsistent with the research
    path, which already resolved costs field by field.
    """

    return profile_from_overrides(
        instrument_type,
        commission=commission,
        min_commission=min_commission,
        stamp_tax=stamp_tax,
    )


def profile_from_overrides(
    instrument_type: InstrumentType,
    *,
    commission: float,
    min_commission: float,
    stamp_tax: float,
) -> TradeProfile:
    """Resolve legacy request defaults one field at a time.

    Unlike :func:`profile_from_config` this does not require the whole stock
    default triple to be present. A field still equal to the legacy stock
    default counts as untouched and falls back to the instrument default, so
    an ETF study that only clears the stamp tax keeps the fund commission
    instead of silently reverting to the stock rate.
    """

    return profile_for(
        instrument_type,
        commission=None if commission == STOCK_DEFAULT_COMMISSION else commission,
        min_commission=None if min_commission == STOCK_DEFAULT_MIN_COMMISSION else min_commission,
        stamp_tax=None if stamp_tax == STOCK_DEFAULT_STAMP_TAX else stamp_tax,
    )
