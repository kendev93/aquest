from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from selector_app.market_data.day_format import classify_board
from selector_app.market_data.scope import InstrumentScope
from selector_app.market_data.store import DuckDbMarketDataStore


@pytest.mark.parametrize(
    ("market", "code", "expected"),
    [
        ("SH", "600000", "main"),
        ("SH", "688001", "star"),
        ("SH", "900001", "b_share"),
        ("SZ", "000001", "main"),
        ("SZ", "300001", "chinext"),
        ("SZ", "200001", "b_share"),
        ("SH", "510300", "fund"),
        ("SZ", "399001", "index"),
        ("SZ", "110001", "bond"),
    ],
)
def test_code_ranges_are_exposed_as_stable_board_segments(
    market: str, code: str, expected: str
) -> None:
    assert classify_board(market, code) == expected


def test_unrestricted_scope_matches_every_supported_instrument() -> None:
    scope = InstrumentScope.from_values()

    assert scope.matches("SH", "stock", "star")
    assert scope.matches("SZ", "fund", "fund")
    assert scope.matches("SZ", "bond", "bond")


def test_scope_can_select_stock_boards_without_selecting_funds_or_bonds() -> None:
    scope = InstrumentScope.from_values(
        instrument_types=("stock",),
        boards=("main", "chinext"),
    )

    assert scope.matches("SH", "stock", "main")
    assert scope.matches("SZ", "stock", "chinext")
    assert not scope.matches("SH", "stock", "star")
    assert not scope.matches("SZ", "fund", "fund")


def test_store_list_instruments_filters_by_type_and_board(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-01-02"]),
            "open": [10.0],
            "high": [11.0],
            "low": [9.0],
            "close": [10.5],
            "volume": [1000.0],
            "amount": [10000.0],
            "bar_status": ["completed"],
        }
    )
    store.replace_local_bars("SH", "600000", "stock", frame)
    store.replace_local_bars("SH", "688001", "stock", frame)
    store.replace_local_bars("SH", "510300", "fund", frame)

    refs = store.list_instruments(instrument_types=("stock",), boards=("main",))

    assert [(ref.market, ref.code, ref.board) for ref in refs] == [("SH", "600000", "main")]
