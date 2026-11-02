"""Small date parsers shared by storage, research and replay code."""

from __future__ import annotations

from datetime import date, datetime


def parse_iso_date(value: object) -> date | None:
    """Parse an ISO date/timestamp prefix, or pass an existing date through."""

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


def parse_store_date(value: object) -> date | None:
    """Parse either ``YYYY-MM-DD`` or the compact ``YYYYMMDD`` storage form."""

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if "-" in text:
            return date.fromisoformat(text[:10])
        return datetime.strptime(text[:8], "%Y%m%d").date()
    except (TypeError, ValueError, OverflowError):
        return None
