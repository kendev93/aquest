from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from selector_app.adapters.local_day_adapter import LocalDayMarketDataAdapter, StockRef
from tests.day_helpers import write_day_records


def touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")


def test_adapter_lists_all_known_shenzhen_and_shanghai_day_files(tmp_path: Path) -> None:
    vipdoc = tmp_path / "vipdoc"
    for filename in (
        "sh600000.day",
        "sh688001.day",
        "sh510300.day",  # ETF
        "sh000001.day",  # 指数
        "sz000001.day",
        "sz300001.day",
        "sz159919.day",  # ETF
        "sz399001.day",  # 指数
    ):
        touch(vipdoc / ("sh" if filename.startswith("sh") else "sz") / "lday" / filename)

    refs = LocalDayMarketDataAdapter().list_stock_refs(str(vipdoc), "all")

    # 基金与指数的文件依然存在，但不再被列举：本系统只研究股票。
    assert [(ref.market, ref.code, ref.instrument_type) for ref in refs] == [
        ("SH", "600000", "stock"),
        ("SH", "688001", "stock"),
        ("SZ", "000001", "stock"),
        ("SZ", "300001", "stock"),
    ]


def test_adapter_excludes_provisional_current_day_before_close(tmp_path: Path) -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    filepath = tmp_path / "sh/lday/sh600000.day"
    filepath.parent.mkdir(parents=True)
    write_day_records(
        filepath,
        [
            (20260825, 10.0, 10.4, 9.8, 10.2, 100_000, 1_000_000),
            (20260826, 10.2, 10.6, 10.0, 10.5, 110_000, 1_100_000),
        ],
    )
    ref = StockRef(market="SH", code="600000", path=filepath)

    before_close = LocalDayMarketDataAdapter(
        clock=lambda: datetime(2026, 8, 26, 10, 0, tzinfo=timezone),
    ).read_stock(ref)
    after_close = LocalDayMarketDataAdapter(
        clock=lambda: datetime(2026, 8, 26, 16, 0, tzinfo=timezone),
    ).read_stock(ref)

    assert before_close["date"].dt.strftime("%Y-%m-%d").tolist() == ["2026-08-25"]
    assert after_close["date"].dt.strftime("%Y-%m-%d").tolist() == [
        "2026-08-25",
        "2026-08-26",
    ]


def test_adapter_builds_a_valid_single_stock_reference(tmp_path: Path) -> None:
    vipdoc = tmp_path / "vipdoc"
    vipdoc.mkdir()

    ref = LocalDayMarketDataAdapter().stock_ref(vipdoc, "SH", "600000")

    assert ref.market == "SH"
    assert ref.code == "600000"
    assert ref.path == vipdoc / "sh/lday/sh600000.day"
