"""行情健康检查的判定测试。

体检的卖点是「把静默的问题显式化」，所以每条断言都钉在**能不能看见**上：
有问题就必须被点名，没问题就必须显示 ok。阈值与口径本身写在
``aquest/market_data/health.py`` 的模块注释里，改口径要同步改那里。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from aquest.adapters.market_sync import MarketSyncConfig
from aquest.adapters.online_market_sync import _restrict_to_codes, _TargetSelection
from aquest.market_data.adjustment import CorporateAction
from aquest.market_data.health import CHECK_KEYS, normalize_keys
from aquest.market_data.store import DuckDbMarketDataStore

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
    store.replace_local_bars("SH", "600666", "stock", crashed(bars(), 20), name="疑似漏记")

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
    # 跳变必须砸在**最后一根** bar 上：单日暴跌之后必然跟着一根回到基准的
    # 补偿性大涨，那也是无事件解释的超限跳变、也会被抓 —— 到时候这里就是
    # 2 次而不是 1 次，断言会莫名失败。砸在末尾就没有次日了。
    store.replace_local_bars("SH", "600666", "stock", crashed(bars(), 59), name="腰斩")
    checks = check_by_key(store)
    assert checks["missing_action"].occurrences == 1
    # 同一次腰斩在 price_jump 里也是超限跳变（主板 -50% 远超 11%）：事件一旦
    # 补上，两边必须同时闭嘴 —— 一条记录只该解释一次。
    assert checks["price_jump"].occurrences == 1

    store.upsert_corporate_actions(
        [
            CorporateAction(
                market="SH",
                code="600666",
                ex_date=date(2024, 2, 29),
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
    assert check_by_key(store)["price_jump"].occurrences == 0


def test_price_jump_thresholds_follow_board_limits(tmp_path: Path) -> None:
    """阈值必须跟着板块走：主板 11%、创业板 21%。

    两条断言各钉死一半参数顺序：主板 +15% 在旧代码里会被漏掉（主板误配了
    创业板的 21%），创业板 +19% 在旧代码里会被误报（创业板误配了主板的
    11%）—— 真实库上 49,324 条误报里 46,594 条来自后者。
    """

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", bars(), name="基准")
    # 同样砸在最后一根 bar：跳变日之后的补偿回落本身也是超限跳变，会多出
    # 一条把「affected == 1」的断言打穿。
    store.replace_local_bars(
        "SH", "600666", "stock", crashed(bars(), 59, ratio=1.15), name="主板跳涨"
    )
    store.replace_local_bars(
        "SZ", "300750", "stock", crashed(bars(), 59, ratio=1.19), name="创业涨停内", board="chinext"
    )

    item = check_by_key(store)["price_jump"]
    assert item.affected_instruments == 1
    assert item.occurrences == 1
    assert (item.examples[0].market, item.examples[0].code) == ("SH", "600666")
    assert "15.0%" in item.examples[0].detail


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


def _with_key(frame: pd.DataFrame, market: str, code: str) -> pd.DataFrame:
    """把单标的帧补成 ``upsert_local_bars`` 需要的多标的帧。"""

    filled = frame.copy()
    filled["market"] = market
    filled["code"] = code
    filled["instrument_type"] = "stock"
    return filled


def test_other_price_basis_does_not_create_a_price_jump(tmp_path: Path) -> None:
    """体检只能看研究读路径的那个口径：跨口径去重会凭空造出超限跳变。

    raw 恒定 10.00、另有同日 hfq 行 13.00 时，按 ``imported_at`` 取最新一行的去重
    会把那根 hfq 插进原始价序列里，报成 30% 跳变。而 ``read_bars`` 只回 raw，
    研究侧根本看不到它——体检的结论是修复（删掉重下）的输入，误报会删掉好数据。
    """

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    flat = bars()
    for column in ("open", "high", "low", "close"):
        flat[column] = 10.0
    store.replace_local_bars("SH", "600000", "stock", flat, name="原始价")

    hfq = flat.copy()
    for column in ("open", "high", "low", "close"):
        hfq.loc[hfq.index[10], column] = 13.0
    store.upsert_local_bars(_with_key(hfq, "SH", "600000"), price_basis="hfq")

    checks = check_by_key(store)
    assert checks["price_jump"].severity == "ok"
    assert store.health.codes_for(("price_jump",)) == ()
    # 研究读路径确实只有 raw 那一套价。
    assert set(store.read_bars("SH", "600000")["close"]) == {10.0}


def test_codes_without_bars_ignores_other_price_basis(tmp_path: Path) -> None:
    """只剩其它口径的残留行，等于研究视图里没有行情，必须继续报成「没有数据」。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", bars(), name="原始价")
    store.upsert_online_bars(_with_key(bars(), "SZ", "000001"))
    store.upsert_local_bars(_with_key(bars(), "SZ", "000001"), price_basis="hfq")

    assert store.health.codes_without_bars(("600000", "000001")) == ()
    # 只删在线写入的 raw 行，hfq 残留行留在库里。
    store.delete_bars(source="online", codes=["000001"])
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
