from __future__ import annotations

from aquest.backtest.profiles import profile_for


def test_stock_profile_uses_stock_lot_and_costs() -> None:
    profile = profile_for("stock")

    assert profile.lot_size == 100
    assert profile.commission == 0.0001
    assert profile.min_commission == 0.1
    assert profile.stamp_tax == 0.001
