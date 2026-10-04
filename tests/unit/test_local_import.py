"""Local TongDaXin day-file import into the DuckDB store."""

from __future__ import annotations

import struct
from pathlib import Path

import pandas as pd
import pytest

from selector_app.market_data import local_adapter
from selector_app.market_data.local_adapter import detect_vipdoc_roots
from selector_app.market_data.local_import import (
    LocalDayImportError,
    LocalDayImportService,
    _ParsedFile,
)
from selector_app.market_data.store import DuckDbMarketDataStore

_DAY_RECORD = struct.Struct("<IIIIIfII")


def _write_day_file(path: Path, rows: list[tuple[int, int, int, int, int, float, int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        for date_int, opening, high, low, close, amount, volume in rows:
            handle.write(_DAY_RECORD.pack(date_int, opening, high, low, close, amount, volume, 0))


def _online_frame(market: str, code: str, days: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "market": market,
                "code": code,
                "instrument_type": "stock",
                "board": "main",
                "date": pd.Timestamp(day),
                "open": 9.0,
                "high": 9.5,
                "low": 8.5,
                "close": 9.2,
                "volume": 100.0,
                "amount": 1000.0,
                "bar_status": "completed",
            }
            for day in days
        ]
    )


def _bars(store: DuckDbMarketDataStore, market: str, code: str) -> pd.DataFrame:
    # vipdoc 导入落的是不复权 raw 行，与研究默认口径一致；显式写出来是为了让
    # "这个 helper 只关心本地导入结果"这件事在阅读时一眼可见。
    return store.read_bars(market, code, price_basis="raw")


def test_import_writes_day_file_bars(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    vipdoc = tmp_path / "vipdoc"
    _write_day_file(
        vipdoc / "sh" / "lday" / "sh600000.day",
        [(20260915, 800, 810, 790, 805, 1.0e8, 1_000_000)],
    )

    report = LocalDayImportService(store, max_workers=1).import_vipdoc(vipdoc)

    assert report["discovered_files"] == 1
    assert report["imported_bars"] == 1
    assert report["replaced_bars"] == 0
    assert report["range_start"] == "2026-09-15"
    bars = _bars(store, "SH", "600000")
    assert len(bars) == 1
    assert bars.iloc[0]["close"] == pytest.approx(8.05)
    assert bars.iloc[0]["source"] == "local"
    assert store.status().instrument_count == 1


def test_import_replaces_same_day_online_rows_and_keeps_the_rest(tmp_path: Path) -> None:
    """vipdoc 导入与在线行情同口径（都是 raw），同一天以本地行为准。

    口径统一成 raw 之后这才是真实场景：唯一键是
    ``(market, code, trade_date, price_basis)``，同口径的同一天就是同一行，本地导入
    必须能替换在线行；本地没覆盖到的那天仍然保留在线数据，不能整段覆盖掉。
    """

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.upsert_online_bars(
        _online_frame("SH", "600000", ["2026-09-16", "2026-09-17", "2026-09-18"]),
        provider="eastmoney",
    )
    vipdoc = tmp_path / "vipdoc"
    _write_day_file(
        vipdoc / "sh" / "lday" / "sh600000.day",
        [
            (20260916, 820, 830, 810, 825, 2.0e8, 2_000_000),
            (20260917, 830, 840, 820, 835, 3.0e8, 3_000_000),
        ],
    )

    report = LocalDayImportService(store, max_workers=1).import_vipdoc(vipdoc)

    assert report["imported_bars"] == 2
    assert report["replaced_bars"] == 2
    # 研究视角（默认读取）就是 raw：同一天已换成本地行，没覆盖的那天仍是原来的在线行。
    bars = store.read_bars("SH", "600000")
    assert len(bars) == 3
    assert bars["source"].tolist() == ["local", "local", "online"]
    assert bars.iloc[0]["close"] == pytest.approx(8.25)


def test_import_respects_type_and_board_scope(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    vipdoc = tmp_path / "vipdoc"
    _write_day_file(
        vipdoc / "sh" / "lday" / "sh600000.day",
        [(20260915, 800, 810, 790, 805, 1.0e8, 1_000_000)],
    )
    _write_day_file(
        vipdoc / "sh" / "lday" / "sh510300.day",
        [(20260915, 300, 310, 290, 305, 1.0e7, 500_000)],
    )

    report = LocalDayImportService(store, max_workers=1).import_vipdoc(
        vipdoc, instrument_types=["stock"]
    )

    assert report["selected_files"] == 1
    assert report["filtered_files"] == 1
    assert len(_bars(store, "SH", "600000")) == 1
    assert _bars(store, "SH", "510300").empty


def test_import_rejects_missing_directory(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    service = LocalDayImportService(store, max_workers=1)
    with pytest.raises(LocalDayImportError):
        service.import_vipdoc(tmp_path / "missing")


def test_import_reports_progress_and_detail(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    vipdoc = tmp_path / "vipdoc"
    _write_day_file(
        vipdoc / "sh" / "lday" / "sh600000.day",
        [(20260915, 800, 810, 790, 805, 1.0e8, 1_000_000)],
    )
    progress: list[tuple[int, int]] = []
    details: list[dict[str, object]] = []

    LocalDayImportService(store, max_workers=1).import_vipdoc(
        vipdoc,
        progress_callback=lambda done, total: progress.append((done, total)),
        detail_callback=details.append,
    )

    assert progress[-1] == (1, 1)
    assert details
    assert details[0]["current_instrument"] == {"market": "SH", "code": "600000"}


def test_detect_finds_nested_vipdoc_directory(tmp_path: Path) -> None:
    vipdoc = tmp_path / "Tdx" / "vipdoc"
    _write_day_file(
        vipdoc / "sh" / "lday" / "sh600000.day",
        [(20260915, 800, 810, 790, 805, 1.0e8, 1_000_000)],
    )
    _write_day_file(
        vipdoc / "sz" / "lday" / "sz000001.day",
        [(20260915, 800, 810, 790, 805, 1.0e8, 1_000_000)],
    )

    found = detect_vipdoc_roots([tmp_path])

    assert [item.path for item in found] == [str(vipdoc)]
    assert found[0].file_count == 2
    assert found[0].markets == ("sh", "sz")


def test_detect_accepts_root_that_is_already_a_vipdoc(tmp_path: Path) -> None:
    _write_day_file(
        tmp_path / "sh" / "lday" / "sh600000.day",
        [(20260915, 800, 810, 790, 805, 1.0e8, 1_000_000)],
    )

    found = detect_vipdoc_roots([tmp_path])

    assert found[0].path == str(tmp_path)
    assert found[0].markets == ("sh",)


def test_detect_returns_nothing_when_no_day_files(tmp_path: Path) -> None:
    (tmp_path / "vipdoc" / "sh" / "lday").mkdir(parents=True)

    assert detect_vipdoc_roots([tmp_path]) == []


def test_map_host_path_rewrites_only_unreachable_host_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_root = tmp_path / "host"
    vipdoc = host_root / "Tdx" / "vipdoc"
    _write_day_file(
        vipdoc / "sh" / "lday" / "sh600000.day",
        [(20260915, 800, 810, 790, 805, 1.0e8, 1_000_000)],
    )
    monkeypatch.setenv("SELECTOR_HOST_MOUNT_PREFIX", "/Users/me")
    monkeypatch.setattr(local_adapter, "_HOST_MOUNT_POINT", str(host_root))

    mapped = local_adapter.map_host_path("/Users/me/Tdx/vipdoc")

    assert mapped == str(vipdoc)


def test_map_host_path_keeps_reachable_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SELECTOR_HOST_MOUNT_PREFIX", "/Users/me")
    monkeypatch.setattr(local_adapter, "_HOST_MOUNT_POINT", "/host")

    assert local_adapter.map_host_path(str(tmp_path)) == str(tmp_path)


def test_map_host_path_ignores_unprefixed_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SELECTOR_HOST_MOUNT_PREFIX", "/Users/me")
    monkeypatch.setattr(local_adapter, "_HOST_MOUNT_POINT", "/host")

    missing = tmp_path / "elsewhere"
    assert local_adapter.map_host_path(str(missing)) == str(missing)


def test_import_error_mentions_container_mount(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")

    with pytest.raises(LocalDayImportError, match="容器"):
        LocalDayImportService(store, max_workers=1).import_vipdoc(tmp_path / "missing")


def _tree_with(tmp_path: Path, count: int, day: int = 20260915) -> Path:
    vipdoc = tmp_path / "vipdoc"
    for index in range(count):
        code = 600000 + index
        _write_day_file(
            vipdoc / "sh" / "lday" / f"sh{code}.day",
            [(day, 800, 810, 790, 805, 1.0e8, 1_000_000)],
        )
    return vipdoc


def test_import_stops_parsing_after_abort(tmp_path: Path) -> None:
    """取消必须真的停：旧实现退出时 wait=True，排队的文件照样全部解析完。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    vipdoc = _tree_with(tmp_path, 40)
    seen: list[str] = []

    def abort_check() -> bool:
        return len(seen) >= 3

    service = LocalDayImportService(store, max_workers=1)
    original_parse = service._parse

    def counting_parse(ref):  # type: ignore[no-untyped-def]
        seen.append(ref.code)
        return original_parse(ref)

    service._parse = counting_parse  # type: ignore[method-assign]
    with pytest.raises(Exception, match="取消"):
        service.import_vipdoc(vipdoc, abort_check=abort_check)

    assert len(seen) < 40


def test_import_keeps_going_when_one_frame_has_bad_numbers(tmp_path: Path) -> None:
    """一个含 NaN 的标的只能废掉它自己，不能毁掉整次导入。

    旧实现里 NaN 会一路带到 flush()，store 抛 ValueError("行情数据包含无效数值")，
    而 flush() 在每文件 try/except 之外 ⇒ 40 个文件坏 1 个就是全部失败，
    且超过 64 个文件时前面的批次已经提交了。
    """

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    vipdoc = _tree_with(tmp_path, 3)
    service = LocalDayImportService(store, max_workers=1)
    original_parse = service._parse

    def parse(ref):  # type: ignore[no-untyped-def]
        parsed = original_parse(ref)
        if ref.code == "600001":
            frame = parsed.frame.copy()
            frame.loc[0, "close"] = float("nan")
            return _ParsedFile(ref=ref, frame=frame)
        return parsed

    service._parse = parse  # type: ignore[method-assign]
    report = service.import_vipdoc(vipdoc)

    assert report["imported_instruments"] == 2
    assert report["failure_reasons"].get("day_file_has_invalid_numbers") == 1
    assert len(_bars(store, "SH", "600000")) == 1
    assert len(_bars(store, "SH", "600002")) == 1


def test_import_reports_a_truncated_file(tmp_path: Path) -> None:
    """尾部不足一条记录说明文件没下载完，必须出现在报告里而不是静默丢弃。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    vipdoc = _tree_with(tmp_path, 1)
    target = vipdoc / "sh" / "lday" / "sh600000.day"
    with target.open("ab") as handle:
        handle.write(b"broken")

    report = LocalDayImportService(store, max_workers=1).import_vipdoc(vipdoc)

    assert report["failure_reasons"].get("day_file_truncated") == 1
    assert report["imported_bars"] == 1


def test_import_report_carries_panel_keys(tmp_path: Path) -> None:
    """结果面板读的是在线同步那套键名，本地导入必须也能对上。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    vipdoc = _tree_with(tmp_path, 2)

    report = LocalDayImportService(store, max_workers=1).import_vipdoc(vipdoc)

    assert report["written_bars"] == report["imported_bars"] == 2
    assert report["updated_files"] == report["imported_instruments"] == 2
    assert report["actual_start"] == report["range_start"] == "2026-09-15"
