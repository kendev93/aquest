from selector_app.market_data.store import (
    DUCKDB_INDICATOR_CACHE,
    SQLITE_INDICATOR_CACHE,
    DuckDbMarketDataStore,
)
from selector_app.market_data.store_bars import DuckDbBarsStore
from selector_app.market_data.store_history import DuckDbHistoryStore
from selector_app.market_data.store_indicator_cache import DuckDbIndicatorCacheStore
from selector_app.market_data.store_indicator_cache_sqlite import SqliteIndicatorCacheStore


def test_store_facade_is_composed_of_domain_repositories(tmp_path) -> None:
    # 派生缓存可以挂 DuckDB 也可以挂 SQLite，两种后端都得是领域仓储之一。
    store = DuckDbMarketDataStore(
        tmp_path / "market.duckdb", indicator_cache_backend=DUCKDB_INDICATOR_CACHE
    )

    assert isinstance(store._bars, DuckDbBarsStore)
    assert isinstance(store._history, DuckDbHistoryStore)
    assert isinstance(store._indicator_cache, DuckDbIndicatorCacheStore)

    sqlite = DuckDbMarketDataStore(
        tmp_path / "other.duckdb", indicator_cache_backend=SQLITE_INDICATOR_CACHE
    )
    assert isinstance(sqlite._indicator_cache, SqliteIndicatorCacheStore)
