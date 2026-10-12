from __future__ import annotations

import pytest

from aquest.backtest.profiles import (
    STOCK_DEFAULT_COMMISSION,
    STOCK_DEFAULT_MIN_COMMISSION,
    STOCK_DEFAULT_STAMP_TAX,
    profile_from_config,
    profile_from_overrides,
)

STOCK_TRIPLE = {
    "commission": STOCK_DEFAULT_COMMISSION,
    "min_commission": STOCK_DEFAULT_MIN_COMMISSION,
    "stamp_tax": STOCK_DEFAULT_STAMP_TAX,
}


def test_stock_profile_keeps_stamp_tax_when_only_commission_changes() -> None:
    """只改佣金时印花税必须留在股票默认档。

    逐字段判定保证未改动的字段回落到品种默认，不会因为「三件套没全命中」
    而把某一项退回完全不同的口径。
    """

    profile = profile_from_config("stock", **{**STOCK_TRIPLE, "commission": 0.0003})

    assert profile.commission == pytest.approx(0.0003)
    assert profile.stamp_tax == pytest.approx(STOCK_DEFAULT_STAMP_TAX)


def test_stock_profile_keeps_min_commission_when_only_stamp_tax_is_cleared() -> None:
    profile = profile_from_config("stock", **{**STOCK_TRIPLE, "stamp_tax": 0.0})

    assert profile.min_commission == pytest.approx(STOCK_DEFAULT_MIN_COMMISSION)
    assert profile.stamp_tax == 0.0


def test_stock_profile_returns_defaults_when_nothing_is_touched() -> None:
    profile = profile_from_config("stock", **STOCK_TRIPLE)

    assert profile.commission == pytest.approx(STOCK_DEFAULT_COMMISSION)
    assert profile.stamp_tax == pytest.approx(STOCK_DEFAULT_STAMP_TAX)


def test_explicit_non_default_costs_always_win() -> None:
    profile = profile_from_config("stock", commission=0.0002, min_commission=1.0, stamp_tax=0.0005)

    assert profile.commission == pytest.approx(0.0002)
    assert profile.min_commission == pytest.approx(1.0)
    assert profile.stamp_tax == pytest.approx(0.0005)


def test_profile_from_config_matches_profile_from_overrides() -> None:
    """两条解析路径必须一致，否则研究与组合回测会算出不同成本。"""

    cases = (
        {**STOCK_TRIPLE, "commission": 0.0003},
        {**STOCK_TRIPLE, "stamp_tax": 0.0},
        {**STOCK_TRIPLE, "min_commission": 5.0},
        STOCK_TRIPLE,
    )
    for costs in cases:
        assert profile_from_config("stock", **costs) == profile_from_overrides("stock", **costs)
