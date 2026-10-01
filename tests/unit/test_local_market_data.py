from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from selector_app.adapters.local_day_adapter import StockRef
from selector_app.market_data.service import LocalMarketDataService


def market_frame(offset: float = 0.0) -> pd.DataFrame:
    dates = pd.date_range("2021-01-01", periods=1600, freq="D")
    close = np.linspace(10 + offset, 40 + offset, len(dates))
    return pd.DataFrame(
        {
            "date": dates,
            "open": close - 0.5,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": np.full(len(dates), 1000.0),
            "amount": close * 1000,
        }
    )


class FakeMarketDataAdapter:
    def __init__(self) -> None:
        self.refs = [
            StockRef(
                "SH",
                "600000",
                Path("/data/vipdoc/sh/lday/sh600000.day"),
                name="浦发银行",
            ),
            StockRef(
                "SH",
                "600001",
                Path("/data/vipdoc/sh/lday/sh600001.day"),
                name="新标的",
            ),
            StockRef(
                "SZ",
                "000001",
                Path("/data/vipdoc/sz/lday/sz000001.day"),
                name="平安银行",
            ),
        ]
        self.frames = {
            (ref.market, ref.code): market_frame(index) for index, ref in enumerate(self.refs)
        }

    def list_stock_refs(
        self,
        vipdoc_path: str | Path,
        universe: str,
        universe_file: str | Path | None = None,
    ) -> list[StockRef]:
        del vipdoc_path, universe_file
        return [ref for ref in self.refs if universe == "all" or ref.market.lower() == universe]

    def stock_ref(self, vipdoc_path: str | Path, market: str, code: str) -> StockRef:
        del vipdoc_path
        for ref in self.refs:
            if ref.market == market and ref.code == code:
                return ref
        raise ValueError(f"找不到本地行情: {market} {code}")

    def read_stock(self, ref: StockRef) -> pd.DataFrame:
        return self.frames[(ref.market, ref.code)].copy()


def test_lists_local_instruments_with_pagination_and_summary() -> None:
    service = LocalMarketDataService(FakeMarketDataAdapter())

    page = service.list_instruments("/data/vipdoc", market="SH", keyword="600", page=2, page_size=1)

    assert page.total == 2
    assert page.pages == 2
    assert page.items[0].code == "600001"
    assert page.items[0].name == "新标的"
    assert page.items[0].bars == 1600
    assert page.items[0].data_start == "2021-01-01"
    assert page.items[0].data_end == "2025-05-19"
    assert page.items[0].last_close == pytest.approx(41.0)


def test_lists_local_instruments_by_chinese_name() -> None:
    service = LocalMarketDataService(FakeMarketDataAdapter())

    page = service.list_instruments("/data/vipdoc", keyword="浦发")

    assert page.total == 1
    assert page.items[0].code == "600000"
    assert page.items[0].name == "浦发银行"


def test_lists_data_bearing_instruments_first_from_persisted_summary() -> None:
    empty = StockRef(
        "SH",
        "000001",
        Path("/data/vipdoc/sh/lday/sh000001.day"),
        name="上证指数",
        bar_count=0,
    )
    populated = StockRef(
        "SH",
        "600519",
        Path("/data/vipdoc/sh/lday/sh600519.day"),
        name="贵州茅台",
        bar_count=807,
        first_date=date(2023, 5, 16),
        last_date=date(2026, 9, 8),
        last_close=1309.3,
    )

    class SummaryOnlyAdapter:
        def list_stock_refs(self, vipdoc_path, universe, universe_file=None):
            del vipdoc_path, universe, universe_file
            return [empty, populated]

        def stock_ref(self, vipdoc_path, market, code):
            del vipdoc_path, market, code
            raise AssertionError("列表摘要不应重新读取每个标的的全部 K 线")

        def read_stock(self, ref):
            del ref
            raise AssertionError("列表摘要不应重新读取每个标的的全部 K 线")

    page = LocalMarketDataService(SummaryOnlyAdapter()).list_instruments(
        "/data/vipdoc", page=1, page_size=2
    )

    assert [item.code for item in page.items] == ["600519", "000001"]
    assert page.items[0].bars == 807
    assert page.items[0].last_close == pytest.approx(1309.3)
    assert page.items[1].error == "没有可用日线数据"


def test_builds_daily_monthly_and_yearly_chart_data_with_indicators() -> None:
    service = LocalMarketDataService(FakeMarketDataAdapter())

    daily = service.get_chart(
        "/data/vipdoc",
        "SH",
        "600000",
        period="daily",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 3, 31),
    )
    monthly = service.get_chart("/data/vipdoc", "SH", "600000", period="monthly")
    yearly = service.get_chart("/data/vipdoc", "SH", "600000", period="yearly")

    assert daily.period == "daily"
    assert daily.data_start == "2024-01-01"
    assert daily.data_end == "2024-03-31"
    assert len(daily.candles) == 91

    assert monthly.period == "monthly"
    assert monthly.candles[0]["date"] == "2021-01-31"
    assert monthly.candles[0]["open"] == pytest.approx(9.5)
    assert monthly.candles[0]["volume"] == pytest.approx(31_000)
    assert monthly.candles[-1]["ma"]["ma5"] is not None
    assert monthly.candles[-1]["rsi14"] is not None
    assert monthly.candles[-1]["macd"] is not None
    assert monthly.candles[-1]["macd_signal"] is not None

    assert yearly.period == "yearly"
    assert yearly.candles[0]["date"] == "2021-12-31"
    assert yearly.candles[-1]["ma"]["ma5"] is not None


def test_builds_requested_formula_indicator_panels_from_full_daily_history() -> None:
    service = LocalMarketDataService(FakeMarketDataAdapter())

    chart = service.get_chart(
        "/data/vipdoc",
        "SH",
        "600000",
        period="daily",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 3, 31),
        formula_ids=("indicator_three", "indicator_two", "indicator_one"),
    )

    assert [panel["id"] for panel in chart.formula_panels] == [
        "indicator_three",
        "indicator_two",
        "indicator_one",
    ]
    assert all(len(panel["dates"]) == 91 for panel in chart.formula_panels)
    assert chart.formula_panels[0]["display_name"] == "准备拉升"
    assert chart.formula_panels[0]["values"]
    assert chart.formula_panels[0]["values"][0]["latest"] is not None


def test_rejects_unknown_or_empty_local_instrument() -> None:
    service = LocalMarketDataService(FakeMarketDataAdapter())

    with pytest.raises(ValueError, match="找不到本地行情"):
        service.get_chart("/data/vipdoc", "SH", "601999")
