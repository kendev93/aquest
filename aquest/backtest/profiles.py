"""Virtual trading profiles for the supported daily-bar instrument types."""

from __future__ import annotations

from dataclasses import dataclass, replace

from aquest.market_data.models import InstrumentType


@dataclass(frozen=True)
class TradeProfile:
    instrument_type: InstrumentType
    lot_size: int
    commission: float
    min_commission: float
    stamp_tax: float
    virtual: bool = True


_STOCK_COMMISSION = 0.0001
_DEFAULT_MIN_COMMISSION = 0.1
_STOCK = TradeProfile("stock", 100, _STOCK_COMMISSION, _DEFAULT_MIN_COMMISSION, 0.001)
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

    del instrument_type
    return replace(
        _STOCK,
        commission=_STOCK.commission if commission is None else commission,
        min_commission=_STOCK.min_commission if min_commission is None else min_commission,
        stamp_tax=_STOCK.stamp_tax if stamp_tax is None else stamp_tax,
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
    The old behaviour required the whole stock default triple to be present.
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
    default counts as untouched and falls back to the instrument default.
    """

    return profile_for(
        instrument_type,
        commission=None if commission == STOCK_DEFAULT_COMMISSION else commission,
        min_commission=None if min_commission == STOCK_DEFAULT_MIN_COMMISSION else min_commission,
        stamp_tax=None if stamp_tax == STOCK_DEFAULT_STAMP_TAX else stamp_tax,
    )
