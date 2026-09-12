from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from selector_app.adapters.akshare_market_sync import (
    AkShareMarketSync,
    AkShareProviderError,
    _ExistingBars,
    _requested_ranges,
)
from selector_app.adapters.market_data_sync import MarketDataSync
from selector_app.adapters.market_sync import MarketDataCancelledError, MarketSyncConfig
from selector_app.market_data.staging import StageTarget
from selector_app.market_data.store import DuckDbMarketDataStore

_NOW = datetime(2026, 9, 12, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def _hist_frame(close: float = 10.0) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "日期": pd.to_datetime(["2026-09-08", "2026-09-09", "2026-09-10"]),
            "开盘": [close, close + 0.1, close + 0.2],
            "收盘": [close + 0.1, close + 0.2, close + 0.4],
            "最高": [close + 0.2, close + 0.3, close + 0.5],
            "最低": [close - 0.2, close - 0.1, close],
            "成交量": [1000.0, 1100.0, 1200.0],
            "成交额": [10000.0, 11000.0, 12000.0],
        }
    )


def _hist_frame_for_dates(days: list[str]) -> pd.DataFrame:
    close = [10.0 + index * 0.1 for index in range(len(days))]
    return pd.DataFrame(
        {
            "日期": pd.to_datetime(days),
            "开盘": close,
            "收盘": [value + 0.1 for value in close],
            "最高": [value + 0.2 for value in close],
            "最低": [value - 0.2 for value in close],
            "成交量": [1000.0 + index * 100 for index in range(len(days))],
            "成交额": [10000.0 + index * 1000 for index in range(len(days))],
        }
    )


class FakeAkShare:
    def __init__(self) -> None:
        self.history_requests: list[dict[str, object]] = []
        self.history_failures = 0

    def stock_info_a_code_name(self) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {"代码": "600000", "名称": "浦发银行"},
                {"代码": "000001", "名称": "平安银行"},
                {"代码": "688001", "名称": "科创样本"},
                {"代码": "830001", "名称": "北交样本"},
                {"代码": "bad", "名称": "无效代码"},
            ]
        )

    def stock_zh_a_hist(self, **kwargs: object) -> pd.DataFrame:
        self.history_requests.append(dict(kwargs))
        if self.history_failures:
            self.history_failures -= 1
            raise RuntimeError("upstream temporarily unavailable")
        return _hist_frame()


class FallbackAkShare(FakeAkShare):
    def stock_zh_a_hist(self, **kwargs: object) -> pd.DataFrame:
        del kwargs
        raise RuntimeError("eastmoney unavailable")

    def stock_zh_a_hist_tx(self, **kwargs: object) -> pd.DataFrame:
        self.history_requests.append(dict(kwargs))
        frame = _hist_frame().rename(
            columns={
                "日期": "date",
                "开盘": "open",
                "收盘": "close",
                "最高": "high",
                "最低": "low",
                "成交量": "volume",
                "成交额": "amount",
            }
        )
        return frame


def _config(**overrides: object) -> MarketSyncConfig:
    values: dict[str, object] = {
        "universe": "all",
        "instrument_types": ("stock",),
        "sync_workers": 1,
    }
    values.update(overrides)
    return MarketSyncConfig(**values)


def test_akshare_discovers_sh_sz_a_shares_and_preserves_names(tmp_path: Path) -> None:
    module = FakeAkShare()
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")

    report = AkShareMarketSync(
        store=store,
        module_factory=lambda: module,
        clock=lambda: _NOW,
    ).discover_instruments(_config())

    assert report.target_source == "akshare_stock_info_a_code_name"
    assert report.listed_instruments == 3
    assert report.total_candidates == 3
    assert report.filtered_instruments == 0
    assert {ref.code for ref in store.list_instruments()} == {"000001", "600000", "688001"}
    assert next(ref for ref in store.list_instruments() if ref.code == "600000").name == "浦发银行"


def test_akshare_sync_uses_full_history_for_new_instruments_and_writes_provider(
    tmp_path: Path,
) -> None:
    module = FakeAkShare()
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    service = AkShareMarketSync(
        store=store,
        module_factory=lambda: module,
        clock=lambda: _NOW,
    )

    service.discover_instruments(_config())
    report = service.sync(_config(prefer_store_targets=True))

    assert report.provider == "akshare"
    assert report.processed == 3
    assert report.written_bars == 9
    assert {str(value) for value in store.read_bars("SH", "600000")["provider"]} == {"akshare"}
    assert (
        next(ref for ref in store.list_instruments() if ref.code == "600000").provider == "akshare"
    )
    assert store.status().last_online_provider == "akshare"
    assert module.history_requests
    assert all(request["start_date"] == "19900101" for request in module.history_requests)
    assert all(request["end_date"] == "20260912" for request in module.history_requests)
    assert all(request["adjust"] == "" for request in module.history_requests)


def test_akshare_sync_retries_a_transient_symbol_failure(tmp_path: Path) -> None:
    module = FakeAkShare()
    module.history_failures = 1
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    service = AkShareMarketSync(
        store=store,
        module_factory=lambda: module,
        clock=lambda: _NOW,
        sleep=lambda _seconds: None,
    )

    service.discover_instruments(_config())
    report = service.sync(_config(prefer_store_targets=True))

    assert report.errors == 0
    assert len(module.history_requests) == 4


def test_akshare_falls_back_to_tencent_daily_history(tmp_path: Path) -> None:
    module = FallbackAkShare()
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    service = AkShareMarketSync(
        store=store,
        module_factory=lambda: module,
        clock=lambda: _NOW,
        sleep=lambda _seconds: None,
    )

    service.discover_instruments(_config())
    report = service.sync(_config(prefer_store_targets=True))

    assert report.errors == 0
    assert report.written_bars == 9
    assert report.history_sources == {"stock_zh_a_hist_tx": 3}
    assert all(
        request["symbol"] in {"sh600000", "sz000001", "sh688001"}
        for request in module.history_requests
    )


def test_akshare_uses_sina_only_after_tencent_fails(tmp_path: Path) -> None:
    class SinaFallbackAkShare(FakeAkShare):
        def stock_zh_a_hist(self, **kwargs: object) -> pd.DataFrame:
            del kwargs
            raise RuntimeError("eastmoney unavailable")

        def stock_zh_a_hist_tx(self, **kwargs: object) -> pd.DataFrame:
            del kwargs
            raise RuntimeError("tencent unavailable")

        def stock_zh_a_daily(self, **kwargs: object) -> pd.DataFrame:
            self.history_requests.append(dict(kwargs))
            return _hist_frame().rename(
                columns={
                    "日期": "date",
                    "开盘": "open",
                    "收盘": "close",
                    "最高": "high",
                    "最低": "low",
                    "成交量": "volume",
                    "成交额": "amount",
                }
            )

    module = SinaFallbackAkShare()
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    service = AkShareMarketSync(
        store=store,
        module_factory=lambda: module,
        clock=lambda: _NOW,
        sleep=lambda _seconds: None,
    )

    service.discover_instruments(_config())
    report = service.sync(_config(prefer_store_targets=True))

    assert report.errors == 0
    assert report.history_sources == {"stock_zh_a_daily": 3}
    assert set(store.read_bars("SH", "600000")["volume"]) == {10.0, 11.0, 12.0}


def test_akshare_full_history_fetches_only_missing_ranges_for_existing_rows(
    tmp_path: Path,
) -> None:
    class RangeAwareAkShare(FakeAkShare):
        def stock_info_a_code_name(self) -> pd.DataFrame:
            return pd.DataFrame([{"代码": "600000", "名称": "浦发银行"}])

        def stock_zh_a_hist(self, **kwargs: object) -> pd.DataFrame:
            self.history_requests.append(dict(kwargs))
            start = pd.Timestamp(str(kwargs["start_date"])).date()
            end = pd.Timestamp(str(kwargs["end_date"])).date()
            available = [
                day
                for day in pd.date_range("2026-09-07", "2026-09-11", freq="D").date
                if start <= day <= end
            ]
            return _hist_frame_for_dates([day.isoformat() for day in available])

    module = RangeAwareAkShare()
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    service = AkShareMarketSync(
        store=store,
        module_factory=lambda: module,
        clock=lambda: _NOW,
    )

    service.discover_instruments(_config())
    existing = _hist_frame_for_dates(["2026-09-08", "2026-09-09", "2026-09-10"]).rename(
        columns={
            "日期": "date",
            "开盘": "open",
            "收盘": "close",
            "最高": "high",
            "最低": "low",
            "成交量": "volume",
            "成交额": "amount",
        }
    )
    existing["bar_status"] = "completed"
    store.replace_local_bars(
        "SH",
        "600000",
        "stock",
        existing,
    )
    module.history_requests.clear()
    report = service.sync(_config(prefer_store_targets=True, full_history=True))

    assert {
        (request["start_date"], request["end_date"]) for request in module.history_requests
    } == {
        ("19900101", "20260907"),
        ("20260911", "20260912"),
    }
    assert report.written_bars == 2
    assert list(store.read_bars("SH", "600000")["date"].dt.strftime("%Y-%m-%d")) == [
        "2026-09-07",
        "2026-09-08",
        "2026-09-09",
        "2026-09-10",
        "2026-09-11",
    ]


def test_akshare_full_history_skips_a_target_covering_the_requested_window() -> None:
    existing = _ExistingBars(
        pd.DataFrame(),
        first_date=pd.Timestamp("1990-01-01").date(),
        last_date=pd.Timestamp("2026-09-12").date(),
        bar_count=10_000,
    )

    assert (
        _requested_ranges(
            existing,
            _config(full_history=True),
            pd.Timestamp("2026-09-12").date(),
        )
        == []
    )


def test_akshare_stage_fetch_normalizes_without_writing_main_store(tmp_path: Path) -> None:
    module = FakeAkShare()
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    service = AkShareMarketSync(
        store=store,
        module_factory=lambda: module,
        clock=lambda: _NOW,
        sleep=lambda _seconds: None,
    )
    target = StageTarget(
        provider="akshare",
        market="SH",
        code="600000",
        instrument_type="stock",
        board="main",
        name="浦发银行",
        requested_ranges=((date(1990, 1, 1), date(2026, 9, 12)),),
        existing_first_date=None,
        existing_last_date=None,
        existing_bar_count=0,
        existing_latest_rows=(),
    )

    result = service.fetch_stage_target(target, _config())

    assert result.status == "updated"
    assert len(result.frame) == 3
    assert store.status().bar_count == 0


def test_akshare_provider_rejects_invalid_ohlc_rows(tmp_path: Path) -> None:
    class InvalidAkShare(FakeAkShare):
        def stock_zh_a_hist(self, **kwargs: object) -> pd.DataFrame:
            del kwargs
            frame = _hist_frame()
            frame.loc[0, "最高"] = 1.0
            return frame

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    service = AkShareMarketSync(
        store=store,
        module_factory=InvalidAkShare,
        clock=lambda: _NOW,
        sleep=lambda _seconds: None,
    )
    service.discover_instruments(_config())

    report = service.sync(_config(prefer_store_targets=True))

    assert report.processed == 3
    assert report.errors == 3
    assert report.written_bars == 0
    assert not store.read_bars("SH", "600000").any(axis=None)


def test_akshare_empty_history_is_reported_as_a_symbol_error(tmp_path: Path) -> None:
    class EmptyHistoryAkShare(FakeAkShare):
        def stock_zh_a_hist(self, **kwargs: object) -> pd.DataFrame:
            del kwargs
            return pd.DataFrame()

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    service = AkShareMarketSync(
        store=store,
        module_factory=EmptyHistoryAkShare,
        clock=lambda: _NOW,
        sleep=lambda _seconds: None,
    )
    service.discover_instruments(_config())

    report = service.sync(_config(prefer_store_targets=True))

    assert report.errors == 3
    assert report.unchanged_files == 0
    assert any("没有可用日线" in reason for reason in report.failure_reasons)


def test_akshare_sync_can_be_cancelled_before_fetching_bars(tmp_path: Path) -> None:
    module = FakeAkShare()
    service = AkShareMarketSync(
        store=DuckDbMarketDataStore(tmp_path / "market.duckdb"),
        module_factory=lambda: module,
        clock=lambda: _NOW,
        sleep=lambda _seconds: None,
    )

    with pytest.raises(MarketDataCancelledError, match="取消"):
        service.sync(_config(), abort_check=lambda: True)

    assert module.history_requests == []


def test_market_data_sync_defaults_to_akshare_without_calling_tdx(
    tmp_path: Path,
) -> None:
    akshare = FakeAkShare()
    calls: list[str] = []

    class FakeTdx:
        def discover_instruments(self, config: MarketSyncConfig, *, detail_callback=None):
            calls.append(f"discover:{config.instrument_types}")
            return {
                "source": "online_discovery",
                "total_candidates": 1,
                "listed_instruments": 1,
                "filtered_instruments": 0,
                "new_instruments": 0,
                "existing_instruments": 1,
                "target_source": "tdx_security_list",
            }

        def sync(self, config: MarketSyncConfig, progress_callback=None, *, detail_callback=None):
            calls.append(f"sync:{config.instrument_types}")
            return {
                "source": "online",
                "provider": "tdx",
                "total_candidates": 1,
                "processed": 1,
                "updated_files": 0,
                "unchanged_files": 1,
                "written_bars": 0,
                "errors": 0,
                "failure_reasons": {},
            }

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    service = MarketDataSync(
        store=store,
        akshare_module_factory=lambda: akshare,
        tdx_sync=FakeTdx(),
    )

    report = service.discover_instruments(MarketSyncConfig(sync_workers=1))

    assert report.provider == "akshare"
    assert calls == []
    assert report.listed_instruments == 3

    sync_report = service.sync(MarketSyncConfig(sync_workers=1))

    assert sync_report.provider == "akshare"
    assert store.get_meta("last_online_provider") == "akshare"


def test_market_data_sync_can_use_legacy_tdx_explicitly(tmp_path: Path) -> None:
    calls: list[str] = []

    class FakeTdx:
        def discover_instruments(self, config: MarketSyncConfig, *, detail_callback=None):
            del detail_callback
            calls.append(f"discover:{config.provider}")
            return {
                "source": "online_discovery",
                "total_candidates": 1,
                "listed_instruments": 1,
                "filtered_instruments": 0,
                "new_instruments": 0,
                "existing_instruments": 1,
                "target_source": "tdx_security_list",
            }

        def sync(self, config: MarketSyncConfig, progress_callback=None, *, detail_callback=None):
            del config, progress_callback, detail_callback
            return {
                "source": "online",
                "provider": "tdx",
                "total_candidates": 1,
                "processed": 1,
                "updated_files": 0,
                "unchanged_files": 1,
                "written_bars": 0,
                "errors": 0,
                "failure_reasons": {},
            }

    service = MarketDataSync(
        store=DuckDbMarketDataStore(tmp_path / "market.duckdb"),
        tdx_sync=FakeTdx(),
    )

    report = service.discover_instruments(MarketSyncConfig(provider="tdx", sync_workers=1))

    assert report.provider == "tdx"
    assert calls == ["discover:tdx"]


def test_akshare_mode_rejects_explicitly_unsupported_instrument_scope(tmp_path: Path) -> None:
    service = MarketDataSync(store=DuckDbMarketDataStore(tmp_path / "market.duckdb"))

    with pytest.raises(AkShareProviderError, match="TDX"):
        service.discover_instruments(MarketSyncConfig(instrument_types=("fund",), sync_workers=1))


def test_market_sync_config_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="行情数据源"):
        MarketSyncConfig(provider="unknown")  # type: ignore[arg-type]
