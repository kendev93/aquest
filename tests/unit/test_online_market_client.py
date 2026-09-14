from __future__ import annotations

from collections.abc import Mapping

import pandas as pd
import pytest

from selector_app.adapters.online_market_sync import OnlineDataError, OnlineMarketClient


class FakeTransport:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, object], float]] = []

    def __call__(self, url: str, params: Mapping[str, object], timeout: float) -> object:
        self.calls.append((url, dict(params), timeout))
        return self.responses.pop(0)


def test_fetch_instruments_maps_eastmoney_rows_and_paginates() -> None:
    transport = FakeTransport(
        [
            {
                "data": {
                    "total": 3,
                    "diff": [
                        {"f12": "600000", "f14": "浦发银行"},
                        {"f12": "000001", "f14": "平安银行"},
                    ],
                }
            },
            {
                "data": {
                    "total": 3,
                    "diff": [{"f12": "688001", "f14": "科创样本"}],
                }
            },
        ]
    )

    frame = OnlineMarketClient(transport=transport, page_size=2).fetch_instruments(timeout=12.5)

    assert list(frame.columns) == ["代码", "名称"]
    assert frame.to_dict(orient="records") == [
        {"代码": "600000", "名称": "浦发银行"},
        {"代码": "000001", "名称": "平安银行"},
        {"代码": "688001", "名称": "科创样本"},
    ]
    assert len(transport.calls) == 2
    assert transport.calls[0][1]["pn"] == "1"
    assert transport.calls[1][1]["pn"] == "2"
    assert transport.calls[0][1]["pz"] == "2"
    assert transport.calls[0][1]["fields"] == "f12,f14"
    assert transport.calls[0][2] == 12.5


def test_fetch_daily_history_maps_eastmoney_kline_rows() -> None:
    transport = FakeTransport(
        [
            {
                "data": {
                    "klines": [
                        "2026-09-10,10.0,10.5,10.6,9.9,1000,10500,7.0,5.0,0.5,1.2,999",
                        "2026-09-11,10.5,10.2,10.7,10.1,1200,12200,5.7,-2.9,-0.3,1.4,998",
                    ]
                }
            }
        ]
    )

    frame = OnlineMarketClient(transport=transport).fetch_daily_history(
        "600000",
        "20260910",
        "20260911",
        timeout=8.0,
    )

    assert list(frame.columns) == [
        "日期",
        "开盘",
        "收盘",
        "最高",
        "最低",
        "成交量",
        "成交额",
        "换手率",
    ]
    assert frame["日期"].tolist() == [pd.Timestamp("2026-09-10"), pd.Timestamp("2026-09-11")]
    assert frame["收盘"].tolist() == [10.5, 10.2]
    assert frame["成交量"].tolist() == [1000.0, 1200.0]
    assert frame["成交额"].tolist() == [10500.0, 12200.0]
    assert transport.calls[0][1]["secid"] == "1.600000"
    assert transport.calls[0][1]["klt"] == "101"
    assert transport.calls[0][1]["fqt"] == "0"


def test_fetch_tencent_daily_history_decodes_jsonp_and_normalizes_units() -> None:
    transport = FakeTransport(
        [
            'kline_day2026={"code":0,"data":{"sh600000":{"day":['
            '["2026-09-10","10.0","10.5","10.6","9.9","1000.00",{},'
            '"1.20","10500.00","0.00","0.00"]'
            "]}}}"
        ]
    )

    frame = OnlineMarketClient(transport=transport).fetch_tencent_daily_history(
        "SH",
        "600000",
        "20260910",
        "20260910",
        timeout=9.0,
    )

    assert frame["date"].tolist() == [pd.Timestamp("2026-09-10").date()]
    assert frame["volume"].tolist() == [100000.0]
    assert frame["amount"].tolist() == [105000000.0]
    assert transport.calls[0][1]["param"] == "sh600000,day,2026-01-01,2027-12-31,640,"


def test_online_client_rejects_malformed_eastmoney_payload() -> None:
    transport = FakeTransport([{"data": {"klines": "not-a-list"}}])

    with pytest.raises(OnlineDataError, match="日线"):
        OnlineMarketClient(transport=transport).fetch_daily_history(
            "600000",
            "20260910",
            "20260911",
            timeout=5.0,
        )


def test_fetch_funds_lofs_and_indexes_uses_catalog_filters() -> None:
    transport = FakeTransport(
        [
            {"data": {"total": 1, "diff": [{"f12": "510300", "f13": "1", "f14": "沪深300ETF"}]}},
            {"data": {"total": 1, "diff": [{"f12": "163406", "f13": "0", "f14": "兴全合润"}]}},
            {"data": {"total": 1, "diff": [{"f12": "000300", "f13": "1", "f14": "沪深300"}]}},
        ]
    )
    client = OnlineMarketClient(transport=transport)

    etf = client.fetch_fund_instruments(timeout=5.0)
    lof = client.fetch_lof_instruments(timeout=5.0)
    indexes = client.fetch_index_instruments(timeout=5.0)

    assert etf.to_dict(orient="records") == [{"代码": "510300", "名称": "沪深300ETF"}]
    assert lof.to_dict(orient="records") == [{"代码": "163406", "名称": "兴全合润"}]
    assert indexes.to_dict(orient="records") == [{"代码": "sh000300", "名称": "沪深300"}]
    assert transport.calls[0][1]["fs"].startswith("b:MK0021")
    assert transport.calls[1][1]["fs"].startswith("b:MK0404")
    assert transport.calls[2][1]["fs"].startswith("m:1 t:2")


def test_fetch_daily_history_for_asset_uses_market_prefix_and_adjustment() -> None:
    transport = FakeTransport(
        [
            {"data": {"klines": ["2026-09-10,3.5,3.6,3.7,3.4,2000,7200,8.0,1.0,0.1,0.2,1"]}},
            {"data": {"klines": ["2026-09-10,3500,3510,3520,3490,0,0,1.0,0.2,10,0,1"]}},
        ]
    )
    client = OnlineMarketClient(transport=transport)

    fund = client.fetch_daily_history_for_asset(
        "510300", "ETF", "2026-09-10", "2026-09-10", timeout=5.0
    )
    index = client.fetch_daily_history_for_asset(
        "sh000300", "Index", "2026-09-10", "2026-09-10", timeout=5.0
    )

    assert fund["close"].tolist() == [3.6]
    assert index["close"].tolist() == [3510.0]
    assert transport.calls[0][1]["secid"] == "1.510300"
    assert transport.calls[0][1]["fqt"] == "1"
    assert transport.calls[1][1]["secid"] == "1.000300"
    assert transport.calls[1][1]["fqt"] == "0"
