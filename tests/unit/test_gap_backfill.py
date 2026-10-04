"""缺失除权事件的反推补全的行为约束。

这个模块的判断链条有两环，两环都容易写错，而且写错之后**代码看上去完全正常**：

1. **"这次到底有没有除权"** 靠板块跌停幅度推出来 —— 主板跌停就是 -10.00%，
   跌幅超过它就不可能是市场波动。但这条判据只在**前后两根 bar 紧邻交易日**时
   成立：跨越停牌的复牌跌幅是跨期累计的，不受涨跌幅约束。把它当除权会给
   600733 算出"停牌 238 天后 10 转 45"这种荒谬因子，而且一旦落库就静默污染
   该股票此后的全部历史指标。

2. **"除权幅度是多少"** 用当日开盘价估计 —— 除权参考价本就是按开盘集合竞价的
   基准算出来的。在 5285 个已知因子的真实事件上实测：开盘价中位误差 0.72%
   （误差 >5% 仅 3.4%），收盘价 2.16%（>5% 达 20.3%）。差三倍，所以不能用收盘价。

交易日历这一环还有个反向的坑：**用自然日判断停牌会把春节长假当成停牌**。
A 股春节休市 7~9 天，周五收盘到节后首日间隔 9~10 个自然日，落在"超过 4 天"
的阈值里。全市场 18.6 万次"跨越"里有 18.5 万次是这么误判的，必须按交易日计。
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from selector_app.market_data.adjustment import compute_factor_ratio
from selector_app.market_data.gap_backfill import (
    MAX_INFERRED_FACTOR,
    SkippedGap,
    _trading_days_between,
    infer_corporate_actions,
)

# 真实交易日历片段（升序）。两段各有用意：
#   2015 段 —— 晨光股份（603898）2015-06-12 停牌、09-14 复牌，中间确无交易日；
#   2024 段 —— 春节休市（02-08 收盘后停市，02-19 开市）加一个普通周末。
# 后者用来区分"长假"和"真停牌"：间隔 11 个自然日，但中间一个交易日都没有。
CALENDAR = [
    date(2015, 6, 12),
    date(2015, 6, 15),
    date(2015, 9, 11),
    date(2015, 9, 14),
    date(2024, 2, 5),
    date(2024, 2, 6),
    date(2024, 2, 7),
    date(2024, 2, 8),
    date(2024, 2, 19),
    date(2024, 2, 20),
    date(2024, 2, 21),
    date(2024, 2, 22),
    date(2024, 2, 23),
    date(2024, 2, 26),
    date(2024, 3, 1),
]


def candidate(
    *,
    prev_date: date = date(2024, 2, 23),
    trade_date: date = date(2024, 2, 26),
    prev_close: float = 10.0,
    open_price: float = 5.0,
    close: float = 5.05,
    board: str = "main",
) -> dict[str, object]:
    return {
        "market": "SH",
        "code": "600000",
        "board": board,
        "date": pd.Timestamp(trade_date),
        "prev_date": prev_date,
        "prev_close": prev_close,
        "open": open_price,
        "close": close,
    }


def frame(*rows: dict[str, object]) -> pd.DataFrame:
    return pd.DataFrame(list(rows))


def test_adjacent_gap_is_inferred_from_open_price() -> None:

    gaps = frame(candidate(prev_close=160.99, open_price=81.00, close=81.00))
    actions, skipped = infer_corporate_actions(gaps, CALENDAR)

    assert not skipped
    assert len(actions) == 1
    # 因子取前收 / 开盘，不取前收 / 收盘：开盘价才是除权参考价的最佳估计。
    assert actions[0].factor_ratio == pytest.approx(160.99 / 81.00)


def test_inferred_factor_round_trips_through_formula() -> None:
    """推断出的事件必须能从自己存的字段重算出同一个因子。

    事件表存的是 ``share_ratio`` 和 ``cash_per_share``，因子是派生量。如果两者
    对不上，那"因子与除权公式重算一致"这条自洽校验就会失败 —— 而这条校验正是
    以后发现数据被改坏的手段，不能自己先把它弄坏。
    """

    gaps = frame(candidate(prev_close=350.00, open_price=175.00, close=175.11))
    actions, _ = infer_corporate_actions(gaps, CALENDAR)

    action = actions[0]
    recomputed = compute_factor_ratio(
        prev_close=350.00,
        share_ratio=action.share_ratio,
        cash_per_share=action.cash_per_share,
    )
    assert recomputed == pytest.approx(action.factor_ratio)
    assert action.action_type == "inferred"


def test_halted_gap_is_never_inferred() -> None:
    """跨越交易日的跳空必须排除：复牌跌幅是跨期累计的，不受涨跌幅约束。

    样本是晨光股份（603898）的真实数据：2015-06-12 收盘 137.49，停牌 3 个月后
    复牌 41.25。当成除权会算出 10 转 23.33，而接口里根本没有这次记录。
    """

    gaps = frame(
        candidate(
            prev_date=date(2015, 6, 12),
            trade_date=date(2015, 9, 14),
            prev_close=137.49,
            open_price=41.25,
            close=41.25,
        )
    )
    actions, skipped = infer_corporate_actions(gaps, CALENDAR)

    assert not actions
    assert len(skipped) == 1
    assert skipped[0].reason == SkippedGap.REASON_HALTED


def test_spring_festival_is_not_mistaken_for_halt() -> None:
    """长假不是停牌：按自然日判断会把 99.8% 的正常跳空误杀。

    2024-02-08 收盘到 02-19 开市之间隔了 11 个自然日，但中间一个交易日都没有，
    所以两根 bar 是紧邻的，跌停幅度判据成立。
    """

    gaps = frame(
        candidate(
            prev_date=date(2024, 2, 8),
            trade_date=date(2024, 2, 19),
            prev_close=10.0,
            open_price=5.0,
            close=5.05,
        )
    )
    actions, skipped = infer_corporate_actions(gaps, CALENDAR)

    assert not skipped
    assert len(actions) == 1


def test_trading_days_between_counts_calendar_only() -> None:

    # 普通周末：周五 -> 周一，中间 0 个交易日。
    assert _trading_days_between(date(2024, 2, 23), date(2024, 2, 26), CALENDAR) == 0
    # 春节长假：跨 9 个自然日，仍是 0 个交易日。
    assert _trading_days_between(date(2024, 2, 8), date(2024, 2, 19), CALENDAR) == 0
    # 真停牌：02-20 停到 03-01，中间漏 02-21、02-22、02-23、02-26 四个交易日。
    assert _trading_days_between(date(2024, 2, 20), date(2024, 3, 1), CALENDAR) == 4


def test_incomplete_calendar_is_rejected() -> None:
    """日历覆盖不到候选范围时必须报错，不能静默把停牌当成除权。

    这是最隐蔽的失效：``_trading_days_between`` 在范围之外一律返回 0，于是
    所有跨停牌的跳空都会被判成"紧邻交易日"而被写成除权事件 —— 正是本模块要
    防的那件事。缺日历时的正确反应是拒绝处理。
    """

    gaps = frame(
        candidate(
            prev_date=date(2015, 6, 12),
            trade_date=date(2015, 9, 14),
            prev_close=137.49,
            open_price=41.25,
            close=41.25,
        )
    )
    with pytest.raises(ValueError, match="未覆盖"):
        infer_corporate_actions(gaps, [date(2024, 2, 5), date(2024, 2, 6)])


def test_open_above_prev_close_is_skipped() -> None:
    """开盘价高于前收盘说明当天没有除权，跌幅发生在盘中。

    这类跳空是真实的盘中跌停（或者数据错误），不是股本变化。硬套因子会算出
    小于 1 的"缩股"，比留一个跳空更糟。
    """

    gaps = frame(candidate(prev_close=10.0, open_price=10.20, close=8.0))
    actions, skipped = infer_corporate_actions(gaps, CALENDAR)

    assert not actions
    assert skipped[0].reason == SkippedGap.REASON_IMPLAUSIBLE


def test_absurd_factor_is_skipped() -> None:
    """因子超出上限说明成因不是送转，宁可留缺口也不能写错因子。"""

    gaps = frame(
        candidate(prev_close=100.0, open_price=100.0 / (MAX_INFERRED_FACTOR + 5), close=0.5)
    )
    actions, skipped = infer_corporate_actions(gaps, CALENDAR)

    assert not actions
    assert skipped[0].reason == SkippedGap.REASON_IMPLAUSIBLE


def test_empty_input_is_handled() -> None:

    actions, skipped = infer_corporate_actions(frame(), CALENDAR)
    assert actions == []
    assert skipped == []


def test_skipped_reason_is_stable_category() -> None:
    """``reason`` 必须是固定分类，不能塞动态数值。

    早期版本直接写 "跨越 128 个交易日"，结果每个天数都成了独立的聚合键，
    跳过的分布统计出来毫无意义。
    """

    gaps = frame(
        candidate(prev_date=date(2024, 1, 2), trade_date=date(2024, 2, 21)),
        candidate(prev_date=date(2024, 1, 2), trade_date=date(2024, 2, 22)),
    )
    _, skipped = infer_corporate_actions(gaps, CALENDAR)

    assert {item.reason for item in skipped} == {SkippedGap.REASON_HALTED}
    assert len({item.detail for item in skipped}) == 2  # 细节各自不同
