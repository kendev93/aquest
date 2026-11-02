"""HTTP translation for storage failures."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import HTTPException, status

from aquest.market_data.store import MarketDataStoreError


@contextmanager
def store_guard(detail: str, *, include_runtime: bool = False) -> Iterator[None]:
    """Map market-store/IO failures to the API's 503 response.

    Some routes also wrap ``RuntimeError`` (service/submit failures) in the same
    503; callers opt in explicitly so unrelated programming errors stay visible.
    """

    errors: tuple[type[BaseException], ...] = (MarketDataStoreError, OSError)
    if include_runtime:
        errors = (*errors, RuntimeError)
    try:
        yield
    except errors as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail) from exc
