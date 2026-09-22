"""行情健康检查的判定测试。

体检的卖点是「把静默的问题显式化」，所以每条断言都钉在**能不能看见**上：
有问题就必须被点名，没问题就必须显示 ok。阈值与口径本身写在
``selector_app/market_data/health.py`` 的模块注释里，改口径要同步改那里。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from selector_app.adapters.market_sync import MarketSyncConfig
from selector_app.adapters.online_market_sync import _restrict_to_codes, _TargetSelection
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
    """三只票、同样的日期、没有异常 → 六项检查必须全部 ok。"""

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
