from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from aquest.market_data.store import DuckDbMarketDataStore
from aquest.strategy_discovery.duckdb_job_store import DuckDbStrategyJobStore
from aquest.strategy_pool.holdings_benchmark import (
    HOLDINGS_BENCHMARK_LABEL,
    holdings_benchmark,
)
from aquest.strategy_pool.tracking_store import dumps

_DAYS = ["2026-11-23", "2026-11-24", "2026-11-25", "2026-11-26"]


def _bars(days: list[str], closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime(days),
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1000.0] * len(days),
            "amount": [10000.0] * len(days),
            "bar_status": ["completed"] * len(days),
        }
    )


def _archive(
    store: DuckDbMarketDataStore,
    day: str,
    *,
    buys: list[str],
    exits: list[str],
) -> None:
    with store.state_write_transaction() as connection:
        connection.execute(
            "INSERT INTO pool_simulation_signals VALUES (?, ?, ?, ?, ?)",
            [
                date.fromisoformat(day),
                "annual_return",
                f"job-{day}",
                dumps(
                    {
                        "buys": [{"market": "SH", "code": code} for code in buys],
                        "exits": [{"market": "SH", "code": code} for code in exits],
                        "rank_version": 2,
                    }
                ),
                datetime.fromisoformat(f"{day}T16:00:00"),
            ],
        )


def test_holdings_benchmark_walks_archives_back_from_the_latest_snapshot(tmp_path):
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars(_DAYS, [10.0, 11.0, 12.0, 13.0]))
    store.replace_local_bars("SH", "600001", "stock", _bars(_DAYS, [20.0, 20.0, 22.0, 22.0]))
    store.replace_local_bars("SH", "600002", "stock", _bars(_DAYS, [5.0, 5.0, 5.0, 6.0]))
    jobs = DuckDbStrategyJobStore(store)
    job = "anchor"
    jobs.create(
        job,
        operation="market_strategy_pool_tracking",
        payload={"mode": "signals", "as_of_date": _DAYS[-1]},
        description="扫描",
    )
    jobs.write_result(
        job,
        {
            "as_of_date": _DAYS[-1],
            "results_by_objective": {
                "annual_return": {
                    "as_of_date": _DAYS[-1],
                    "model_portfolio": {
                        "available": True,
                        "holdings": [
                            {"market": "SH", "code": "600000"},
                            {"market": "SH", "code": "600001"},
                            {"market": "SH", "code": "600002"},
                        ],
                    },
                },
            },
        },
    )
    # 11-24 买入 C 并卖出 A（都在 11-25 开盘成交）：11-24 收盘还持有 A、B。
    _archive(store, "2026-11-24", buys=["600002"], exits=["600000"])
    _archive(store, "2026-11-25", buys=[], exits=[])
    _archive(store, "2026-11-23", buys=[], exits=[])

    result = holdings_benchmark(store, objective="annual_return", equity_dates=_DAYS)

    assert result is not None
    assert result["label"] == HOLDINGS_BENCHMARK_LABEL
    assert result["dates"] == _DAYS
    # 11-24 收益取 11-23 收盘持仓 {A,B}：mean(+10%, 0%) = 5%。
    # 11-25 取 11-24 收盘持仓 {A,B}：mean(12/11-1, 22/20-1)。
    # 11-26 取 11-25 收盘持仓 {A,B,C}：mean(13/12-1, 0, 6/5-1)。
    first = 1 + (0.10 + 0.0) / 2
    second = first * (1 + ((12 / 11 - 1) + (22 / 20 - 1)) / 2)
    third = second * (1 + ((13 / 12 - 1) + 0.0 + (6 / 5 - 1)) / 3)
    assert result["totals"] == pytest.approx([1.0, first, second, third])


def test_holdings_benchmark_needs_a_snapshot_and_two_dates(tmp_path):
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars(_DAYS, [10.0, 11.0, 12.0, 13.0]))

    assert holdings_benchmark(store, objective="annual_return", equity_dates=_DAYS[:1]) is None
    assert holdings_benchmark(store, objective="annual_return", equity_dates=_DAYS) is None
