from __future__ import annotations

from collections.abc import Mapping
from typing import cast

import pandas as pd
import pytest

from aquest.adapters.online_market_sync import (
    OnlineDataError,
    OnlineMarketClient,
    OnlineProviderError,
)
from aquest.market_data.models import MarketCode


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
    # 股票目录必须在服务端就按板块收窄，否则会拉全市场 59k 条再丢掉大半。
    assert transport.calls[0][1]["fs"] == "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23"
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
    # fqt=0：不复权。复权价不能当数据源（每发生一次除权全部历史 bar 都会变），
    # 除权跳空由 market_data.adjustment 在计算期消除。
    assert transport.calls[0][1]["fqt"] == "0"


def test_eastmoney_empty_kline_list_is_an_explicit_valid_no_data_response() -> None:
    transport = FakeTransport([{"data": {"klines": []}}])

    frame = OnlineMarketClient(transport=transport).fetch_daily_history(
        "600000", "20260910", "20260911", timeout=5.0
    )

    assert frame.empty
    assert frame.attrs["selector_empty_response_status"] == "valid_no_data"


@pytest.mark.parametrize("payload", [{}, {"data": None}, {"data": {}}, {"data": {"klines": None}}])
def test_eastmoney_missing_empty_payload_shape_is_not_valid_no_data(payload: object) -> None:
    with pytest.raises(OnlineDataError, match="data|klines"):
        OnlineMarketClient(transport=FakeTransport([payload])).fetch_daily_history(
            "600000", "20260910", "20260911", timeout=5.0
        )


@pytest.mark.parametrize(
    ("code", "expected_secid"),
    [
        ("600000", "1.600000"),  # 沪市股票
        ("000001", "0.000001"),  # 深市股票
        ("600000", "1.600000"),  # 沪市主板
        ("688001", "1.688001"),  # 科创板
        ("300001", "0.300001"),  # 创业板
        ("000001", "0.000001"),  # 深市主板
    ],
)
def test_fetch_daily_history_uses_the_right_market_prefix(code: str, expected_secid: str) -> None:
    """secid 前缀填错了在线数据源就返回空 K 线。"""

    transport = FakeTransport([{"data": {"klines": ["2026-09-10,10,10,10,10,1,10,0,0,0,0,1"]}}])

    OnlineMarketClient(transport=transport).fetch_daily_history(
        code, "20260910", "20260910", timeout=8.0
    )

    assert transport.calls[0][1]["secid"] == expected_secid


def test_fetch_tencent_daily_history_decodes_jsonp_and_normalizes_units() -> None:
    # 原始成交量 100000 手、成交额 10500 万元 ⇒ 均价 10.5 = close，量纲自洽。
    transport = FakeTransport(
        [
            'kline_day2026={"code":0,"data":{"sh600000":{"day":['
            '["2026-09-10","10.0","10.5","10.6","9.9","100000.00",{},'
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
    # param 第 6 段留空 = 不复权，响应 key 随之是 day。
    assert transport.calls[0][1]["param"] == "sh600000,day,2026-01-01,2027-12-31,640,"


def test_tencent_empty_day_list_is_an_explicit_valid_no_data_response() -> None:
    transport = FakeTransport(['kline_day2026={"code":0,"data":{"sh600000":{"day":[]}}}'])

    frame = OnlineMarketClient(transport=transport).fetch_tencent_daily_history(
        "SH", "600000", "20260910", "20260911", timeout=5.0
    )

    assert frame.empty
    assert frame.attrs["selector_empty_response_status"] == "valid_no_data"


@pytest.mark.parametrize(
    "payload",
    [
        'kline_day2026={"code":0}',
        'kline_day2026={"code":0,"data":null}',
        'kline_day2026={"code":0,"data":{"sh600000":{}}}',
        'kline_day2026={"code":0,"data":{"sh600000":{"day":null}}}',
    ],
)
def test_tencent_missing_day_payload_shape_is_not_valid_no_data(payload: str) -> None:
    with pytest.raises(OnlineDataError, match="data|数据对象|day"):
        OnlineMarketClient(transport=FakeTransport([payload])).fetch_tencent_daily_history(
            "SH", "600000", "20260910", "20260911", timeout=5.0
        )


@pytest.mark.parametrize(
    ("market", "code", "expected_volume"),
    [
        ("SH", "600000", 100_000.0),  # 沪主板：手，原样入库
        ("SZ", "000001", 100_000.0),  # 深主板：手（"sz000" 曾误伤）
        ("SZ", "002415", 100_000.0),  # 中小板：手
        ("SZ", "300750", 100_000.0),  # 创业板：手
        ("SH", "688981", 1_000.0),  # 科创板 688：腾讯给股，股 → 手
        ("SH", "689009", 1_000.0),  # 科创板 689：同为股，曾因只判 688 被漏掉
        ("SH", "510300", 100_000.0),  # 沪 ETF：手
        ("SZ", "159915", 100_000.0),  # 深 ETF：手
        ("SH", "000300", 100_000.0),  # 沪指数：手（"sh000" 曾误伤）
        ("SZ", "399006", 100_000.0),  # 深指数：手（"sz399" 曾误伤）
    ],
)
def test_fetch_tencent_daily_history_scales_units_per_board(
    market: str, code: str, expected_volume: float
) -> None:
    """成交量统一以"手"入库，只有科创板（688/689）需要从"股"换算。

    成交额一律万元 → 元、换手率一律百分数 → 小数，这两项与板块无关。
    旧实现把三者绑在同一个前缀判断里：深市 000/001 被 "sz000" 误伤后成交量
    少算 100 倍，科创板与两个指数前缀被排除后成交额少算 1 万倍。
    """

    symbol = f"{market.lower()}{code}"
    transport = FakeTransport(
        [
            'kline_day2026={"code":0,"data":{"'
            f"{symbol}"
            '":{"day":['
            '["2026-09-10","10.0","10.5","10.6","9.9","100000.00",{},'
            '"1.20","10500.00","0.00","0.00"]'
            "]}}}"
        ]
    )

    frame = OnlineMarketClient(transport=transport).fetch_tencent_daily_history(
        cast(MarketCode, market),
        code,
        "20260910",
        "20260910",
        timeout=9.0,
    )

    assert frame["volume"].tolist() == [expected_volume]
    assert frame["amount"].tolist() == [105_000_000.0]
    assert frame["turnover"].tolist() == [0.012]


def test_online_client_rejects_malformed_eastmoney_payload() -> None:
    transport = FakeTransport([{"data": {"klines": "not-a-list"}}])

    with pytest.raises(OnlineDataError, match="日线"):
        OnlineMarketClient(transport=transport).fetch_daily_history(
            "600000",
            "20260910",
            "20260911",
            timeout=5.0,
        )


def test_security_list_keeps_paging_when_the_server_caps_page_size() -> None:
    """East Money caps a page at 100 rows even when ``pz`` asks for more.

    Stopping on "short page" truncates the catalog to the first page, which is
    sorted by code descending and holds no A-share at all - the sync then finds
    zero targets and reports a clean success.
    """

    def page_of(start: int, count: int) -> dict[str, object]:
        return {
            "rc": 0,
            "data": {
                "total": 250,
                "diff": [
                    {"f12": f"{start + index:06d}", "f14": f"样本{index}"} for index in range(count)
                ],
            },
        }

    transport = FakeTransport(
        [
            page_of(0, 100),  # 服务端把 pz=5000 截成 100
            page_of(100, 100),
            page_of(200, 50),
        ]
    )

    frame = OnlineMarketClient(transport=transport).fetch_instruments(timeout=5.0)

    assert len(frame) == 250
    assert [call[1]["pn"] for call in transport.calls] == ["1", "2", "3"]


def test_security_list_stops_on_a_short_page_when_total_is_unknown() -> None:
    transport = FakeTransport(
        [
            {"data": {"diff": [{"f12": "600000", "f14": "浦发银行"}]}},
            {"data": {"diff": []}},
        ]
    )

    frame = OnlineMarketClient(transport=transport, page_size=100).fetch_instruments(timeout=5.0)

    assert len(frame) == 1
    assert len(transport.calls) == 1


def test_security_list_pages_on_when_total_is_unknown_and_the_server_caps_at_100() -> None:
    """total 缺失时也要按服务端真实每页上限（100）判断，不能拿请求的 pz=5000 比。

    拿 5000 比的话，100 行的满页会被当成"最后一页"，同步只取到第一页就收工；
    而目录按代码倒序返回，第一页全是非 A 股代码，过滤后一个不剩 —— 任务报成功，
    研究却在空数据上跑。
    """

    def page_of(offset: int) -> dict[str, object]:
        return {
            "data": {
                "diff": [
                    {"f12": f"{600000 - offset - index:06d}", "f14": f"样本{index}"}
                    for index in range(100)
                ]
            }
        }

    transport = FakeTransport([page_of(0), page_of(100), {"data": {"diff": []}}])

    frame = OnlineMarketClient(transport=transport).fetch_instruments(timeout=5.0)

    assert len(frame) == 200
    assert [call[1]["pn"] for call in transport.calls] == ["1", "2", "3"]


def test_security_list_raises_when_the_catalog_comes_back_short() -> None:
    """提前遇到空页会 break，之后必须拿 total 对账，否则目录被截断却报成功。"""

    transport = FakeTransport(
        [
            {"data": {"total": 250, "diff": [{"f12": "600000", "f14": "浦发银行"}]}},
            {"data": {"total": 250, "diff": []}},
        ]
    )

    with pytest.raises(OnlineDataError, match="不完整"):
        OnlineMarketClient(transport=transport).fetch_instruments(timeout=5.0)


def test_security_list_rejects_eastmoney_soft_failure(monkeypatch) -> None:
    """HTTP 200 + ``rc != 0`` must not be mistaken for "no securities".

    East Money answers throttling this way.  Treating it as an empty list makes a
    broken sync look like a successful one with zero targets.
    """

    from aquest.adapters import online_market_sync as module

    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    # 单页重试是"时间预算 + 次数上限"；测试把上限压到 3，预算给足以免提前退出。
    monkeypatch.setattr(module, "_CATALOG_RETRY_SECONDS", 60.0)
    monkeypatch.setattr(module, "_CATALOG_MAX_ATTEMPTS", 3)
    transport = FakeTransport([{"rc": 102, "data": None}] * 3)

    with pytest.raises(OnlineProviderError, match="rc=102"):
        OnlineMarketClient(transport=transport).fetch_instruments(timeout=5.0)

    # 失败要重试到上限，而不是第一次就放弃。
    assert len(transport.calls) == 3


def test_security_list_retries_soft_failure_then_succeeds(monkeypatch) -> None:
    from aquest.adapters import online_market_sync as module

    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    transport = FakeTransport(
        [
            {"rc": 102, "data": None},
            {"rc": 0, "data": {"total": 1, "diff": [{"f12": "600000", "f14": "浦发银行"}]}},
        ]
    )

    frame = OnlineMarketClient(transport=transport).fetch_instruments(timeout=5.0)

    assert frame.to_dict(orient="records") == [{"代码": "600000", "名称": "浦发银行"}]


def test_security_list_rotates_hosts_when_one_node_is_throttled(monkeypatch) -> None:
    """East Money 的 clist 节点各自独立限流，重试必须换节点而不是打同一个。"""

    from aquest.adapters import online_market_sync as module

    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    transport = FakeTransport(
        [
            {"rc": 102, "data": None},  # 第一个节点被限流
            {"rc": 0, "data": {"total": 1, "diff": [{"f12": "600000", "f14": "浦发银行"}]}},
        ]
    )

    OnlineMarketClient(transport=transport).fetch_instruments(timeout=5.0)

    first_host = transport.calls[0][0].split("/")[2]
    second_host = transport.calls[1][0].split("/")[2]
    assert first_host != second_host
    # 路径保持不变，只是换节点。
    assert transport.calls[0][0].endswith("/api/qt/clist/get")
    assert transport.calls[1][0].endswith("/api/qt/clist/get")


def test_empty_security_list_stays_empty_when_the_provider_reports_success() -> None:
    """``rc == 0`` with no rows is a genuinely empty catalog, not an error."""

    transport = FakeTransport([{"rc": 0, "data": {"total": 0, "diff": []}}])

    frame = OnlineMarketClient(transport=transport).fetch_instruments(timeout=5.0)

    assert frame.empty
