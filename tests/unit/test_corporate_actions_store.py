"""分红送转事件表的读写约束。

事件表是复权因子的唯一来源，所以它的行为边界必须明确：覆盖语义要正确（provider
补发修正后不能出现两条同一天的事件），版本号要真的随内容变（指标缓存靠它失效）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from selector_app.market_data.adjustment import CorporateAction, compute_factor_ratio
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.market_data.store_bars import corporate_actions_from_frame


def action(
    code: str,
    day: str,
    *,
    share_ratio: float = 0.3,
    cash_per_share: float = 0.2,
    factor_ratio: float | None = None,
    market: str = "SH",
) -> CorporateAction:
    return CorporateAction(
        market=market,
        code=code,
        ex_date=date.fromisoformat(day),
        share_ratio=share_ratio,
        cash_per_share=cash_per_share,
        factor_ratio=(
            factor_ratio
            if factor_ratio is not None
            else compute_factor_ratio(
                prev_close=15.47,
                share_ratio=share_ratio,
                cash_per_share=cash_per_share,
            )
        ),
        action_type="mixed",
    )


def store_at(tmp_path: Path) -> DuckDbMarketDataStore:
    return DuckDbMarketDataStore(tmp_path / "market.duckdb")


def test_actions_round_trip_with_all_fields(tmp_path: Path) -> None:
    store = store_at(tmp_path)

    assert store.upsert_corporate_actions([action("600000", "2017-05-25")]) == 1

    frame = store.read_corporate_actions("SH", "600000")
    assert len(frame) == 1
    row = frame.iloc[0]
    assert row["code"] == "600000"
    assert row["ex_date"] == pd.Timestamp("2017-05-25")
    assert row["share_ratio"] == pytest.approx(0.3)
    assert row["cash_per_share"] == pytest.approx(0.2)
    assert row["factor_ratio"] == pytest.approx(1.317027, abs=1e-6)
    assert row["action_type"] == "mixed"
    assert row["provider"] == "eastmoney"


def test_read_filters_by_market_and_date(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.upsert_corporate_actions(
        [
            action("600000", "2011-06-03"),
            action("600000", "2017-05-25"),
            action("000001", "2015-06-23", market="SZ"),
        ]
    )

    assert len(store.read_corporate_actions("SH", "600000")) == 2
    assert len(store.read_corporate_actions("SH", "600000", start_date=date(2015, 1, 1))) == 1
    assert len(store.read_corporate_actions("SH", "600000", end_date=date(2015, 1, 1))) == 1
    # 600000 的事件不能串到 000001 上
    assert len(store.read_corporate_actions("SZ", "000001")) == 1


def test_same_ex_date_is_overwritten_not_duplicated(tmp_path: Path) -> None:
    """provider 补发修正后重跑同步，不能在同一除权日留下两条事件。"""

    store = store_at(tmp_path)
    store.upsert_corporate_actions([action("600000", "2017-05-25")])
    store.upsert_corporate_actions(
        [action("600000", "2017-05-25", share_ratio=0.5, cash_per_share=0.25)]
    )

    frame = store.read_corporate_actions("SH", "600000")
    assert len(frame) == 1
    assert frame.iloc[0]["share_ratio"] == pytest.approx(0.5)
    assert frame.iloc[0]["factor_ratio"] != pytest.approx(1.317027, abs=1e-6)


def test_batch_read_returns_only_requested_symbols(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.upsert_corporate_actions(
        [
            action("600000", "2017-05-25"),
            action("600000", "2011-06-03"),
            action("000001", "2015-06-23", market="SZ"),
            action("600001", "2016-06-23"),
        ]
    )

    frame = store.read_corporate_actions_batch(
        [StockRefLike("SH", "600000"), StockRefLike("SH", "600001")]
    )

    assert set(frame["code"]) == {"600000", "600001"}
    assert len(frame) == 3
    # 按 market, code, ex_date 排序，因子累乘依赖这个顺序
    listed = list(zip(frame["code"], frame["ex_date"]))
    assert listed == sorted(listed, key=lambda item: (item[0], item[1]))


def test_batch_read_of_nothing_returns_empty_frame(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    assert store.read_corporate_actions_batch([]).empty


def test_version_is_empty_until_something_is_written(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    assert store.corporate_actions_version() == ""

    store.upsert_corporate_actions([action("600000", "2017-05-25")])
    seeded = store.corporate_actions_version()
    assert seeded

    # 内容没变 → 版本不变，指标缓存可以继续复用
    store.upsert_corporate_actions([action("600000", "2017-05-25")])
    assert store.corporate_actions_version() == seeded

    # 内容变了 → 版本必须变，否则事件补录后指标缓存不会重算
    store.upsert_corporate_actions([action("600000", "2011-06-03")])
    assert store.corporate_actions_version() != seeded


def test_empty_upsert_is_a_no_op(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    assert store.upsert_corporate_actions([]) == 0
    assert store.corporate_actions_version() == ""


def test_frame_conversion_round_trip(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    original = [action("600000", "2011-06-03"), action("600000", "2017-05-25")]
    store.upsert_corporate_actions(original)

    restored = corporate_actions_from_frame(store.read_corporate_actions("SH", "600000"))

    assert [(item.ex_date, round(item.factor_ratio, 9)) for item in restored] == [
        (item.ex_date, round(item.factor_ratio, 9)) for item in original
    ]


class StockRefLike:
    """只要 market / code 两个属性，够 :func:`read_corporate_actions_batch` 使用。"""

    def __init__(self, market: str, code: str) -> None:
        self.market = market
        self.code = code
