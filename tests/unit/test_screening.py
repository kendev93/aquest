from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from selector_app.adapters.local_day_adapter import StockRef
from selector_app.screening.engine import ScreenEngine, combine_matches
from selector_app.screening.models import ScanConfig


def make_bars(length: int = 160) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=length, freq="D")
    close = np.full(length, 1.2)
    return pd.DataFrame(
        {
            "date": dates,
            "open": close,
            "high": np.full(length, 10.0),
            "low": np.full(length, 1.0),
            "close": close,
            "volume": np.full(length, 100_000.0),
            "amount": close * 100_000,
        }
    )


class FakeAdapter:
    def __init__(
        self,
        refs: list[StockRef],
        frames: dict[str, pd.DataFrame],
        failures: set[str] | None = None,
    ):
        self.refs = refs
        self.frames = frames
        self.failures = failures or set()

    def list_stock_refs(self, vipdoc_path: str, universe: str, universe_file: str | None = None):
        if universe == "custom":
            return self.refs[:1]
        if universe == "sh":
            return [ref for ref in self.refs if ref.market == "SH"]
        if universe == "sz":
            return [ref for ref in self.refs if ref.market == "SZ"]
        return list(self.refs)

    def read_stock(self, ref: StockRef) -> pd.DataFrame:
        if ref.code in self.failures:
            raise ValueError("坏数据")
        return self.frames[ref.code].copy(deep=True)


def config(*signals: str, mode: str = "at_least", minimum: int | None = None) -> ScanConfig:
    effective_minimum = 1 if mode == "at_least" and minimum is None else minimum
    return ScanConfig(
        selected_signals=tuple(signals),
        combine_mode=mode,
        minimum_matches=effective_minimum,
        universe="all",
        universe_file=None,
        vipdoc_path="/tmp/vipdoc",
        workers=1,
        period="daily",
    )


def test_combine_modes_and_minimum_bounds() -> None:
    assert combine_matches([True, True], "all", None) is True
    assert combine_matches([True, False], "all", None) is False
    assert combine_matches([False, True], "any", None) is True
    assert combine_matches([False, False], "any", None) is False
    assert combine_matches([True, False, True], "at_least", 2) is True
    assert combine_matches([True, False, False], "at_least", 2) is False


def test_engine_scans_multiple_stocks_and_keeps_user_facing_result_fields(tmp_path: Path) -> None:
    refs = [
        StockRef(
            market="SH",
            code="600000",
            path=tmp_path / "sh600000.day",
            name="浦发银行",
        ),
        StockRef(market="SZ", code="000001", path=tmp_path / "sz000001.day"),
    ]
    frames = {ref.code: make_bars() for ref in refs}
    engine = ScreenEngine(FakeAdapter(refs, frames))

    report = engine.scan(config("indicator_three.accumulation_zone"))

    assert report.total_candidates == 2
    assert report.total_scanned == 2
    assert report.errors == 0
    assert len(report.results) == 2
    assert report.results[0].market in {"SH", "SZ"}
    assert report.results[0].signal_date == 20240608
    assert report.results[0].last_close == 1.2
    assert report.results[0].name == "浦发银行"
    assert report.results[0].matched_signals == ("indicator_three.accumulation_zone",)
    assert report.results[0].match_count == 1
    assert set(report.results[0].indicator_values) == {
        "indicator_three.var1",
        "indicator_three.var2",
        "indicator_three.prepare_rally",
        "indicator_three.suppress_main",
    }
    assert "indicator_three.varo7" not in report.results[0].indicator_values


def test_engine_continues_after_one_stock_failure_and_reports_reason(tmp_path: Path) -> None:
    refs = [
        StockRef(market="SH", code="600000", path=tmp_path / "sh600000.day"),
        StockRef(market="SZ", code="000001", path=tmp_path / "sz000001.day"),
    ]
    adapter = FakeAdapter(refs, {ref.code: make_bars() for ref in refs}, failures={"000001"})
    report = ScreenEngine(adapter).scan(config("indicator_three.accumulation_zone"))

    assert len(report.results) == 1
    assert report.errors == 1
    assert report.failure_reasons["坏数据"] == 1


def test_engine_skips_insufficient_data_without_counting_as_error(tmp_path: Path) -> None:
    ref = StockRef(market="SH", code="600000", path=tmp_path / "sh600000.day")
    adapter = FakeAdapter([ref], {ref.code: make_bars(10)})

    report = ScreenEngine(adapter).scan(config("indicator_three.accumulation_zone"))

    assert report.results == ()
    assert report.errors == 0
    assert report.skipped == 1
    assert report.skip_reasons["数据不足"] == 1


def test_engine_reports_malformed_market_values_as_one_stock_error(tmp_path: Path) -> None:
    ref = StockRef(market="SH", code="600000", path=tmp_path / "sh600000.day")
    frame = make_bars()
    frame.loc[0, "close"] = np.inf

    report = ScreenEngine(FakeAdapter([ref], {ref.code: frame})).scan(
        config("indicator_three.accumulation_zone")
    )

    assert report.results == ()
    assert report.errors == 1
    assert report.failure_reasons["行情数据包含非有限数值"] == 1


def test_custom_universe_is_passed_to_adapter(tmp_path: Path) -> None:
    ref = StockRef(market="SH", code="600000", path=tmp_path / "sh600000.day")
    adapter = FakeAdapter([ref], {ref.code: make_bars()})

    report = ScreenEngine(adapter).scan(
        ScanConfig(
            selected_signals=("indicator_three.accumulation_zone",),
            combine_mode="any",
            minimum_matches=None,
            universe="custom",
            universe_file=str(tmp_path / "stocks.txt"),
            vipdoc_path=str(tmp_path),
            workers=1,
            period="daily",
        )
    )

    assert report.total_candidates == 1


def test_engine_scans_a_custom_formula_signal(tmp_path: Path) -> None:
    ref = StockRef(market="SH", code="600000", path=tmp_path / "sh600000.day")
    adapter = FakeAdapter([ref], {ref.code: make_bars()})
    custom_config = ScanConfig(
        selected_signals=("custom.accumulation",),
        combine_mode="any",
        minimum_matches=None,
        universe="all",
        universe_file=None,
        vipdoc_path=str(tmp_path),
        workers=1,
        period="daily",
        formula_text="N:=5; ACCUMULATION:IF(C<2,1,0);",
        formula_parameters={"N": 5},
    )

    report = ScreenEngine(adapter).scan(custom_config)

    assert len(report.results) == 1
    assert report.results[0].matched_signals == ("custom.accumulation",)
    assert "custom.ACCUMULATION" in report.results[0].indicator_values
