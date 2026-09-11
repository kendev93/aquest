from __future__ import annotations

import struct
import time
import zlib

import pandas as pd
import pytest

from selector_app.tdx_protocol.client import TdxClient
from selector_app.tdx_protocol.codec import HEADER_SIZE, decode_frame_header, decompress_body
from selector_app.tdx_protocol.commands import GetSecurityBars, GetSecurityCount, GetSecurityList
from selector_app.tdx_protocol.errors import TdxConnectionError, TdxDecodeError
from selector_app.tdx_protocol.types import KlineCategory, Market


def _put_price(value: int) -> bytes:
    negative = value < 0
    value = abs(value)
    first = value & 0x3F
    value >>= 6
    if negative:
        first |= 0x40
    if value:
        first |= 0x80
    result = bytearray([first])
    while value:
        current = value & 0x7F
        value >>= 7
        if value:
            current |= 0x80
        result.append(current)
    return bytes(result)


def test_daily_bar_request_matches_the_tdx_wire_layout() -> None:
    request = GetSecurityBars(Market.SH, "600000", KlineCategory.DAY, 0, 800).build_request()

    assert len(request) == 38
    assert request[0:2] == b"\x0c\x01"
    assert request[-2:] == b"\x00\x00"


def test_daily_bar_response_decodes_differential_ohlc() -> None:
    body = b"\x01\x00"
    body += struct.pack("<I", 20240102)
    body += _put_price(10_000)
    body += _put_price(100)
    body += _put_price(500)
    body += _put_price(-200)
    body += struct.pack("<II", 0, 0)

    bars = GetSecurityBars(Market.SH, "600000", KlineCategory.DAY, 0, 1).parse_response(body)

    assert len(bars) == 1
    assert bars[0].open == pytest.approx(10.0)
    assert bars[0].close == pytest.approx(10.1)
    assert bars[0].high == pytest.approx(10.5)
    assert bars[0].low == pytest.approx(9.8)


def test_daily_bar_response_handles_empty_and_truncated_payloads() -> None:
    command = GetSecurityBars(Market.SH, "600000", KlineCategory.DAY, 0, 2)

    assert command.parse_response(b"") == []
    assert command.parse_response(b"\x02\x00") == []


def test_index_bar_response_skips_breadth_fields_between_records() -> None:
    body = b"\x02\x00"
    for day, opening in ((20240102, 10_000), (20240103, 10_100)):
        body += struct.pack("<I", day)
        body += _put_price(opening if day == 20240102 else 100)
        body += _put_price(100)
        body += _put_price(500)
        body += _put_price(-200)
        body += struct.pack("<IIHH", 0, 0, 10, 20)

    bars = GetSecurityBars(
        Market.SH,
        "000001",
        KlineCategory.DAY,
        0,
        2,
        is_index=True,
    ).parse_response(body)

    assert len(bars) == 2
    assert bars[1].open == pytest.approx(10.2)


def test_count_and_security_list_requests_are_available() -> None:
    assert len(GetSecurityCount(Market.SH).build_request()) == 18
    assert len(GetSecurityList(Market.SH, 0).build_request()) == 18


def test_security_list_all_fetches_every_reported_page(monkeypatch: pytest.MonkeyPatch) -> None:
    client = object.__new__(TdxClient)

    def get_count(market: Market, *, deadline: float | None = None) -> int:
        assert deadline is not None
        return 1001 if market is Market.SH else 0

    def get_page(
        market: Market,
        start: int,
        *,
        deadline: float | None = None,
    ) -> object:
        assert deadline is not None
        assert market is Market.SH
        if start == 0:
            return pd.DataFrame([{"market": "SH", "code": f"{index:06d}"} for index in range(1000)])
        return pd.DataFrame([{"market": "SH", "code": "001000"}])

    monkeypatch.setattr(client, "get_security_count", get_count)
    monkeypatch.setattr(client, "get_security_list", get_page)

    result = client.get_security_list_all()

    assert len(result) == 1001
    assert result.iloc[-1]["code"] == "001000"


def test_security_list_all_does_not_hide_a_failed_page(monkeypatch: pytest.MonkeyPatch) -> None:
    client = object.__new__(TdxClient)

    monkeypatch.setattr(
        client,
        "get_security_count",
        lambda market, *, deadline=None: 1001 if market is Market.SH else 0,
    )
    monkeypatch.setattr(
        client,
        "get_security_list",
        lambda _market, start, *, deadline=None: (
            pd.DataFrame([{"market": "SH", "code": "600000"}])
            if start == 0
            else (_ for _ in ()).throw(TdxConnectionError("分页连接失败"))
        ),
    )

    with pytest.raises(TdxConnectionError, match="证券列表分页"):
        client.get_security_list_all()


def test_tdx_request_stops_immediately_after_security_list_deadline() -> None:
    class UnexpectedConnection:
        def set_timeout(self, timeout: float) -> None:
            del timeout

        def execute(self, command: object) -> object:
            del command
            raise AssertionError("expired request must not reach the socket")

    client = object.__new__(TdxClient)
    client._connection = UnexpectedConnection()
    client._timeout = 30.0
    client._hosts = []

    with pytest.raises(TdxConnectionError, match="证券列表请求超时"):
        client._execute(GetSecurityCount(Market.SH), deadline=time.monotonic() - 1)


def test_daily_bar_request_rejects_unsafe_page_size() -> None:
    with pytest.raises(ValueError, match="分页参数"):
        GetSecurityBars(Market.SH, "600000", KlineCategory.DAY, 0, 801)


def test_frame_decoder_supports_uncompressed_and_zlib_bodies() -> None:
    body = b"payload"
    compressed = zlib.compress(body)
    raw_header = struct.pack("<IIIHH", 7654321, 0, 0, len(body), len(body))
    zipped_header = struct.pack("<IIIHH", 7654321, 0, 0, len(compressed), len(body))

    assert HEADER_SIZE == 16
    assert decompress_body(body, decode_frame_header(raw_header)) == body
    assert decompress_body(compressed, decode_frame_header(zipped_header)) == body


def test_frame_decoder_rejects_truncated_body() -> None:
    header = decode_frame_header(struct.pack("<IIIHH", 7654321, 0, 0, 3, 3))

    with pytest.raises(TdxDecodeError):
        decompress_body(b"x", header)
