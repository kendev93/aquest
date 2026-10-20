from __future__ import annotations

import threading
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from aquest.adapters import online_market_sync as online_market_sync_module
from aquest.adapters.baostock_catalog import BaostockCatalogError
from aquest.adapters.market_data_sync import MarketDataSync
from aquest.adapters.market_sync import MarketDataCancelledError, MarketSyncConfig
from aquest.adapters.online_market_sync import (
    OnlineMarketClient,
    OnlineMarketSync,
    _ExistingBars,
    _has_new_data,
    _requested_ranges,
)
from aquest.market_data.staging import (
    ProcessRateGate,
    StageTarget,
    create_rate_state,
)
from aquest.market_data.store import DuckDbMarketDataStore

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


class FakeOnline:
    def __init__(self) -> None:
        self.history_requests: list[dict[str, object]] = []
        self.history_failures = 0

    def fetch_instruments(self, *, timeout: float) -> pd.DataFrame:
        del timeout
        return pd.DataFrame(
            [
                {"代码": "600000", "名称": "浦发银行"},
                {"代码": "000001", "名称": "平安银行"},
                {"代码": "688001", "名称": "科创样本"},
                {"代码": "830001", "名称": "北交样本"},
                {"代码": "900001", "名称": "沪市 B 股"},
                {"代码": "200001", "名称": "深市 B 股"},
                {"代码": "bad", "名称": "无效代码"},
            ]
        )

    def fetch_daily_history(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        *,
        timeout: float,
    ) -> pd.DataFrame:
        self.history_requests.append(
            {
                "symbol": symbol,
                "start_date": start_date,
                "end_date": end_date,
                "timeout": timeout,
            }
        )
        if self.history_failures:
            self.history_failures -= 1
            raise RuntimeError("upstream temporarily unavailable")
        return _hist_frame()


class FallbackOnline(FakeOnline):
    def fetch_daily_history(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        *,
        timeout: float,
    ) -> pd.DataFrame:
        del symbol, start_date, end_date, timeout
        raise RuntimeError("eastmoney unavailable")

    def fetch_tencent_daily_history(
        self,
        market: str,
        code: str,
        start_date: str,
        end_date: str,
        *,
        timeout: float,
    ) -> pd.DataFrame:
        self.history_requests.append(
            {
                "market": market,
                "code": code,
                "start_date": start_date,
                "end_date": end_date,
                "timeout": timeout,
            }
        )
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


class RecordingOnline(FakeOnline):
    """FakeOnline that also records how often the East Money catalog was asked."""

    def __init__(self) -> None:
        super().__init__()
        self.catalog_requests: list[str] = []

    def fetch_instruments(self, *, timeout: float) -> pd.DataFrame:
        self.catalog_requests.append("stock")
        return super().fetch_instruments(timeout=timeout)


class BothSourcesOnline(FakeOnline):
    """East Money and Tencent both answer; Tencent must win."""

    def __init__(self) -> None:
        super().__init__()
        self.tencent_requests: list[dict[str, object]] = []

    def fetch_tencent_daily_history(
        self,
        market: str,
        code: str,
        start_date: str,
        end_date: str,
        *,
        timeout: float,
    ) -> pd.DataFrame:
        self.tencent_requests.append(
            {
                "market": market,
                "code": code,
                "start_date": start_date,
                "end_date": end_date,
                "timeout": timeout,
            }
        )
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


class EmptyTencentOnline(BothSourcesOnline):
    def fetch_tencent_daily_history(
        self,
        market: str,
        code: str,
        start_date: str,
        end_date: str,
        *,
        timeout: float,
    ) -> pd.DataFrame:
        del market, code, start_date, end_date, timeout
        return pd.DataFrame()


class ValidEmptyHistoryOnline(FakeOnline):
    @staticmethod
    def _empty_history(columns: list[str]) -> pd.DataFrame:
        frame = pd.DataFrame(columns=columns)
        frame.attrs["selector_empty_response_status"] = "valid_no_data"
        return frame

    def fetch_tencent_daily_history(
        self,
        market: str,
        code: str,
        start_date: str,
        end_date: str,
        *,
        timeout: float,
    ) -> pd.DataFrame:
        del market, code, start_date, end_date, timeout
        return self._empty_history(
            ["date", "open", "close", "high", "low", "volume", "turnover", "amount"]
        )

    def fetch_daily_history(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        *,
        timeout: float,
    ) -> pd.DataFrame:
        del symbol, start_date, end_date, timeout
        return self._empty_history(
            ["日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额", "换手率"]
        )


def _config(**overrides: object) -> MarketSyncConfig:
    values: dict[str, object] = {
        "universe": "all",
        "instrument_types": ("stock",),
        "sync_workers": 1,
    }
    values.update(overrides)
    return MarketSyncConfig(**values)


def test_eastmoney_discovers_sh_sz_a_shares_and_preserves_names(tmp_path: Path) -> None:
    module = FakeOnline()
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")

    report = OnlineMarketSync(
        store=store,
        module_factory=lambda: module,
        clock=lambda: _NOW,
    ).discover_instruments(_config())

    assert report.target_source == "eastmoney_stock_list"
    assert report.listed_instruments == 3
    assert report.total_candidates == 3
    assert report.filtered_instruments == 0
    assert {ref.code for ref in store.list_instruments()} == {"000001", "600000", "688001"}
    assert next(ref for ref in store.list_instruments() if ref.code == "600000").name == "浦发银行"


def test_eastmoney_unscoped_request_stays_stock_only(tmp_path: Path) -> None:
    """An unscoped request must not silently pull the whole ETF catalog."""

    module = FakeOnline()
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")

    report = OnlineMarketSync(
        store=store,
        module_factory=lambda: module,
        clock=lambda: _NOW,
    ).discover_instruments(MarketSyncConfig(universe="all", sync_workers=1))

    assert report.target_source == "eastmoney_stock_list"
    assert {ref.code for ref in store.list_instruments()} == {"000001", "600000", "688001"}


def test_online_client_reads_sse_daily_sessions_for_the_calendar() -> None:
    calls: list[tuple[str, dict[str, object], float]] = []

    def transport(url, params, timeout):
        calls.append((url, dict(params), timeout))
        return {
            "data": {
                "klines": [
                    "2026-09-29,1,1,1,1,0,0",
                    "2026-09-30,1,1,1,1,0,0",
                    "2026-10-09,1,1,1,1,0,0",
                ]
            }
        }

    client = OnlineMarketClient(transport=transport)

    dates = client.fetch_trading_calendar(
        date(2026, 9, 1),
        date(2026, 10, 9),
        timeout=4.0,
    )

    assert dates == (date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 9))
    assert calls[0][1]["secid"] == "1.000001"
    assert calls[0][1]["klt"] == "101"


def test_market_data_sync_forwards_the_calendar_probe_to_its_online_provider(
    tmp_path: Path,
) -> None:
    expected = (date(2026, 9, 30), date(2026, 10, 9))

    class CalendarClient:
        def fetch_trading_calendar(self, start_date, end_date, *, timeout):
            assert start_date <= date(2026, 9, 30)
            assert end_date == date(2026, 10, 9)
            assert timeout == 7.0
            return expected

    store = DuckDbMarketDataStore(tmp_path / "calendar.duckdb")
    provider = OnlineMarketSync(
        store=store,
        module_factory=CalendarClient,
    )
    service = MarketDataSync(store=store, eastmoney_sync=provider)

    assert (
        service.fetch_trading_calendar(
            date(2026, 6, 1),
            date(2026, 10, 9),
            timeout=7.0,
        )
        == expected
    )


def test_stage_plan_caps_incremental_ranges_at_the_selected_session(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "bounded-sync.duckdb")
    store.replace_local_bars(
        "SH",
        "600000",
        "stock",
        pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-09-08"]),
                "open": [10.0],
                "high": [10.2],
                "low": [9.8],
                "close": [10.1],
                "volume": [1000.0],
                "amount": [10100.0],
                "bar_status": ["completed"],
            }
        ),
    )
    service = OnlineMarketSync(
        store=store,
        module_factory=FakeOnline,
        clock=lambda: _NOW,
    )

    plan = service.prepare_stage_plan(
        _config(prefer_store_targets=True, end_date=date(2026, 9, 10))
    )

    assert plan.targets[0].requested_ranges[-1][1] == date(2026, 9, 10)


def test_eastmoney_sync_uses_full_history_for_new_instruments_and_writes_provider(
    tmp_path: Path,
) -> None:
    module = FakeOnline()
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    service = OnlineMarketSync(
        store=store,
        module_factory=lambda: module,
        clock=lambda: _NOW,
    )

    service.discover_instruments(_config())
    report = service.sync(_config(prefer_store_targets=True))

    assert report.provider == "eastmoney"
    assert report.processed == 3
    assert report.written_bars == 9
    assert {str(value) for value in store.read_bars("SH", "600000")["provider"]} == {"eastmoney"}
    assert (
        next(ref for ref in store.list_instruments() if ref.code == "600000").provider
        == "eastmoney"
    )
    assert store.status().last_online_provider == "eastmoney"
    assert module.history_requests
    # 全量起点是 2015-01-01：1990 年只有 4 只股票、2005 年才 1170 只，那段
    # "历史"里的全市场根本不是全市场，用它选股毫无泛化性可言。
    assert all(request["start_date"] == "20150101" for request in module.history_requests)
    assert all(request["end_date"] == "20260912" for request in module.history_requests)
    assert all(request["timeout"] == 30.0 for request in module.history_requests)


def test_eastmoney_sync_retries_a_transient_symbol_failure(tmp_path: Path) -> None:
    module = FakeOnline()
    module.history_failures = 1
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    service = OnlineMarketSync(
        store=store,
        module_factory=lambda: module,
        clock=lambda: _NOW,
        sleep=lambda _seconds: None,
    )

    service.discover_instruments(_config())
    report = service.sync(_config(prefer_store_targets=True))

    assert report.errors == 0
    assert len(module.history_requests) == 4


def test_eastmoney_falls_back_to_tencent_daily_history(tmp_path: Path) -> None:
    module = FallbackOnline()
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    service = OnlineMarketSync(
        store=store,
        module_factory=lambda: module,
        clock=lambda: _NOW,
        sleep=lambda _seconds: None,
    )

    service.discover_instruments(_config())
    report = service.sync(_config(prefer_store_targets=True))

    assert report.errors == 0
    assert report.written_bars == 9
    assert report.history_sources == {"tencent": 3}
    assert {str(value) for value in store.read_bars("SH", "600000")["provider"]} == {"tencent"}
    assert (
        next(ref for ref in store.list_instruments() if ref.code == "600000").provider == "tencent"
    )
    assert all(
        f"{request['market'].lower()}{request['code']}" in {"sh600000", "sz000001", "sh688001"}
        for request in module.history_requests
    )


def test_baostock_leads_the_instrument_catalog_when_it_answers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """东财 clist 已封禁，目录源由 baostock 打头（调研 2026-09-20 第七节）。"""

    monkeypatch.setattr(
        online_market_sync_module,
        "fetch_all_securities",
        lambda **_kwargs: pd.DataFrame(
            [
                {"代码": "600000", "名称": "浦发银行"},
                {"代码": "000001", "名称": "平安银行"},
                {"代码": "510300", "名称": "沪深300ETF"},
                {"代码": "830001", "名称": "北交样本"},
            ]
        ),
    )
    module = RecordingOnline()
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")

    report = OnlineMarketSync(
        store=store,
        module_factory=lambda: module,
        clock=lambda: _NOW,
    ).discover_instruments(_config())

    assert report.target_source == "baostock_stock_list"
    assert report.listed_instruments == 2
    # 目录拿到了就完全不该再打东财的目录接口。
    assert module.catalog_requests == []
    assert {ref.code for ref in store.list_instruments()} == {"000001", "600000"}


def test_baostock_catalog_drops_funds_from_the_stock_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """baostock 一次返回全市场，ETF 行必须在同一份帧里被剔掉。"""

    monkeypatch.setattr(
        online_market_sync_module,
        "fetch_all_securities",
        lambda **_kwargs: pd.DataFrame(
            [
                {"代码": "600000", "名称": "浦发银行"},
                {"代码": "510300", "名称": "沪深300ETF"},
                {"代码": "159915", "名称": "创业板ETF"},
            ]
        ),
    )
    module = RecordingOnline()
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")

    report = OnlineMarketSync(
        store=store,
        module_factory=lambda: module,
        clock=lambda: _NOW,
    ).discover_instruments(_config(instrument_types=("stock",)))

    assert report.target_source == "baostock_stock_list"
    assert {ref.code for ref in store.list_instruments()} == {"600000"}
    assert module.catalog_requests == []


def test_baostock_index_codes_do_not_collide_with_same_numbered_stocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上证指数 sh.000001 不能被当成平安银行 sz.000001。

    baostock 目录里两市代码会撞车；带着交易所进来后，指数会被正确分类成
    ``index`` 从而不进入股票标的，深市那只个股也才不会被去重丢掉。
    """

    monkeypatch.setattr(
        online_market_sync_module,
        "fetch_all_securities",
        lambda **_kwargs: pd.DataFrame(
            [
                {"代码": "000001", "名称": "上证综合指数", "市场": "SH"},
                {"代码": "000001", "名称": "平安银行", "市场": "SZ"},
                {"代码": "399001", "名称": "深证成份指数", "市场": "SZ"},
                {"代码": "600000", "名称": "浦发银行", "市场": "SH"},
            ]
        ),
    )
    module = RecordingOnline()
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")

    report = OnlineMarketSync(
        store=store,
        module_factory=lambda: module,
        clock=lambda: _NOW,
    ).discover_instruments(_config())

    instruments = {(ref.market, ref.code, ref.name) for ref in store.list_instruments()}
    assert instruments == {("SH", "600000", "浦发银行"), ("SZ", "000001", "平安银行")}
    # 两只指数都不该混进股票池，更不能把上证指数记成深市。
    assert report.listed_instruments == 2


def test_a_catalog_without_a_market_column_still_infers_by_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """东财目录只有代码列，缺市场列时必须退回按代码推断。"""

    monkeypatch.setattr(
        online_market_sync_module,
        "fetch_all_securities",
        lambda **_kwargs: pd.DataFrame(
            [{"代码": "600000", "名称": "浦发银行"}, {"代码": "000001", "名称": "平安银行"}]
        ),
    )
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")

    OnlineMarketSync(
        store=store,
        module_factory=lambda: RecordingOnline(),
        clock=lambda: _NOW,
    ).discover_instruments(_config())

    assert {(ref.market, ref.code) for ref in store.list_instruments()} == {
        ("SH", "600000"),
        ("SZ", "000001"),
    }


def test_catalog_falls_back_to_eastmoney_when_baostock_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """baostock 没装或查询失败时，目录必须仍能靠东财走通。"""

    def refuse(**_kwargs: object) -> pd.DataFrame:
        raise BaostockCatalogError("未安装可选依赖 baostock")

    monkeypatch.setattr(online_market_sync_module, "fetch_all_securities", refuse)
    module = FakeOnline()
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")

    report = OnlineMarketSync(
        store=store,
        module_factory=lambda: module,
        clock=lambda: _NOW,
    ).discover_instruments(_config())

    assert report.target_source == "eastmoney_stock_list"
    assert {ref.code for ref in store.list_instruments()} == {"000001", "600000", "688001"}


def test_a_broken_catalog_source_warns_instead_of_failing_silently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """退回东财目录不能是静默的。

    东财 clist 处于封禁状态，「退回」实际上等于目录断供：同步照常报成功，
    但只会覆盖库里已有的标的。必须留一条能看懂的告警。
    """

    def refuse(**_kwargs: object) -> pd.DataFrame:
        raise BaostockCatalogError("未安装可选依赖 baostock")

    monkeypatch.setattr(online_market_sync_module, "fetch_all_securities", refuse)
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")

    with caplog.at_level("WARNING", logger="aquest.adapters.online_market_sync"):
        OnlineMarketSync(
            store=store,
            module_factory=lambda: FakeOnline(),
            clock=lambda: _NOW,
        ).discover_instruments(_config())

    messages = [record.getMessage() for record in caplog.records]
    assert any("baostock 不可用" in message for message in messages), messages
    # 告警里要给出补救办法，否则看到的人不知道该装什么。
    assert any("[catalog]" in message for message in messages), messages


def test_catalog_sources_can_be_pinned_to_eastmoney(tmp_path: Path) -> None:
    """保留一条不走 baostock 的显式通道，方便排查与回归。"""

    module = RecordingOnline()
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")

    report = OnlineMarketSync(
        store=store,
        module_factory=lambda: module,
        clock=lambda: _NOW,
        catalog_sources=("eastmoney",),
    ).discover_instruments(_config())

    assert report.target_source == "eastmoney_stock_list"
    assert len(module.catalog_requests) == 1


def test_tencent_is_the_primary_daily_source_when_both_are_reachable(tmp_path: Path) -> None:
    """东财日线属于「间歇可用」，腾讯才是稳定主源（调研 2026-09-20 第四节）。

    两个源都能返回数据时必须先走腾讯，东财一次都不该被打扰。
    """

    module = BothSourcesOnline()
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    service = OnlineMarketSync(
        store=store,
        module_factory=lambda: module,
        clock=lambda: _NOW,
        sleep=lambda _seconds: None,
    )

    service.discover_instruments(_config())
    report = service.sync(_config(prefer_store_targets=True))

    assert report.errors == 0
    assert report.written_bars == 9
    assert report.history_sources == {"tencent": 3}
    assert {str(value) for value in store.read_bars("SH", "600000")["provider"]} == {"tencent"}
    assert len(module.tencent_requests) == 3
    # 主源成功时东财应保持零请求：它现在只是容错路径。
    assert module.history_requests == []


def test_tencent_refetch_corrects_old_provider_label_without_touching_local_rows(
    tmp_path: Path,
) -> None:
    module = BothSourcesOnline()
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    history = OnlineMarketSync(
        store=store,
        module_factory=lambda: module,
        clock=lambda: _NOW,
        sleep=lambda _seconds: None,
    )
    old = _hist_frame().rename(
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
    old["date"] = pd.to_datetime(old["date"])
    old["market"] = "SH"
    old["code"] = "600000"
    old["instrument_type"] = "stock"
    old["board"] = "main"
    old["provider"] = "eastmoney"
    old["bar_status"] = "completed"
    store.upsert_online_bars(old, provider="eastmoney")
    local_override = old.iloc[[0]].drop(columns=["provider"]).copy()
    local_override["close"] = local_override["open"]
    local_override["high"] = local_override["open"] + 0.1
    local_override["low"] = local_override["open"] - 0.1
    store.upsert_local_bars(local_override)
    store.ensure_instruments(
        [
            online_market_sync_module.InstrumentRef(
                market="SH", code="600000", instrument_type="stock", board="main"
            ),
            online_market_sync_module.InstrumentRef(
                market="SH", code="688001", instrument_type="stock", board="star"
            ),
        ]
    )

    report = history.sync(_config(prefer_store_targets=True))

    assert report.errors == 0
    local = store.read_bars("SH", "600000")
    online = local.loc[local["source"] == "online"]
    assert set(online["provider"].astype(str)) == {"tencent"}
    assert local.iloc[0]["source"] == "local"
    assert local.iloc[0]["close"] == local.iloc[0]["open"]


def test_local_rows_with_vendor_labels_do_not_trigger_provider_only_refetch() -> None:
    existing = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-01-02"]),
            "open": [10.0],
            "high": [11.0],
            "low": [9.0],
            "close": [10.5],
            "volume": [1000.0],
            "amount": [10000.0],
            "bar_status": ["completed"],
            "source": ["local"],
            "provider": ["eastmoney"],
        }
    )
    fetched = existing.drop(columns=["source", "bar_status"]).assign(
        provider="tencent", bar_status="completed"
    )

    assert _has_new_data(existing, fetched) is False


def test_online_upsert_rejects_missing_provider_before_writing_any_rows(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
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
    frame["market"] = "SH"
    frame["code"] = "600000"
    frame["instrument_type"] = "stock"
    frame["board"] = "main"
    frame["bar_status"] = "completed"
    frame["provider"] = ["tencent", None, "eastmoney"]

    with pytest.raises(ValueError, match="缺少来源字段"):
        store.upsert_online_bars(frame)

    assert store.status().bar_count == 0


def test_catalog_cancellation_stops_before_requesting_the_next_page() -> None:
    calls = 0
    cancelled = threading.Event()

    def transport(_url, _params, _timeout):
        nonlocal calls
        calls += 1
        cancelled.set()
        return {"data": {"diff": [{"f12": "600000", "f14": "浦发银行"}], "total": 2}}

    client = OnlineMarketClient(transport=transport, page_size=1, sleep=lambda _seconds: None)

    with pytest.raises(MarketDataCancelledError, match="取消"):
        client.fetch_instruments(timeout=1.0, abort_check=cancelled.is_set)

    assert calls == 1


def test_staged_catalog_plan_checks_cancellation_before_catalog_fetch(tmp_path: Path) -> None:
    module = RecordingOnline()
    service = OnlineMarketSync(
        store=DuckDbMarketDataStore(tmp_path / "market.duckdb"),
        module_factory=lambda: module,
        clock=lambda: _NOW,
        catalog_sources=("eastmoney",),
    )

    with pytest.raises(MarketDataCancelledError, match="取消"):
        service.prepare_stage_plan(_config(), abort_check=lambda: True)

    assert module.catalog_requests == []


def test_catalog_cancellation_interrupts_retry_backoff() -> None:
    calls = 0
    cancelled = threading.Event()
    sleeps: list[float] = []

    def transport(_url, _params, _timeout):
        nonlocal calls
        calls += 1
        return {"rc": 1, "data": None}

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        cancelled.set()

    client = OnlineMarketClient(transport=transport, page_size=1, sleep=sleep)

    with pytest.raises(MarketDataCancelledError, match="取消"):
        client.fetch_instruments(timeout=1.0, abort_check=cancelled.is_set)

    assert calls == 1
    assert sleeps


def test_catalog_retries_without_cancellation() -> None:
    calls = 0
    sleeps: list[float] = []

    def transport(_url, _params, _timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"rc": 1, "data": None}
        return {"data": {"diff": [{"f12": "600000", "f14": "浦发银行"}], "total": 1}}

    client = OnlineMarketClient(transport=transport, page_size=1, sleep=sleeps.append)

    frame = client.fetch_instruments(timeout=1.0)

    assert calls == 2
    assert len(sleeps) == 10
    assert frame.iloc[0]["代码"] == "600000"


def test_eastmoney_full_history_fetches_only_missing_ranges_for_existing_rows(
    tmp_path: Path,
) -> None:
    class RangeAwareOnline(FakeOnline):
        def fetch_instruments(self, *, timeout: float) -> pd.DataFrame:
            del timeout
            return pd.DataFrame([{"代码": "600000", "名称": "浦发银行"}])

        def fetch_daily_history(
            self,
            symbol: str,
            start_date: str,
            end_date: str,
            *,
            timeout: float,
        ) -> pd.DataFrame:
            self.history_requests.append(
                {
                    "symbol": symbol,
                    "start_date": start_date,
                    "end_date": end_date,
                    "timeout": timeout,
                }
            )
            start = pd.Timestamp(start_date).date()
            end = pd.Timestamp(end_date).date()
            available = [
                day
                for day in pd.date_range("2026-09-07", "2026-09-11", freq="D").date
                if start <= day <= end
            ]
            return _hist_frame_for_dates([day.isoformat() for day in available])

    module = RangeAwareOnline()
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    service = OnlineMarketSync(
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
        ("20150101", "20260907"),
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


def test_eastmoney_full_history_skips_a_target_covering_the_requested_window() -> None:
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


def test_eastmoney_stage_fetch_normalizes_without_writing_main_store(tmp_path: Path) -> None:
    module = FakeOnline()
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    service = OnlineMarketSync(
        store=store,
        module_factory=lambda: module,
        clock=lambda: _NOW,
        sleep=lambda _seconds: None,
    )
    target = StageTarget(
        provider="eastmoney",
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


def test_staged_tencent_request_uses_the_shared_rate_gate(tmp_path: Path) -> None:
    """进程池同步里，腾讯请求必须能被共享限流状态接纳。

    这是线上真实组合：staging worker 把 ``ProcessRateGate`` 传给 ``fetch_stage_target``，
    而腾讯是主源，第一个被 wait 的就是它。状态里只建了东财时会 KeyError，
    而 worker 会把每个标的都记成 error，表现为「全市场同步全部失败」。
    """

    class _FakeManager:
        def dict(self, initial: dict[str, object]) -> dict[str, object]:
            return dict(initial)

        def Lock(self) -> threading.Lock:
            return threading.Lock()

    module = BothSourcesOnline()
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    service = OnlineMarketSync(
        store=store,
        module_factory=lambda: module,
        clock=lambda: _NOW,
        sleep=lambda _seconds: None,
    )
    target = StageTarget(
        provider="eastmoney",
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
    rate_state, rate_lock = create_rate_state(_FakeManager())
    gate = ProcessRateGate(rate_state, rate_lock)

    result = service.fetch_stage_target(target, _config(), rate_gate=gate)

    assert result.status == "updated"
    assert result.history_source == "tencent"
    # 腾讯真的占了限流状态的名额，而不是被记到东财桶里。
    assert float(rate_state["next_allowed_tencent"]) > 0
    assert float(rate_state["next_allowed_eastmoney"]) == 0.0


def test_empty_tencent_response_falls_back_to_eastmoney(tmp_path: Path) -> None:
    module = EmptyTencentOnline()
    service = OnlineMarketSync(
        store=DuckDbMarketDataStore(tmp_path / "empty-tencent.duckdb"),
        module_factory=lambda: module,
        clock=lambda: _NOW,
        sleep=lambda _seconds: None,
    )

    result = service._fetch_target(
        module,
        "SH",
        "600000",
        "stock",
        "main",
        "浦发银行",
        _config(),
        existing=_ExistingBars(pd.DataFrame()),
    )

    assert not result.frame.empty
    assert result.history_source == "eastmoney"


def test_fetch_target_keeps_provider_provenance_per_requested_range(tmp_path: Path) -> None:
    class MixedRangeOnline(FakeOnline):
        def fetch_tencent_daily_history(
            self,
            market: str,
            code: str,
            start_date: str,
            end_date: str,
            *,
            timeout: float,
        ) -> pd.DataFrame:
            del market, code, end_date, timeout
            if start_date == "20240103":
                raise RuntimeError("Tencent temporarily unavailable")
            return pd.DataFrame(
                {
                    "date": pd.to_datetime(["2024-01-02"]),
                    "open": [10.0],
                    "close": [10.5],
                    "high": [11.0],
                    "low": [9.5],
                    "volume": [1000.0],
                    "amount": [10000.0],
                }
            )

        def fetch_daily_history(
            self,
            symbol: str,
            start_date: str,
            end_date: str,
            *,
            timeout: float,
        ) -> pd.DataFrame:
            del symbol, end_date, timeout
            assert start_date == "20240103"
            return pd.DataFrame(
                {
                    "日期": pd.to_datetime(["2024-01-03"]),
                    "开盘": [11.0],
                    "收盘": [11.5],
                    "最高": [12.0],
                    "最低": [10.5],
                    "成交量": [1000.0],
                    "成交额": [10000.0],
                }
            )

    module = MixedRangeOnline()
    service = OnlineMarketSync(
        store=DuckDbMarketDataStore(tmp_path / "market.duckdb"),
        module_factory=lambda: module,
        clock=lambda: _NOW,
        sleep=lambda _seconds: None,
    )

    result = service._fetch_target(
        module,
        "SH",
        "600000",
        "stock",
        "main",
        "浦发银行",
        _config(),
        existing=_ExistingBars(pd.DataFrame()),
        requested_ranges=(
            (date(2024, 1, 2), date(2024, 1, 2)),
            (date(2024, 1, 3), date(2024, 1, 3)),
        ),
    )

    assert result.history_source == "tencent+eastmoney"
    assert list(result.frame["provider"]) == ["tencent", "eastmoney"]


def test_eastmoney_provider_rejects_invalid_ohlc_rows(tmp_path: Path) -> None:
    class InvalidOnline(FakeOnline):
        def fetch_daily_history(
            self,
            symbol: str,
            start_date: str,
            end_date: str,
            *,
            timeout: float,
        ) -> pd.DataFrame:
            del symbol, start_date, end_date, timeout
            frame = _hist_frame()
            frame.loc[0, "最高"] = 1.0
            return frame

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    service = OnlineMarketSync(
        store=store,
        module_factory=InvalidOnline,
        clock=lambda: _NOW,
        sleep=lambda _seconds: None,
    )
    service.discover_instruments(_config())

    report = service.sync(_config(prefer_store_targets=True))

    assert report.processed == 3
    assert report.errors == 3
    assert report.written_bars == 0
    assert not store.read_bars("SH", "600000").any(axis=None)


def test_eastmoney_empty_history_is_reported_as_a_symbol_error(tmp_path: Path) -> None:
    class EmptyHistoryOnline(FakeOnline):
        def fetch_daily_history(
            self,
            symbol: str,
            start_date: str,
            end_date: str,
            *,
            timeout: float,
        ) -> pd.DataFrame:
            del symbol, start_date, end_date, timeout
            return pd.DataFrame()

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    service = OnlineMarketSync(
        store=store,
        module_factory=EmptyHistoryOnline,
        clock=lambda: _NOW,
        sleep=lambda _seconds: None,
    )
    service.discover_instruments(_config())

    report = service.sync(_config(prefer_store_targets=True))

    assert report.errors == 3
    assert report.unchanged_files == 0
    assert any("空日线响应没有明确的无数据标记" in reason for reason in report.failure_reasons)


def test_empty_full_history_response_preserves_existing_bars_and_reports_failure(
    tmp_path: Path,
) -> None:
    class EmptyHistoryOnline(FakeOnline):
        def fetch_daily_history(
            self,
            symbol: str,
            start_date: str,
            end_date: str,
            *,
            timeout: float,
        ) -> pd.DataFrame:
            del symbol, start_date, end_date, timeout
            return pd.DataFrame()

    store = DuckDbMarketDataStore(tmp_path / "existing-empty.duckdb")
    existing = _hist_frame().rename(
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
    existing["market"] = "SH"
    existing["code"] = "600000"
    existing["instrument_type"] = "stock"
    existing["board"] = "main"
    existing["bar_status"] = "completed"
    existing["name"] = "浦发银行"
    store.upsert_local_bars(existing)
    before = store.read_bars("SH", "600000")
    service = OnlineMarketSync(
        store=store,
        module_factory=EmptyHistoryOnline,
        clock=lambda: _NOW,
        sleep=lambda _seconds: None,
    )
    service.discover_instruments(_config())

    report = service.sync(_config(codes=("600000",), prefer_store_targets=True, full_history=True))

    assert report.processed == 1
    assert report.errors == 1
    assert report.unchanged_files == 0
    assert report.history_sources.get("already_complete", 0) == 0
    pd.testing.assert_frame_equal(store.read_bars("SH", "600000"), before)


def test_explicit_valid_empty_full_history_is_no_new_data_and_preserves_bars(
    tmp_path: Path,
) -> None:
    store = DuckDbMarketDataStore(tmp_path / "explicit-empty.duckdb")
    existing = _hist_frame().rename(
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
    existing["market"] = "SH"
    existing["code"] = "600000"
    existing["instrument_type"] = "stock"
    existing["board"] = "main"
    existing["bar_status"] = "completed"
    existing["name"] = "浦发银行"
    store.upsert_local_bars(existing)
    before = store.read_bars("SH", "600000")
    service = OnlineMarketSync(
        store=store,
        module_factory=ValidEmptyHistoryOnline,
        clock=lambda: _NOW,
        sleep=lambda _seconds: None,
    )
    service.discover_instruments(_config())

    report = service.sync(_config(codes=("600000",), prefer_store_targets=True, full_history=True))

    assert report.processed == 1
    assert report.errors == 0
    assert report.unchanged_files == 1
    assert report.history_sources == {"no_new_data": 1}
    pd.testing.assert_frame_equal(store.read_bars("SH", "600000"), before)


def test_eastmoney_sync_can_be_cancelled_before_fetching_bars(tmp_path: Path) -> None:
    module = FakeOnline()
    service = OnlineMarketSync(
        store=DuckDbMarketDataStore(tmp_path / "market.duckdb"),
        module_factory=lambda: module,
        clock=lambda: _NOW,
        sleep=lambda _seconds: None,
    )

    with pytest.raises(MarketDataCancelledError, match="取消"):
        service.sync(_config(), abort_check=lambda: True)

    assert module.history_requests == []


def test_market_data_sync_routes_every_run_through_eastmoney(tmp_path: Path) -> None:
    eastmoney = FakeOnline()

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    service = MarketDataSync(
        store=store,
        eastmoney_module_factory=lambda: eastmoney,
    )

    report = service.discover_instruments(MarketSyncConfig(sync_workers=1))

    assert report.provider == "eastmoney"
    assert report.listed_instruments == 3

    sync_report = service.sync(MarketSyncConfig(sync_workers=1))

    assert sync_report.provider == "eastmoney"
    assert store.get_meta("last_online_provider") == "eastmoney"


def test_sync_config_rejects_the_b_share_board(tmp_path: Path) -> None:
    """B 股已从板块取值里移除，配置层就拒绝，不会走到在线同步。"""

    with pytest.raises(ValueError, match="不支持的板块范围"):
        MarketSyncConfig(instrument_types=("stock",), boards=("b_share",), sync_workers=1)


def test_sync_config_rejects_non_stock_instrument_types() -> None:
    """基金、债券、指数在配置层就被拒，不会一路走到在线同步才报错。"""

    with pytest.raises(ValueError, match="不支持的证券类型"):
        MarketSyncConfig(instrument_types=("bond",), sync_workers=1)


def test_market_sync_config_rejects_unknown_and_removed_providers() -> None:
    with pytest.raises(ValueError, match="行情数据源"):
        MarketSyncConfig(provider="unknown")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="行情数据源"):
        MarketSyncConfig(provider="tdx")  # type: ignore[arg-type]


def test_batched_bar_writer_merges_concurrent_writes_and_returns_each_result(
    tmp_path, monkeypatch
) -> None:
    """8 路并发写库合并成一个事务：每个调用拿到自己的计数，一只都不能少。"""

    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from aquest.adapters.online_market_sync import _BatchedBarWriter

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    writer = _BatchedBarWriter(store, max_batch=8)
    real = store.upsert_online_bars_many
    batches: list[int] = []

    def recording(frames, *, provider: str = "eastmoney"):
        batches.append(len(frames))
        return real(frames, provider=provider)

    monkeypatch.setattr(store, "upsert_online_bars_many", recording)
    barrier = Barrier(8)

    def one(index: int) -> tuple[int, int]:
        frame = pd.DataFrame(
            {
                "market": ["SH"],
                "code": [f"{600000 + index:06d}"],
                "instrument_type": ["stock"],
                "date": [pd.Timestamp("2026-10-19")],
                "open": [10.0],
                "high": [11.0],
                "low": [9.0],
                "close": [10.5],
                "volume": [1000.0],
                "amount": [10500.0],
                "source": ["online"],
                "price_basis": ["raw"],
                "bar_status": ["completed"],
                "provider": ["eastmoney"],
            }
        )
        barrier.wait()
        return writer.upsert(frame)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(one, range(40)))

    assert results == [(1, 0)] * 40
    assert sum(batches) == 40
    # 并发到达的那一刻至少有一批被合并（否则和逐只写没有区别）
    assert max(batches) >= 2
    with store.read_session() as connection:
        assert connection.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0] == 40
