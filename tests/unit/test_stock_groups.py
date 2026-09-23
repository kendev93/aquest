"""股票组：成员归一化与等权买入持有的口径。

这里的断言不是为了覆盖代码，是为了钉住三条一旦改错就没人看得出来的口径：
首日无价要剔除、缺失日沿用前值、超额用差值不用比值。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from selector_app.stock_groups.models import normalize_members
from selector_app.stock_groups.performance import (
    equal_weight_curve,
    excess_return,
    member_spread,
    pivot_closes,
    price_ratios,
    stalled_members,
)


def _frame(rows: dict[str, list[float]], days: tuple[int, ...]) -> pd.DataFrame:
    return pd.DataFrame(rows, index=list(days))


def test_normalize_members_dedupes_and_keeps_order() -> None:
    members = normalize_members(
        [
            {"market": "sh", "code": "600001", "name": "A"},
            {"market": "SZ", "code": "000001"},
            {"market": "SH", "code": "600001", "name": "A again"},
            {"market": "", "code": "000002"},
            {"market": "SH", "code": ""},
        ]
    )

    # 同一只票出现两次会让等权权重悄悄翻倍，必须只留一个；顺序保留勾选先后。
    assert [member.key for member in members] == [("SH", "600001"), ("SZ", "000001")]
    assert members[0].name == "A"
    assert members[1].name is None


def test_price_ratios_drops_symbols_without_a_starting_price() -> None:
    prices = _frame(
        {"SH:600001": [10.0, 11.0, 12.0], "SZ:000001": [np.nan, 8.0, 9.0]},
        (20260101, 20260102, 20260103),
    )

    ratios = price_ratios(prices)

    # 起点没价的（新股、停牌）不能回填：那等于默认「上市之前也买得到」。
    assert list(ratios.columns) == ["SH:600001"]
    assert ratios["SH:600001"].iloc[-1] == pytest.approx(1.2)


def test_price_ratios_carries_the_last_price_through_missing_days() -> None:
    prices = _frame(
        {"SH:600001": [10.0, np.nan, np.nan, 12.0]},
        (20260101, 20260102, 20260103, 20260104),
    )

    ratios = price_ratios(prices)

    # 停牌期间价格没动，收益就是 0——不是跳过，也不是把这段日子从曲线里删掉。
    assert list(ratios["SH:600001"]) == pytest.approx([1.0, 1.0, 1.0, 1.2])
    assert len(ratios.index) == 4


def test_equal_weight_curve_averages_member_ratios() -> None:
    prices = _frame(
        {"SH:600001": [10.0, 20.0], "SH:600002": [10.0, 5.0]},
        (20260101, 20260102),
    )

    curve = equal_weight_curve(prices, "组")

    assert curve is not None
    assert curve.instrument_count == 2
    # (2.0 + 0.5) / 2 = 1.25
    assert curve.totals[-1] == pytest.approx(1.25)
    assert curve.performance["total_return"] == pytest.approx(0.25)


def test_equal_weight_curve_needs_two_trading_days() -> None:
    prices = _frame({"SH:600001": [10.0]}, (20260101,))

    assert equal_weight_curve(prices, "组") is None


def test_excess_return_is_a_difference_not_a_ratio() -> None:
    group = _frame({"SH:600001": [10.0, 9.5]}, (20260101, 20260102))
    market = _frame({"SH:600002": [10.0, 9.0]}, (20260101, 20260102))

    value = excess_return(
        equal_weight_curve(group, "组"),
        equal_weight_curve(market, "全市场"),
    )

    # 组 -5%、全市场 -10%：相减是「少亏了 5 个百分点」，相除会读出「差两倍」这种
    # 符号都未必对得住的结论。
    assert value == pytest.approx(0.05)


def test_excess_return_is_none_without_both_curves() -> None:
    prices = _frame({"SH:600001": [10.0, 11.0]}, (20260101, 20260102))

    assert excess_return(equal_weight_curve(prices, "组"), None) is None


def test_stalled_members_names_symbols_that_stop_having_prices() -> None:
    """末段连续缺 20 个交易日以上 = 不再有价（摘牌）；中间的停牌不算。"""

    days = tuple(20260101 + offset for offset in range(30))
    prices = _frame(
        {
            # 第 5 天之后就再没有价：末段缺 25 个交易日 —— 摘牌的特征。
            "SH:600001": [10.0] * 5 + [np.nan] * 25,
            # 中间停牌 10 天又复牌：那段时间价格确实没动，不是消失。
            "SH:600002": [10.0] * 5 + [np.nan] * 10 + [11.0] * 15,
        },
        days,
    )
    members = normalize_members(
        [
            {"market": "SH", "code": "600001", "name": "A"},
            {"market": "SH", "code": "600002", "name": "B"},
        ]
    )

    stalled = stalled_members(prices, members)

    assert [item["code"] for item in stalled] == ["600001"]
    assert stalled[0]["name"] == "A"
    assert stalled[0]["last_date"] == "2026-01-05"
    assert stalled[0]["missing_days"] == 25

    # 曲线**不改数**：摘牌的票继续按末次收盘价挂着，那是「钱还在里面、变现不了」
    # 的写法；改写成「按末次价卖出」等于替用户做了一笔他没做的交易。
    curve = equal_weight_curve(prices, "组")
    assert curve is not None
    assert curve.instrument_count == 2
    assert curve.totals[-1] == pytest.approx(1.05)


def test_stalled_threshold_is_twenty_trading_days() -> None:
    """20 ≈ 一个自然月：退市整理期（15 个交易日）之后的空白必然被判出来。"""

    days = tuple(20260101 + offset for offset in range(30))

    def _symbol(valid_rows: int) -> dict[str, list[float]]:
        return {"SH:600001": [10.0] * valid_rows + [np.nan] * (len(days) - valid_rows)}

    # 末段只缺 19 个交易日：还按停牌处理。
    assert stalled_members(_frame(_symbol(11), days), ()) == ()
    # 缺满 20 个交易日就判定不再有价。
    assert [item["code"] for item in stalled_members(_frame(_symbol(10), days), ())] == ["600001"]


def test_member_spread_reports_the_distribution() -> None:
    prices = _frame(
        {"SH:600001": [10.0, 12.0], "SH:600002": [10.0, 9.0], "SH:600003": [10.0, 10.5]},
        (20260101, 20260102),
    )
    members = normalize_members(
        [
            {"market": "SH", "code": "600001", "name": "A"},
            {"market": "SH", "code": "600002", "name": "B"},
            {"market": "SH", "code": "600003", "name": "C"},
        ]
    )

    spread = member_spread(prices, members)

    assert spread is not None
    assert spread.counted == 3
    assert spread.advanced == 2
    assert spread.declined == 1
    assert spread.best is not None and spread.best["code"] == "600001"
    assert spread.worst is not None and spread.worst["code"] == "600002"


def test_pivot_closes_builds_a_date_by_symbol_matrix() -> None:
    frame = pd.DataFrame(
        {
            "market": ["SH", "SH", "SZ"],
            "code": ["600001", "600001", "000001"],
            "date": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-01"]),
            "close": [10.0, 11.0, 5.0],
        }
    )

    wide = pivot_closes(frame)

    assert list(wide.columns) == ["SH:600001", "SZ:000001"]
    assert list(wide.index) == [20260101, 20260102]
    assert wide.loc[20260102, "SH:600001"] == pytest.approx(11.0)
    assert np.isnan(wide.loc[20260102, "SZ:000001"])
