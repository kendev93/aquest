"""Small, dependency-free helpers shared across domains."""

from __future__ import annotations


def error_reason(
    exc: BaseException,
    *,
    limit: int = 200,
    fallback: str | None = None,
) -> str:
    """A bounded, human-readable reason for a failed operation."""

    message = str(exc).strip()
    return message[:limit] or (fallback or type(exc).__name__)
