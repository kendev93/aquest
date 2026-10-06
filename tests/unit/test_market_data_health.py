"""行情健康检查的判定测试。

体检的卖点是「把静默的问题显式化」，所以每条断言都钉在**能不能看见**上：
有问题就必须被点名，没问题就必须显示 ok。阈值与口径本身写在
``selector_app/market_data/health.py`` 的模块注释里，改口径要同步改那里。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from selector_app.adapters.market_sync import MarketSyncConfig
from selector_app.adapters.online_market_sync import _restrict_to_codes, _TargetSelection
from selector_app.market_data.adjustment import CorporateAction
from selector_app.market_data.health import CHECK_KEYS, normalize_keys
from selector_app.market_data.store import DuckDbMarketDataStore

DAYS = 60
START = "2024-01-01"


def bars(drop: set[str] | None = None, *, length: int = DAYS) -> pd.DataFrame:
    """一段干净的日线：缓涨、成交量恒定、每天都有。"""

    dates = pd.date_range(START, periods=length, freq="D")
    rows = []
    for index, day in enumerate(dates):
        iso = day.strftime("%Y-%m-%d")
        if drop and iso in drop:
            continue
        close = 10.0 + index * 0.05
        rows.append(
            {
                "date": day,
                "open": close - 0.1,
                "high": close + 0.2,
                "low": close - 0.2,
                "close": close,
                "volume": 100_000.0,
                "amount": close * 100_000.0,
                "bar_status": "completed",
            }
        )
    return pd.DataFrame(rows)


def report(store: DuckDbMarketDataStore):
    return store.health_report(generated_at="2024-03-01T00:00:00", duration_seconds=0.0)


def check_by_key(store: DuckDbMarketDataStore) -> dict[str, object]:
    return {item.key: item for item in report(store).checks}


def test_clean_store_reports_everything_ok(tmp_path: Path) -> None:
    """三只票、同样的日期、没有异常 → 七项检查必须全部 ok。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    for market, code in (("SH", "600000"), ("SZ", "000001"), ("SZ", "300750")):
        store.replace_local_bars(market, code, "stock", bars(), name=code)

    checks = check_by_key(store)
    assert {key: checks[key].severity for key in CHECK_KEYS} == {
        "gap": "ok",
        "long_gap": "ok",
        "ohlc": "ok",
        "price_jump": "ok",
        "zero_volume": "ok",
        "stale": "ok",
        "missing_action": "ok",
    }


def test_missing_days_are_reported_as_gap(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", bars(), name="基准")
    store.replace_local_bars(
        "SZ",
        "000001",
        "stock",
        bars(drop={"2024-01-10", "2024-02-01", "2024-02-02"}),
        name="缺三天",
    )

    gap = check_by_key(store)["gap"]
    assert gap.severity == "error"
    assert gap.affected_instruments == 1
    assert gap.occurrences == 3
    assert gap.examples[0].code == "000001"


def test_occurrence_counts_are_not_capped_by_the_example_limit(tmp_path: Path) -> None:
    """「次数」是全库总数，不能是前 20 条示例之和。

    存储层已经把总数算出来了，曾经汇总时却丢掉它、改成拿示例行求和——示例默认只
    有 20 条，于是 25 只票各缺一天会显示 20。这个数看着像总数，用户拿它判断要不要
    重下数据，少报的恰恰是最该修的那一批。
    """

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    # 25 只票，各缺一个不同的交易日；其余票当天仍有 bar，所以那天仍是交易日。
    for offset in range(25):
        code = f"{600000 + offset}"
        store.replace_local_bars(
            "SH", code, "stock", bars(drop={f"2024-01-{2 + offset:02d}"}), name=code
        )

    gap = check_by_key(store)["gap"]
    assert gap.affected_instruments == 25
    assert gap.occurrences == 25
    # 示例该截断还是要截断，不然这一项本身就大到打不开。
    assert len(gap.examples) == 20


def test_long_missing_run_is_flagged_separately(tmp_path: Path) -> None:
    """连续缺 8 天：既算缺口，也要单独标成「连续缺失段」（可能是长期停牌）。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", bars(), name="基准")
    store.replace_local_bars(
        "SZ",
        "000001",
        "stock",
        bars(drop={f"2024-01-{day:02d}" for day in range(11, 19)}),
        name="停牌",
    )

    checks = check_by_key(store)
    assert checks["long_gap"].affected_instruments == 1
    assert checks["long_gap"].severity == "warn"
    assert "连续缺 8 个交易日" in checks["long_gap"].examples[0].detail


def test_bad_ohlc_and_price_jump_are_reported(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", bars(), name="基准")
    frame = bars()
    frame.loc[frame.index[20], "high"] = 1.0
    frame.loc[frame.index[21], "close"] = frame.loc[frame.index[20], "close"] * 1.35
    store.replace_local_bars("SZ", "300750", "stock", frame, name="异常", board="chinext")

    checks = check_by_key(store)
    assert checks["ohlc"].affected_instruments == 1
    assert checks["price_jump"].affected_instruments == 1
    assert "35.0%" in checks["price_jump"].examples[0].detail


def test_zero_volume_and_stale_are_warned(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", bars(), name="基准")
    frame = bars()
    frame.loc[frame.index[22], "volume"] = 0.0
    store.replace_local_bars("SZ", "000001", "stock", frame, name="零成交")
    store.replace_local_bars("SH", "600001", "stock", bars(length=20), name="陈旧")

    checks = check_by_key(store)
    assert checks["zero_volume"].affected_instruments == 1
    assert checks["stale"].affected_instruments == 1
    assert "落后全市场" in checks["stale"].examples[0].detail


def crashed(frame: pd.DataFrame, index: int, *, ratio: float = 0.5) -> pd.DataFrame:
    """把第 ``index`` 天砸成远超跌停的暴跌，同时保持 OHLC 自洽。

    ``ratio`` 默认腰斩：主板跌停只有 -10%，腰斩不可能是市场波动。
    """

    previous = float(frame.loc[frame.index[index - 1], "close"])
    frame.loc[frame.index[index], "open"] = previous * ratio
    frame.loc[frame.index[index], "close"] = previous * ratio
    frame.loc[frame.index[index], "high"] = previous * (ratio + 0.01)
    frame.loc[frame.index[index], "low"] = previous * (ratio - 0.01)
    return frame


def test_missing_action_reports_unexplained_limit_down(tmp_path: Path) -> None:
    """跌到跌停板以下却查不到事件 → 必须被点名，且**不可**一键重下修复。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", bars(), name="基准")
    store.replace_local_bars(
        "SH", "600666", "stock", crashed(bars(), 20), name="疑似漏记"
    )

    item = check_by_key(store)["missing_action"]
    assert item.severity == "warn"
    assert item.affected_instruments == 1
    assert item.occurrences == 1
    # 重下只是拿回同一份 bar，缺的是事件本身；让它可修会诱导人删掉一批数据。
    assert item.repairable is False
    assert item.examples[0].market == "SH"
    assert item.examples[0].code == "600666"
    assert item.examples[0].name == "疑似漏记"
    assert item.examples[0].trade_date == "2024-01-21"
    assert "事件表当日无记录" in item.examples[0].detail


def test_recorded_corporate_action_is_not_reported(tmp_path: Path) -> None:
    """同一天补上事件记录后就必须消失。

    这条是这一项能不能用下去的关键：正常除权日本来就该大跌，如果连它们都报，
    这一项会长期挂在几百条上，跟永远红灯没区别 —— 而永久红灯等于没人看。
    """

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", bars(), name="基准")
    store.replace_local_bars(
        "SH", "600666", "stock", crashed(bars(), 20), name="腰斩"
    )
    assert check_by_key(store)["missing_action"].occurrences == 1

    store.upsert_corporate_actions(
        [
            CorporateAction(
                market="SH",
                code="600666",
                ex_date=date(2024, 1, 21),
                share_ratio=1.0,
                cash_per_share=0.0,
                factor_ratio=2.0,
                action_type="split",
            )
        ],
        source="test",
        provider="test",
    )

    item = check_by_key(store)["missing_action"]
    assert item.occurrences == 0
    assert item.affected_instruments == 0
    assert item.severity == "ok"


def test_missing_action_examples_are_newest_first(tmp_path: Path) -> None:
    """示例按日期倒序：历史候选是常态，要第一时间看见的是**新冒出来的那条**。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", bars(), name="基准")
    frame = bars()
    crashed(frame, 10)
    crashed(frame, 30)
    store.replace_local_bars("SH", "600666", "stock", frame, name="两次腰斩")

    item = check_by_key(store)["missing_action"]
    assert item.occurrences == 2
    assert item.affected_instruments == 1
    assert [example.trade_date for example in item.examples] == [
        "2024-01-31",
        "2024-01-11",
    ]


def test_codes_for_collects_every_flagged_instrument(tmp_path: Path) -> None:
    """修复名单不能只取示例里的那几条——示例只有 20 条，会静默漏掉大部分。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", bars(), name="基准")
    for offset in range(3):
        code = f"00000{offset + 1}"
        store.replace_local_bars(
            "SZ",
            code,
            "stock",
            bars(drop={f"2024-01-{10 + offset:02d}"}),
            name=code,
        )

    codes = store.health.codes_for(("gap",))
    assert codes == (("SZ", "000001"), ("SZ", "000002"), ("SZ", "000003"))
    assert store.health.codes_for(("long_gap",)) == ()


def test_codes_without_bars_after_a_failed_redownload(tmp_path: Path) -> None:
    """先删后下的风险出口：删了没补回来的标的必须被点名。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", bars(), name="基准")
    store.replace_local_bars("SZ", "000001", "stock", bars(), name="会被删掉")

    assert store.health.codes_without_bars(("600000", "000001")) == ()
    store.delete_bars(codes=["000001"])
    assert store.health.codes_without_bars(("600000", "000001")) == ("000001",)


def test_report_carries_overview_counts(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", bars(), name="基准")

    data = report(store).to_dict()
    assert data["instrument_count"] == 1
    assert data["bar_count"] == DAYS
    assert data["calendar_days"] == DAYS
    assert data["data_start"] == "2024-01-01"
    assert data["checks"][0]["key"] == "gap"


def test_sync_config_normalizes_and_validates_codes() -> None:
    """修复要按标的列表重下，代码必须是数字，重复项要合并。"""

    config = MarketSyncConfig(codes=("600000", " 600000 ", "000001"))
    assert config.codes == ("600000", "000001")
    with pytest.raises(ValueError, match="只能是数字"):
        MarketSyncConfig(codes=("600000", "sh600000"))


def test_restrict_to_codes_keeps_only_the_requested_instruments() -> None:
    selection = _TargetSelection(
        [
            ("SH", "600000", "stock", "main", "甲"),
            ("SZ", "000001", "stock", "main", "乙"),
        ],
        listed_instruments=2,
        filtered_instruments=0,
        target_source="stored",
    )

    assert _restrict_to_codes(selection, ()) is selection
    kept = _restrict_to_codes(selection, ("000001",))
    assert kept.targets == [("SZ", "000001", "stock", "main", "乙")]
    # 被筛掉的必须记进 filtered，否则报告里的"候选数"会和实际下载数对不上。
    assert kept.filtered_instruments == 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, CHECK_KEYS),
        (["gap"], ("gap",)),
        ("price_jump", ("price_jump",)),
        (["gap", "不存在的检查"], ("gap",)),
        (["GAP"], ("gap",)),
    ],
)
def test_normalize_keys(raw: object, expected: tuple[str, ...]) -> None:
    assert normalize_keys(raw) == expected
