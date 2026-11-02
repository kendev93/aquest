"""Shared predicates for interpreting portfolio and single-security trade rows."""

from __future__ import annotations

from collections.abc import Iterable, Mapping


def count_closed_trades(trades: Iterable[object]) -> int:
    """Count executed sells; a rejected sell order never closed a position."""

    return sum(
        1
        for trade in trades
        if isinstance(trade, Mapping)
        and str(trade.get("direction") or "") == "SELL"
        and not bool(trade.get("rejected", False))
    )
