"""Pair execution trades into round trips.

A backtest leaves a flat list of BUY / SELL rows behind. Attribution needs one
row per *trade* - an entry and the exit that closed it - because "did this trade
win" is a property of the pair, not of either leg.

Two rules keep the sample honest:

* **FIFO**, the same pairing the portfolio report already uses to average
  holding periods. Any other pairing invents trades that never happened.
* **Open positions are not trades.** A position still held on the last day has
  no realised P&L, and marking it to market would quietly turn unrealised gains
  into "wins" that the strategy never actually banked.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime

RoundTripKey = tuple[date, int]


def _parse_day(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


@dataclass(frozen=True)
class RoundTrip:
    """One closed trade: where the signal fired and what it returned."""

    key: str
    market: str
    code: str
    name: str
    #: Day the entry signal fired - the day whose indicators get measured.
    signal_date: date
    entry_date: date
    exit_date: date
    #: ``pnl / cost_basis``: net of commission, stamp tax and slippage.
    net_return: float
    win: bool
    #: Trading days held, filled in once the stock's calendar is in hand.
    holding_days: int = 0

    def with_holding_days(self, days: int) -> RoundTrip:
        return replace(self, holding_days=days)


@dataclass(frozen=True)
class TradeSample:
    """Closed trades plus everything that was dropped, with a count for each."""

    trips: tuple[RoundTrip, ...]
    open_positions: int = 0
    orphan_sells: int = 0
    #: Trades the archived result flagged as unfillable. No engine sets the flag
    #: today; the count is kept so older records still report what they dropped.
    rejected: int = 0
    unparsable: int = 0

    @property
    def total(self) -> int:
        return len(self.trips)

    @property
    def wins(self) -> int:
        return sum(1 for trip in self.trips if trip.win)

    @property
    def losses(self) -> int:
        return sum(1 for trip in self.trips if not trip.win)

    @property
    def signal_days(self) -> int:
        return len({trip.signal_date for trip in self.trips})

    @property
    def stocks(self) -> int:
        return len({trip.key for trip in self.trips})


def pair_round_trips(trades: Iterable[Mapping[str, object]]) -> TradeSample:
    """Group trades by instrument and close them first-in-first-out."""

    grouped: dict[str, list[tuple[RoundTripKey, Mapping[str, object]]]] = defaultdict(list)
    rejected = 0
    unparsable = 0
    for index, trade in enumerate(trades):
        if bool(trade.get("rejected", False)):
            rejected += 1
            continue
        direction = str(trade.get("direction", "")).strip().upper()
        day = _parse_day(trade.get("date"))
        market = str(trade.get("market", "")).strip()
        code = str(trade.get("code", "")).strip()
        if direction not in ("BUY", "SELL") or day is None or not code:
            unparsable += 1
            continue
        grouped[f"{market}{code}"].append(((day, index), trade))

    trips: list[RoundTrip] = []
    open_positions = 0
    orphan_sells = 0
    for key, items in grouped.items():
        items.sort(key=lambda item: item[0])
        pending: deque[tuple[date, Mapping[str, object]]] = deque()
        market = str(items[0][1].get("market", "")).strip()
        code = str(items[0][1].get("code", "")).strip()
        name = str(items[0][1].get("name", "")).strip()
        for (day, _index), trade in items:
            if str(trade.get("direction", "")).strip().upper() == "BUY":
                pending.append((day, trade))
                continue
            if not pending:
                orphan_sells += 1
                continue
            entry_day, entry = pending.popleft()
            pnl = _number(trade.get("pnl"))
            cost_basis = _number(entry.get("cost_basis"))
            if pnl is None or cost_basis is None or cost_basis <= 0:
                unparsable += 1
                continue
            signal = _parse_day(entry.get("signal_date")) or entry_day
            trips.append(
                RoundTrip(
                    key=key,
                    market=market,
                    code=code,
                    name=name,
                    signal_date=signal,
                    entry_date=entry_day,
                    exit_date=day,
                    net_return=pnl / cost_basis,
                    win=pnl > 0,
                )
            )
        open_positions += len(pending)

    # Sorted by signal day so that "adjacent trades" means "adjacent days":
    # the permutation moves contiguous blocks of positions and only means
    # something if position order tracks calendar order.
    trips.sort(key=lambda trip: (trip.signal_date, trip.key, trip.entry_date, trip.exit_date))
    return TradeSample(
        trips=tuple(trips),
        open_positions=open_positions,
        orphan_sells=orphan_sells,
        rejected=rejected,
        unparsable=unparsable,
    )


__all__ = ["RoundTrip", "TradeSample", "pair_round_trips"]
