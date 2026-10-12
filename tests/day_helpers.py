from __future__ import annotations

import struct
from collections.abc import Iterable
from pathlib import Path

_RECORD = struct.Struct("<IIIIIfII")


def write_day_records(
    path: Path,
    records: Iterable[tuple[int, float, float, float, float, float, float]],
    *,
    price_coefficient: float = 0.01,
    volume_coefficient: float = 0.01,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = b"".join(
        _RECORD.pack(
            date_int,
            round(opening / price_coefficient),
            round(high / price_coefficient),
            round(low / price_coefficient),
            round(close / price_coefficient),
            amount,
            round(volume / volume_coefficient),
            0,
        )
        for date_int, opening, high, low, close, volume, amount in records
    )
    path.write_bytes(encoded)
