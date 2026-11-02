from collections.abc import Mapping

from aquest.backtest.trades import count_closed_trades


def test_count_closed_trades_ignores_buys_rejections_and_invalid_rows() -> None:
    trades = [
        {"direction": "BUY"},
        {"direction": "SELL"},
        {"direction": "SELL", "rejected": False},
        {"direction": "SELL", "rejected": True},
        "invalid",
    ]

    assert count_closed_trades(trades) == 2


def test_count_closed_trades_normalizes_non_string_directions() -> None:
    trade: Mapping[str, object] = {"direction": "SELL", "rejected": None}

    assert count_closed_trades([trade]) == 1
