from __future__ import annotations

import struct
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from aquest.market_data.day_format import (
    DayFileError,
    classify_instrument,
    read_day_file,
)

_RECORD = struct.Struct("<IIIIIfII")
_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _write_day(path: Path, *dates: int, tail: bytes = b"") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    records = b"".join(
        _RECORD.pack(
            value,
            1000 + index,
            1100 + index,
            900 + index,
            1050 + index,
            123456.5 + index,
            200000 + index,
            0,
        )
        for index, value in enumerate(dates)
    )
    path.write_bytes(records + tail)


@pytest.mark.parametrize(
    ("market", "code", "expected"),
    [
        ("SH", "600000", "stock"),
        ("SH", "688001", "stock"),
        ("SZ", "000001", "stock"),
        ("SZ", "300001", "stock"),
    ],
)
def test_classify_instrument_supports_all_known_day_types(
    market: str, code: str, expected: str
) -> None:
    assert classify_instrument(market, code) == expected


def test_classify_instrument_rejects_non_stock_ranges() -> None:
    """基金、指数、债券与 B 股都不再是本系统的研究对象。"""

    assert classify_instrument("SH", "510300") is None
    assert classify_instrument("SH", "000001") is None
    assert classify_instrument("SH", "110001") is None
    assert classify_instrument("SH", "900001") is None  # 沪市 B 股
    assert classify_instrument("SZ", "159915") is None
    assert classify_instrument("SZ", "399001") is None
    assert classify_instrument("SZ", "110001") is None
    assert classify_instrument("SZ", "200001") is None  # 深市 B 股


def test_classify_instrument_rejects_unknown_market_or_code() -> None:
    assert classify_instrument("BJ", "430001") is None
    assert classify_instrument("SH", "490001") is None
    assert classify_instrument("SH", "not-code") is None


def test_read_day_file_decodes_records_and_ignores_incomplete_tail(tmp_path: Path) -> None:
    path = tmp_path / "sh" / "lday" / "sh600000.day"
    _write_day(path, 20240102, 20240103, tail=b"broken")

    result = read_day_file(
        path,
        now=datetime(2024, 1, 4, 16, 0, tzinfo=_SHANGHAI),
    )

    assert result.instrument_type == "stock"
    assert result.tail_bytes == 6
    assert list(result.frame["date"].dt.strftime("%Y-%m-%d")) == [
        "2024-01-02",
        "2024-01-03",
    ]
    assert result.frame.iloc[0]["open"] == pytest.approx(10.0)
    assert result.frame.iloc[0]["close"] == pytest.approx(10.5)
    assert result.frame.iloc[0]["volume"] == pytest.approx(2000.0)
    assert result.frame.iloc[0]["amount"] == pytest.approx(123456.5)
    assert set(result.frame["bar_status"]) == {"completed"}


def test_read_day_file_rejects_b_share_day_files(tmp_path: Path) -> None:
    """B 股目录里的 .day 文件不再被识别，导入时会整只跳过。"""

    path = tmp_path / "sh" / "lday" / "sh900001.day"
    _write_day(path, 20240102)

    with pytest.raises(DayFileError):
        read_day_file(path, now=datetime(2024, 1, 4, 16, 0, tzinfo=_SHANGHAI))


def test_read_day_file_marks_current_bar_provisional_before_close(tmp_path: Path) -> None:
    path = tmp_path / "sz" / "lday" / "sz000001.day"
    _write_day(path, 20240103, 20240104)

    result = read_day_file(
        path,
        now=datetime(2024, 1, 4, 15, 4, tzinfo=_SHANGHAI),
    )

    assert list(result.frame["bar_status"]) == ["completed", "provisional"]


def test_read_day_file_rejects_invalid_complete_date(tmp_path: Path) -> None:
    path = tmp_path / "sh" / "lday" / "sh600000.day"
    _write_day(path, 20241301)

    with pytest.raises(DayFileError, match="日期无效"):
        read_day_file(path)
