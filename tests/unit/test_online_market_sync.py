from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from selector_app.adapters.market_sync import MarketSyncConfig, TdxMarketSync
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.tdx_protocol.types import KlineCategory


def _frame(close: float = 10.0) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-08-20", "2026-08-21", "2026-08-22"]),
            "open": [close, close + 0.1, close + 0.2],
            "high": [close + 0.2, close + 0.3, close + 0.4],
            "low": [close - 0.2, close - 0.1, close],
            "close": [close + 0.1, close + 0.2, close + 0.4],
            "volume": [1000.0, 1100.0, 1200.0],
            "amount": [10000.0, 11000.0, 12000.0],
            "bar_status": ["completed", "completed", "completed"],
        }
    )


class FakeClient:
    def __init__(self, frame: pd.DataFrame | None = None) -> None:
        self.frame = frame if frame is not None else _frame()
        self.requests: list[tuple[str, int]] = []
        self.list_calls = 0

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def get_security_list_all(self, pages: str = "all") -> pd.DataFrame:
        del pages
        self.list_calls += 1
        return pd.DataFrame(
            [
                {"market": "SH", "code": "600000", "name": "浦发银行"},
                {"market": "SH", "code": "510300", "name": "沪深300ETF"},
                {"market": "SH", "code": "000001", "name": "上证指数"},
            ]
        )

    def get_security_bars(
        self, market: str, code: str, category: str, start: int, count: int
    ) -> pd.DataFrame:
        assert category == KlineCategory.DAY
        assert start == 0
        self.requests.append((code, count))
        return self.frame.tail(min(count, len(self.frame))).copy()


def test_online_sync_writes_duckdb_without_touching_vipdoc(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    source_dir = tmp_path / "vipdoc"
    source_dir.mkdir()
    client = FakeClient()

    report = TdxMarketSync(
        store=store,
        client_factory=lambda _timeout: client,
        clock=lambda: datetime(2026, 8, 30, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    ).sync(MarketSyncConfig(universe="all"))

    assert report.total_candidates == 3
    assert report.processed == 3
    assert report.updated_files == 3
    assert report.written_bars == 9
    assert store.status().bar_count == 9
    assert not list(source_dir.rglob("*.day"))
    assert store.list_instruments()[0].name == "上证指数"


def test_online_sync_does_not_overwrite_local_rows(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    local = _frame(20.0)
    store.replace_local_bars("SH", "600000", "stock", local)
    client = FakeClient(_frame(99.0))

    report = TdxMarketSync(
        store=store,
        client_factory=lambda _timeout: client,
    ).sync(MarketSyncConfig(universe="sh"))

    assert report.written_bars == 6
    frame = store.read_bars("SH", "600000")
    assert frame.iloc[0]["close"] == 20.1
    assert set(frame.loc[frame["code"] == "600000", "source"]) == {"local"}


def test_online_sync_uses_existing_store_targets_without_refetching_security_list(
    tmp_path: Path,
) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _frame())
    client = FakeClient()

    report = TdxMarketSync(
        store=store,
        client_factory=lambda _timeout: client,
    ).sync(MarketSyncConfig(universe="sh", prefer_store_targets=True))

    assert report.total_candidates == 1
    assert client.list_calls == 0


def test_online_security_list_fills_name_for_existing_local_instrument(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _frame())
    client = FakeClient()

    TdxMarketSync(
        store=store,
        client_factory=lambda _timeout: client,
    ).sync(MarketSyncConfig(universe="sh"))

    ref = next(ref for ref in store.list_instruments() if ref.code == "600000")
    assert ref.name == "浦发银行"


def test_store_target_sync_refreshes_missing_names_once(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _frame())
    client = FakeClient()

    report = TdxMarketSync(
        store=store,
        client_factory=lambda _timeout: client,
    ).sync(
        MarketSyncConfig(
            universe="sh",
            prefer_store_targets=True,
            refresh_names=True,
        )
    )

    assert report.total_candidates == 1
    assert client.list_calls == 1
    ref = next(ref for ref in store.list_instruments() if ref.code == "600000")
    assert ref.name == "浦发银行"


def test_full_security_discovery_adds_new_targets_and_reports_scope(
    tmp_path: Path,
) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _frame())

    class ExpandedClient(FakeClient):
        def get_security_list_all(self, pages: str = "all") -> pd.DataFrame:
            del pages
            self.list_calls += 1
            return pd.DataFrame(
                [
                    {"market": "SH", "code": "600000", "name": "浦发银行"},
                    {"market": "SH", "code": "600001", "name": "新标的"},
                    {"market": "SH", "code": "510300", "name": "沪深300ETF"},
                    {"market": "SH", "code": "000001", "name": "上证指数"},
                ]
            )

    service = TdxMarketSync(
        store=store,
        client_factory=lambda _timeout: ExpandedClient(),
    )

    report = service.sync(
        MarketSyncConfig(
            universe="sh",
            instrument_types=("stock",),
            boards=("main",),
        )
    )

    assert report.listed_instruments == 4
    assert report.filtered_instruments == 2
    assert report.total_candidates == 2
    assert report.existing_instruments == 1
    assert report.new_instruments == 1
    assert report.target_source == "tdx_security_list"
    assert [ref.code for ref in store.list_instruments(market="SH")] == [
        "600000",
        "600001",
    ]
