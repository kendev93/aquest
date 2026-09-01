"""Shared pytest configuration for isolated application tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def disable_automatic_market_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ordinary tests offline; scheduler tests opt in explicitly."""

    monkeypatch.setenv("SELECTOR_AUTO_SYNC_ENABLED", "0")
