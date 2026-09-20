"""Shared, opt-in market-data scope filters."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import cast

from .models import InstrumentBoard, InstrumentType, MarketCode

_ALLOWED_MARKETS = {"SH", "SZ"}
_ALLOWED_TYPES = {"stock"}
_ALLOWED_BOARDS = {"main", "star", "chinext", "b_share"}


@dataclass(frozen=True)
class InstrumentScope:
    """A filter where every ``None`` or empty dimension means unrestricted."""

    markets: tuple[MarketCode, ...] | None = None
    instrument_types: tuple[InstrumentType, ...] | None = None
    boards: tuple[InstrumentBoard, ...] | None = None

    @classmethod
    def from_values(
        cls,
        *,
        universe: str = "all",
        markets: Iterable[str] | None = None,
        instrument_types: Iterable[str] | None = None,
        boards: Iterable[str] | None = None,
    ) -> InstrumentScope:
        normalized_universe = universe.strip().lower()
        if normalized_universe in {"custom", "codes"}:
            normalized_universe = "all"
        if normalized_universe not in {"all", "sh", "sz"}:
            raise ValueError(f"不支持的行情范围: {universe}")
        selected_markets = (
            _normalize_markets(markets)
            if markets is not None
            else None
            if normalized_universe == "all"
            else ("SH",)
            if normalized_universe == "sh"
            else ("SZ",)
        )
        return cls(
            markets=selected_markets,
            instrument_types=_normalize_types(instrument_types),
            boards=_normalize_boards(boards),
        )

    def matches(self, market: str, instrument_type: str, board: str) -> bool:
        return (
            (self.markets is None or market.upper() in self.markets)
            and (self.instrument_types is None or instrument_type in self.instrument_types)
            and (self.boards is None or board in self.boards)
        )


def _normalize_markets(values: Iterable[str] | None) -> tuple[MarketCode, ...] | None:
    if values is None:
        return None
    normalized = tuple(str(value).strip().upper() for value in values)
    if not normalized:
        return None
    if len(set(normalized)) != len(normalized):
        raise ValueError("市场范围不能重复")
    invalid = sorted(set(normalized) - _ALLOWED_MARKETS)
    if invalid:
        raise ValueError(f"不支持的市场范围: {', '.join(invalid)}")
    return cast(tuple[MarketCode, ...], normalized)


def _normalize_types(values: Iterable[str] | None) -> tuple[InstrumentType, ...] | None:
    if values is None:
        return None
    normalized = tuple(str(value).strip().lower() for value in values)
    if not normalized:
        return None
    if len(set(normalized)) != len(normalized):
        raise ValueError("证券类型不能重复")
    invalid = sorted(set(normalized) - _ALLOWED_TYPES)
    if invalid:
        raise ValueError(f"不支持的证券类型: {', '.join(invalid)}")
    return cast(tuple[InstrumentType, ...], normalized)


def _normalize_boards(values: Iterable[str] | None) -> tuple[InstrumentBoard, ...] | None:
    if values is None:
        return None
    normalized = tuple(str(value).strip().lower() for value in values)
    if not normalized:
        return None
    if len(set(normalized)) != len(normalized):
        raise ValueError("板块范围不能重复")
    invalid = sorted(set(normalized) - _ALLOWED_BOARDS)
    if invalid:
        raise ValueError(f"不支持的板块范围: {', '.join(invalid)}")
    return cast(tuple[InstrumentBoard, ...], normalized)
