from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from threading import Lock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from selector_app.adapters.market_sync import (
    MarketDataCancelledError,
    MarketSyncConfig,
    TdxMarketSync,
)
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.tdx_protocol.types import KlineCategory


def bars_frame(close: float = 10.0) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-08-20", "2026-08-21", "2026-08-22"]),
            "open": [close, close + 0.1, close + 0.2],
            "high": [close + 0.4, close + 0.5, close + 0.6],
            "low": [close - 0.2, close - 0.1, close],
            "close": [close + 0.2, close + 0.3, close + 0.5],
            "vol": [100_000.0, 110_000.0, 120_000.0],
            "amount": [1_000_000.0, 1_100_000.0, 1_200_000.0],
        }
    )


class FakeClient:
    def __init__(self, *, fail_code: str | None = None, frame: pd.DataFrame | None = None) -> None:
        self.fail_code = fail_code
        self.frame = frame if frame is not None else bars_frame()
        self.requests: list[tuple[str, int]] = []

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def get_security_list_all(self, pages: str = "all") -> pd.DataFrame:
        del pages
        return pd.DataFrame(
            [
                {"market": "SH", "code": "600000"},
                {"market": "SZ", "code": "000001"},
                {"market": "SH", "code": "510300"},
            ]
        )

    def get_security_bars(
        self, market: object, code: str, category: object, start: int, count: int
    ) -> pd.DataFrame:
        del market
        assert category == KlineCategory.DAY
        assert start == 0
        self.requests.append((code, count))
        if code == self.fail_code:
            raise RuntimeError("upstream unavailable")
        return self.frame.tail(min(count, len(self.frame))).copy()


def _service(tmp_path: Path, client: FakeClient, *, clock: datetime | None = None) -> TdxMarketSync:
    return TdxMarketSync(
        store=DuckDbMarketDataStore(tmp_path / "market.duckdb"),
        client_factory=lambda _timeout: client,
        clock=(lambda: clock) if clock is not None else None,
    )


def test_sync_writes_all_known_instruments_to_duckdb(tmp_path: Path) -> None:
    service = _service(tmp_path, FakeClient())

    report = service.sync(MarketSyncConfig())

    assert report.total_candidates == 3
    assert report.processed == 3
    assert report.updated_files == 3
    assert report.written_bars == 9
    assert report.errors == 0
    assert service._store.status().bar_count == 9


def test_sync_continues_after_one_upstream_failure(tmp_path: Path) -> None:
    service = _service(tmp_path, FakeClient(fail_code="000001"))

    report = service.sync(MarketSyncConfig())

    assert report.processed == 3
    assert report.updated_files == 2
    assert report.written_bars == 6
    assert report.errors == 1
    assert report.failure_reasons["upstream unavailable"] == 1


def test_sync_skips_unchanged_online_rows(tmp_path: Path) -> None:
    client = FakeClient()
    service = _service(tmp_path, client)

    first = service.sync(MarketSyncConfig())
    second = service.sync(MarketSyncConfig())

    assert first.written_bars == 9
    assert second.written_bars == 0
    assert second.unchanged_files == 3
    assert client.requests.count(("600000", 800)) == 1
    assert client.requests.count(("600000", 2)) == 1


def test_sync_uses_completed_bar_after_close(tmp_path: Path) -> None:
    current_day = pd.Timestamp("2026-08-26")
    frame = bars_frame()
    frame.loc[frame.index[-1], "date"] = current_day
    client = FakeClient(frame=frame)
    service = _service(
        tmp_path,
        client,
        clock=datetime(2026, 8, 26, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    report = service.sync(MarketSyncConfig(universe="sh"))

    assert report.written_bars == 6
    assert service._store.read_bars("SH", "600000").iloc[-1]["date"] == pd.Timestamp("2026-08-26")


def test_sync_uses_eight_workers_without_duplicate_target_requests(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    frame = bars_frame()
    store_frame = frame.rename(columns={"vol": "volume"}).copy()
    store_frame["bar_status"] = "completed"
    codes = [f"600{index:03d}" for index in range(16)]
    for code in codes:
        store.replace_local_bars("SH", code, "stock", store_frame)

    lock = Lock()
    active = 0
    max_active = 0
    requests: list[str] = []

    class TrackingClient(FakeClient):
        def get_security_bars(
            self, market: object, code: str, category: object, start: int, count: int
        ) -> pd.DataFrame:
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
                requests.append(code)
            try:
                time.sleep(0.02)
                return super().get_security_bars(market, code, category, start, count)
            finally:
                with lock:
                    active -= 1

    client = FakeClient()
    service = TdxMarketSync(
        store=store,
        client_factory=lambda _timeout: TrackingClient(frame=client.frame),
    )

    report = service.sync(
        MarketSyncConfig(universe="sh", prefer_store_targets=True, sync_workers=8)
    )

    assert report.total_candidates == len(codes)
    assert report.processed == len(codes)
    assert sorted(requests) == sorted(codes)
    assert max_active > 1
    assert max_active <= 8


def test_sync_can_be_cancelled_before_fetching_bars(tmp_path: Path) -> None:
    client = FakeClient()
    service = _service(tmp_path, client)

    with pytest.raises(MarketDataCancelledError, match="取消"):
        service.sync(MarketSyncConfig(), abort_check=lambda: True)

    assert client.requests == []
