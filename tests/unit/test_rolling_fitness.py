from __future__ import annotations

from selector_app.strategy_fitness.rolling import RollingFitnessFilter, RollingFitnessHistory


def test_rolling_fitness_excludes_trades_from_the_current_signal_date() -> None:
    history = RollingFitnessHistory.from_records(
        trades=[
            {
                "date": "2024-01-02",
                "direction": "SELL",
                "pnl": 100.0,
                "cost_basis": 1_000.0,
                "rejected": False,
            }
        ],
        equity=[
            {"date": "2024-01-01", "total": 10_000.0, "drawdown_pct": 0.0},
            {"date": "2024-01-02", "total": 10_100.0, "drawdown_pct": 0.0},
        ],
    )
    fitness = RollingFitnessFilter(
        {"SH600000": history},
        min_trades=1,
        max_drawdown=0.3,
    )

    before_sell = fitness.decide("SH600000", 20240102)
    after_sell = fitness.decide("SH600000", 20240103)

    assert before_sell.trades == 0
    assert before_sell.score == 20.0
    assert before_sell.note.startswith("历史成交不足")
    assert after_sell.trades == 1
    assert after_sell.score == 100.0


def test_rolling_fitness_derives_drawdown_when_raw_equity_has_no_drawdown_field() -> None:
    history = RollingFitnessHistory.from_records(
        trades=[
            {
                "date": "2024-01-02",
                "direction": "SELL",
                "pnl": 100.0,
                "cost_basis": 1_000.0,
                "rejected": False,
            }
        ],
        equity=[
            {"date": "2024-01-01", "total": 10_000.0},
            {"date": "2024-01-02", "total": 8_000.0},
        ],
    )
    fitness = RollingFitnessFilter(
        {"SH600000": history},
        min_trades=1,
        max_drawdown=0.1,
    )

    decision = fitness.decide("SH600000", 20240103)

    assert decision.max_drawdown == 0.2
    assert decision.score == 60.0
    assert decision.note == "历史成交 1 笔"
