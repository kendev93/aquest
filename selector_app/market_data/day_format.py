"""Small, application-owned reader for TongDaXin daily ``.day`` files."""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .models import InstrumentBoard, InstrumentType

_DAILY_RECORD = struct.Struct("<IIIIIfII")
# Exposed so callers can detect a truncated file without decoding it again.
DAY_RECORD_SIZE = _DAILY_RECORD.size
_FILENAME = re.compile(r"^(sh|sz)(\d{6})\.day$", re.IGNORECASE)
_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_MARKET_CLOSE = (15, 5)


class DayFileError(ValueError):
    """Raised when a daily file cannot be safely normalized."""


@dataclass(frozen=True)
class DayFileResult:
    frame: pd.DataFrame
    instrument_type: InstrumentType
    board: InstrumentBoard
    tail_bytes: int
    price_coefficient: float
    volume_coefficient: float


def classify_board(market: str, code: str) -> InstrumentBoard | None:
    """Classify all known SH/SZ daily-file code ranges.

    The market is required because the same six-digit code can represent a
    stock on one exchange and an index on the other (for example 000001).
    Unknown ranges are rejected instead of silently applying stock units.
    """

    normalized_market = market.upper().strip()
    normalized_code = code.strip()
    if normalized_market not in {"SH", "SZ"} or not re.fullmatch(r"\d{6}", normalized_code):
        return None
    head = normalized_code[:2]
    if normalized_market == "SH":
        if head == "60":
            return "main"
        if head == "68":
            return "star"
        # 90 开头是沪市 B 股：B 股不进候选池，直接不识别。
        return None
    if head == "00":
        return "main"
    if head == "30":
        return "chinext"
    # 20 开头是深市 B 股，同上。
    return None


def classify_instrument(market: str, code: str) -> InstrumentType | None:
    board = classify_board(market, code)
    if board is not None:
        return "stock"
    return None


def _coefficients(
    market: str,
    code: str,
    instrument_type: InstrumentType,
) -> tuple[float, float]:
    # 只研究股票，系数不再有品种分支（沪市 B 股的 0.001 特例随 B 股一起删掉）。
    del market, code, instrument_type
    return (0.01, 0.01)


def _normalized_now(now: datetime | None) -> datetime:
    current = now or datetime.now(_SHANGHAI_TZ)
    if current.tzinfo is None:
        return current.replace(tzinfo=_SHANGHAI_TZ)
    return current.astimezone(_SHANGHAI_TZ)


def _bar_status(bar_date: object, now: datetime) -> str:
    if (now.hour, now.minute) < _MARKET_CLOSE and pd.Timestamp(bar_date).date() == now.date():
        return "provisional"
    return "completed"


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
            "bar_status",
        ]
    )


def read_day_file(filepath: str | Path, *, now: datetime | None = None) -> DayFileResult:
    """Decode a TDX daily file into the project's normalized DataFrame."""

    path = Path(filepath).expanduser()
    if not path.is_file():
        raise DayFileError(f"日线数据文件不存在: {path}")
    match = _FILENAME.fullmatch(path.name)
    if match is None:
        raise DayFileError(f"日线文件名格式无效: {path.name}")
    market = match.group(1).upper()
    code = match.group(2)
    instrument_type = classify_instrument(market, code)
    if instrument_type is None:
        raise DayFileError(f"未知证券类型: {market} {code}")
    price_coefficient, volume_coefficient = _coefficients(market, code, instrument_type)
    raw = path.read_bytes()
    record_size = _DAILY_RECORD.size
    complete_size = len(raw) - len(raw) % record_size
    tail_bytes = len(raw) - complete_size
    if complete_size == 0:
        return DayFileResult(
            frame=_empty_frame(),
            instrument_type=instrument_type,
            board=classify_board(market, code) or "main",
            tail_bytes=tail_bytes,
            price_coefficient=price_coefficient,
            volume_coefficient=volume_coefficient,
        )

    current = _normalized_now(now)
    rows: list[dict[str, object]] = []
    for offset in range(0, complete_size, record_size):
        date_int, opening, high, low, close, amount, volume, _reserved = _DAILY_RECORD.unpack(
            raw[offset : offset + record_size]
        )
        try:
            bar_date = datetime(
                date_int // 10000,
                (date_int % 10000) // 100,
                date_int % 100,
            )
        except ValueError as exc:
            raise DayFileError(f"日期无效: {date_int} ({path.name})") from exc
        rows.append(
            {
                "date": pd.Timestamp(bar_date),
                "open": float(opening * price_coefficient),
                "high": float(high * price_coefficient),
                "low": float(low * price_coefficient),
                "close": float(close * price_coefficient),
                "volume": float(volume * volume_coefficient),
                "amount": float(amount),
                "bar_status": _bar_status(bar_date, current),
            }
        )
    frame = pd.DataFrame(rows, columns=list(_empty_frame().columns))
    frame = frame.sort_values("date", kind="stable").reset_index(drop=True)
    return DayFileResult(
        frame=frame,
        instrument_type=instrument_type,
        board=classify_board(market, code) or "main",
        tail_bytes=tail_bytes,
        price_coefficient=price_coefficient,
        volume_coefficient=volume_coefficient,
    )


def discover_day_files(vipdoc_path: str | Path, universe: str = "all") -> list[Path]:
    """Find known exchange daily directories without traversing arbitrary files."""

    root = Path(vipdoc_path).expanduser()
    if not root.is_dir():
        raise DayFileError(f"vipdoc 目录不存在或不是目录: {root}")
    normalized_universe = universe.lower()
    if normalized_universe not in {"all", "sh", "sz"}:
        raise DayFileError(f"不支持的行情范围: {universe}")
    markets = ("sh", "sz") if normalized_universe == "all" else (normalized_universe,)
    files: list[Path] = []
    for market in markets:
        files.extend(sorted((root / market / "lday").glob(f"{market}??????.day")))
    return files
