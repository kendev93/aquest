"""The default date window must come from the source the bars are read from."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

from selector_app.market_data.models import DataStoreStatus
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.web.market_source import resolve_market_status


def _status(store_name: str, start: date, end: date) -> DataStoreStatus:
    return DataStoreStatus(
        database_path=store_name,
        schema_version=0,
        instrument_count=1,
        bar_count=10,
        data_start=start,
        data_end=end,
    )


class _Adapter:
    def __init__(self, status: DataStoreStatus) -> None:
        self.database_path = Path(status.database_path)
        self._status = status

    def status(self) -> DataStoreStatus:
        return self._status


def _request(store: object, adapter: object | None) -> SimpleNamespace:
    state = SimpleNamespace(market_data_store=store, market_data_adapter=adapter)
    return SimpleNamespace(app=SimpleNamespace(state=state))


def test_resolve_market_status_prefers_the_adapter(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    adapter_status = _status(str(tmp_path / "adapter.db"), date(1990, 12, 19), date(2026, 9, 11))

    resolved = resolve_market_status(_request(store, _Adapter(adapter_status)))

    assert resolved.data_end == date(2026, 9, 11)


def test_resolve_market_status_falls_back_to_the_local_store(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")

    resolved = resolve_market_status(_request(store, None))

    assert resolved.database_path == str(tmp_path / "market.duckdb")


def test_resolve_market_status_ignores_adapters_without_status(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")

    resolved = resolve_market_status(_request(store, SimpleNamespace()))

    assert resolved.database_path == str(tmp_path / "market.duckdb")
