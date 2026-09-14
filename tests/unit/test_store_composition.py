from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.market_data.store_bars import DuckDbBarsStore
from selector_app.market_data.store_history import DuckDbHistoryStore
from selector_app.market_data.store_indicator_cache import DuckDbIndicatorCacheStore


def test_store_facade_is_composed_of_domain_repositories(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")

    assert isinstance(store._bars, DuckDbBarsStore)
    assert isinstance(store._history, DuckDbHistoryStore)
    assert isinstance(store._indicator_cache, DuckDbIndicatorCacheStore)
