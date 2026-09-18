from __future__ import annotations

import pytest

from selector_app.backtest.profiles import (
    FUND_DEFAULT_COMMISSION,
    FUND_DEFAULT_STAMP_TAX,
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


def test_fund_profile_keeps_zero_stamp_tax_when_only_commission_changes() -> None:
    """只改佣金时印花税必须留在基金档。

    旧的整组判定会因为「三件套没全命中」而把印花税退回股票档的 0.1%，
    每笔卖出被收 10bp —— 约为基金佣金万 0.5 的 20 倍。
    """

    profile = profile_from_config("fund", **{**STOCK_TRIPLE, "commission": 0.0003})

    assert profile.commission == pytest.approx(0.0003)
    assert profile.stamp_tax == FUND_DEFAULT_STAMP_TAX == 0.0


def test_fund_profile_keeps_fund_commission_when_only_stamp_tax_is_cleared() -> None:
    """只把印花税改成 0 时，佣金不该退回股票档。"""

    profile = profile_from_config("fund", **{**STOCK_TRIPLE, "stamp_tax": 0.0})

    assert profile.commission == pytest.approx(FUND_DEFAULT_COMMISSION)
    assert profile.stamp_tax == 0.0


def test_fund_profile_keeps_fund_defaults_when_nothing_is_touched() -> None:
    profile = profile_from_config("fund", **STOCK_TRIPLE)

    assert profile.commission == pytest.approx(FUND_DEFAULT_COMMISSION)
    assert profile.stamp_tax == pytest.approx(FUND_DEFAULT_STAMP_TAX)


def test_stock_profile_is_unaffected_by_the_per_field_fallback() -> None:
    profile = profile_from_config("stock", **STOCK_TRIPLE)

    assert profile.commission == pytest.approx(STOCK_DEFAULT_COMMISSION)
    assert profile.stamp_tax == pytest.approx(STOCK_DEFAULT_STAMP_TAX)


def test_explicit_non_default_costs_always_win() -> None:
    profile = profile_from_config("fund", commission=0.0002, min_commission=1.0, stamp_tax=0.0005)

    assert profile.commission == pytest.approx(0.0002)
    assert profile.min_commission == pytest.approx(1.0)
    assert profile.stamp_tax == pytest.approx(0.0005)


def test_profile_from_config_matches_profile_from_overrides() -> None:
    """两条解析路径必须一致，否则研究与组合回测会算出不同成本。"""

    cases = (
        ("fund", {**STOCK_TRIPLE, "commission": 0.0003}),
        ("fund", {**STOCK_TRIPLE, "stamp_tax": 0.0}),
        ("fund", {**STOCK_TRIPLE, "min_commission": 5.0}),
        ("fund", STOCK_TRIPLE),
        ("stock", STOCK_TRIPLE),
    )
    for instrument_type, costs in cases:
        assert profile_from_config(instrument_type, **costs) == profile_from_overrides(
            instrument_type, **costs
        )
