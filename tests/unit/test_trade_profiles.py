from __future__ import annotations

from selector_app.backtest.profiles import profile_for


def test_stock_profile_uses_stock_lot_and_costs() -> None:
    profile = profile_for("stock")

    assert profile.lot_size == 100
    assert profile.commission == 0.0001
    assert profile.min_commission == 0.1
    assert profile.stamp_tax == 0.001


def test_non_stock_profile_uses_fund_like_virtual_costs() -> None:
    for instrument_type in ("fund", "index", "bond"):
        profile = profile_for(instrument_type)
        assert profile.lot_size == 100
        assert profile.commission == 0.00005
        assert profile.min_commission == 0.1
        assert profile.stamp_tax == 0.0
