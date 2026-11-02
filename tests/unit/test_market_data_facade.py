import pytest

import aquest.market_data as market_data


def test_package_facade_resolves_public_exports() -> None:
    from aquest.market_data import DuckDbMarketDataStore, StockRef

    assert DuckDbMarketDataStore.__module__ == "aquest.market_data.store"
    assert StockRef.__module__ == "aquest.market_data.models"


def test_package_facade_rejects_unknown_names() -> None:
    with pytest.raises(AttributeError, match="NoSuchThing"):
        market_data.NoSuchThing
