"""Shared pytest configuration for isolated application tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def disable_automatic_market_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ordinary tests offline; scheduler tests opt in explicitly."""

    monkeypatch.setenv("SELECTOR_AUTO_SYNC_ENABLED", "0")


@pytest.fixture(autouse=True)
def isolate_default_duckdb(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let app-factory tests initialize the user's home database."""

    monkeypatch.setenv("SELECTOR_DATA_DIR", str(tmp_path / "default-indicator-lab"))
