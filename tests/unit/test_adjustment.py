"""复权引擎的行为约束。

因子计算本身只有乘法，出错概率极低；真正容易写错的是**从哪天开始生效** ——
写成次日生效会让除权日的跳空原样留在复权序列里，代码看上去完全正常，复权却一点
没做。所以这里用浦发银行（600000）的真实除权日做回归样本，把生效起点钉死。

样本数据取自本地 raw 行情，并用当日真实开盘价交叉验证过：理论除权参考价与实际
开盘的偏差在 0.03% ~ 0.57%（2008-04-24 的 9.83% 和 2010-06-10 的 -2.94% 是
除权当天的真实高开低开，不是公式误差）。
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from selector_app.market_data.adjustment import (
    CorporateAction,
    apply_adjustment,
    build_factor_series,
    compute_factor_ratio,
    events_version,
    verify_against_provider,
)

# (除权日, 除权前收盘价, 每股股本扩张倍数, 每股税前派息, 方案文本)
PUFA_BANK = "600000"
PUFA_EX_DIVIDENDS = [
    ("2002-08-22", 18.40, 0.5, 0.20, "10转5派2.00"),
    ("2009-06-09", 30.59, 0.4, 0.23, "10送4派2.30"),
    ("2010-06-10", 18.50, 0.3, 0.15, "10送3派1.50"),
    ("2011-06-03", 13.21, 0.3, 0.16, "10送3派1.60"),
    ("2017-05-25", 15.47, 0.3, 0.20, "10转3派2.00"),
]


def pufa_action(day: str, prev_close: float, share_ratio: float, cash: float) -> CorporateAction:
    return CorporateAction(
        market="SH",
        code=PUFA_BANK,
        ex_date=date.fromisoformat(day),
        share_ratio=share_ratio,
        cash_per_share=cash,
        factor_ratio=compute_factor_ratio(
            prev_close=prev_close,
            share_ratio=share_ratio,
            cash_per_share=cash,
        ),
        action_type="mixed" if share_ratio > 0 and cash > 0 else "dividend",
    )


def test_factor_ratio_follows_exchange_ex_rights_formula() -> None:
    # 2017-05-25：10转3派2.00，除权前收盘 15.47
    # 除权参考价 = (15.47 - 0.2) / 1.3 = 11.7462；因子 = 15.47 / 11.7462
    ratio = compute_factor_ratio(prev_close=15.47, share_ratio=0.3, cash_per_share=0.2)

    assert ratio == pytest.approx(15.47 / ((15.47 - 0.2) / 1.3))
    assert ratio == pytest.approx(1.317027, abs=1e-6)


def test_factor_takes_effect_on_ex_dividend_day_itself() -> None:
    """除权日当天的 raw 价已经是除权**后**的价格，因子必须当天就乘上。

    这条是整个引擎最容易写错的地方：若从次一 bar 起算，除权日那个断崖会原样
    留在复权序列里，指标照样被污染，而代码看起来完全正常。
    """

    dates = pd.to_datetime(["2017-05-24", "2017-05-25", "2017-05-26"])
    action = pufa_action("2017-05-25", 15.47, 0.3, 0.2)
    factors = build_factor_series(dates, [action])

    assert factors == pytest.approx([1.0, 1.317027, 1.317027])
    raw_close = [15.47, 11.75, 13.10]
    adjusted = [raw * factor for raw, factor in zip(raw_close, factors)]
    # 除权日从 -24.05% 的假跳空变成 +0.03%（当天真实涨幅）
    assert (raw_close[1] / raw_close[0] - 1) < -0.2
    assert (adjusted[1] / adjusted[0] - 1) == pytest.approx(0.0003, abs=5e-4)


def test_factor_accumulates_across_events() -> None:
    dates = pd.to_datetime(
        [
            "2010-06-09",
            "2010-06-10",
            "2011-06-02",
            "2011-06-03",
            "2011-06-04",
        ]
    )
    actions = [
        pufa_action("2010-06-10", 18.50, 0.3, 0.15),
        pufa_action("2011-06-03", 13.21, 0.3, 0.16),
    ]
    factors = build_factor_series(dates, actions)

    first = compute_factor_ratio(prev_close=18.50, share_ratio=0.3, cash_per_share=0.15)
    second = compute_factor_ratio(prev_close=13.21, share_ratio=0.3, cash_per_share=0.16)
    assert factors == pytest.approx([1.0, first, first, first * second, first * second])


def test_event_on_first_bar_applies_from_that_bar() -> None:
    dates = pd.to_datetime(["2017-05-25", "2017-05-26"])
    action = pufa_action("2017-05-25", 15.47, 0.3, 0.2)
    factors = build_factor_series(dates, [action])

    assert factors == pytest.approx([1.317027, 1.317027])


def test_event_outside_sample_does_not_shift_factors() -> None:
    dates = pd.to_datetime(["2017-05-24", "2017-05-25", "2017-05-26"])
    inside = pufa_action("2017-05-25", 15.47, 0.3, 0.2)
    after = pufa_action("2030-01-02", 9.0, 1.0, 0.0)
    before = pufa_action("1999-11-10", 1.0, 0.0, 0.5)

    assert build_factor_series(dates, [inside, after]) == pytest.approx(
        build_factor_series(dates, [inside])
    )
    # 样本之前的事件已经被累乘进首根 bar，不存在"从零开始"的分支
    assert build_factor_series(dates, [before])[0] == pytest.approx(
        compute_factor_ratio(prev_close=1.0, share_ratio=0.0, cash_per_share=0.5)
    )


def test_no_actions_gives_unit_factors() -> None:
    dates = pd.to_datetime(["2017-05-24", "2017-05-25"])
    assert build_factor_series(dates, []) == pytest.approx([1.0, 1.0])
    assert build_factor_series([], []).size == 0


def test_apply_adjustment_scales_prices_but_not_volume() -> None:
    frame = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2017-05-24", "2017-05-25"]),
            "open": [15.40, 11.70],
            "high": [15.60, 11.90],
            "low": [15.30, 11.60],
            "close": [15.47, 11.75],
            "volume": [1_000_000.0, 1_200_000.0],
            "amount": [15_470_000.0, 14_100_000.0],
        }
    )
    factors = np.array([1.0, 1.317027])

    adjusted = apply_adjustment(frame, factors)

    assert adjusted["close"].tolist() == pytest.approx([15.47, 15.475], abs=1e-3)
    assert adjusted["open"].tolist() == pytest.approx([15.40, 11.70 * 1.317027], abs=1e-6)
    # 成交量与成交额是**事实量**：分红送转不改变当天实际成交了多少股、多少元
    assert adjusted["volume"].tolist() == [1_000_000.0, 1_200_000.0]
    assert adjusted["amount"].tolist() == [15_470_000.0, 14_100_000.0]
    # 入参不能被就地改掉
    assert frame["close"].tolist() == [15.47, 11.75]


def test_apply_adjustment_rejects_length_mismatch() -> None:
    frame = pd.DataFrame({"close": [1.0, 2.0]})
    with pytest.raises(ValueError, match="长度"):
        apply_adjustment(frame, np.array([1.0]))


def test_corporate_action_rejects_bad_numbers() -> None:
    with pytest.raises(ValueError, match="因子增量"):
        CorporateAction("SH", "600000", date(2017, 5, 25), 0.3, 0.2, 0.0, "mixed")
    with pytest.raises(ValueError, match="股本扩张"):
        CorporateAction("SH", "600000", date(2017, 5, 25), -1.0, 0.2, 1.2, "mixed")
    with pytest.raises(ValueError, match="每股派息"):
        CorporateAction("SH", "600000", date(2017, 5, 25), 0.3, float("nan"), 1.2, "mixed")


def test_compute_factor_ratio_rejects_non_positive_reference() -> None:
    with pytest.raises(ValueError, match="必须为正"):
        compute_factor_ratio(prev_close=1.0, share_ratio=0.0, cash_per_share=1.5)
    with pytest.raises(ValueError, match="必须为正"):
        compute_factor_ratio(prev_close=0.0, share_ratio=0.0, cash_per_share=0.0)


def test_events_version_tracks_content_not_order() -> None:
    first = pufa_action("2017-05-25", 15.47, 0.3, 0.2)
    second = pufa_action("2011-06-03", 13.21, 0.3, 0.16)

    assert events_version([first, second]) == events_version([second, first])
    assert events_version([first]) != events_version([first, second])
    changed = pufa_action("2017-05-25", 15.47, 0.3, 0.2)
    assert events_version([changed]) == events_version([first])


def test_verify_accepts_matching_provider_factors() -> None:
    raw = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2017-05-24", "2017-05-25", "2017-05-26"]),
            "close": [15.47, 11.75, 13.10],
        }
    )
    factor = compute_factor_ratio(prev_close=15.47, share_ratio=0.3, cash_per_share=0.2)
    provider = raw.copy()
    provider["close"] = raw["close"].to_numpy() * np.array([1.0, factor, factor])
    actions = [pufa_action("2017-05-25", 15.47, 0.3, 0.2)]

    assert verify_against_provider(raw, provider, actions) == []


def test_verify_flags_missing_non_cash_event() -> None:
    """provider 那边多出一次股本调整（配股 / 回购注销），事件表没有，必须报出来。"""

    raw = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2017-05-24", "2017-05-25", "2017-05-26"]),
            "close": [15.47, 11.75, 13.10],
        }
    )
    expected = compute_factor_ratio(prev_close=15.47, share_ratio=0.3, cash_per_share=0.2)
    # provider 实际给出的单次因子比自算的大 10%，典型的"漏了一次配股"
    provider = raw.copy()
    provider["close"] = raw["close"].to_numpy() * np.array([1.0, expected * 1.1, expected * 1.1])
    actions = [pufa_action("2017-05-25", 15.47, 0.3, 0.2)]

    mismatches = verify_against_provider(raw, provider, actions)

    assert len(mismatches) == 1
    assert mismatches[0].ex_date == date(2017, 5, 25)
    assert mismatches[0].expected == pytest.approx(expected)
    assert mismatches[0].observed == pytest.approx(expected * 1.1)
    assert mismatches[0].drift == pytest.approx(0.1, abs=1e-3)


def test_verify_requires_aligned_columns() -> None:
    actions = [pufa_action("2017-05-25", 15.47, 0.3, 0.2)]
    raw = pd.DataFrame({"trade_date": pd.to_datetime(["2017-05-24"]), "close": [1.0]})
    missing_close = pd.DataFrame({"trade_date": pd.to_datetime(["2017-05-24"])})

    with pytest.raises(ValueError, match="缺少列"):
        verify_against_provider(raw, missing_close, actions)
    with pytest.raises(ValueError, match="缺少列"):
        verify_against_provider(missing_close, raw, actions)
