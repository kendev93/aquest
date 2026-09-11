from __future__ import annotations

from datetime import datetime, timedelta, timezone

import duckdb

from selector_app.market_data.store import DuckDbMarketDataStore


def test_backtest_history_round_trip_keeps_timestamps_and_payloads(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    started = datetime(2026, 9, 11, 8, 30, tzinfo=timezone.utc)
    finished = started + timedelta(seconds=2.5)

    store.save_backtest_history(
        "single-1",
        backtest_type="single_stock",
        request={"market": "SH", "code": "600000"},
        result={
            "market": "SH",
            "code": "600000",
            "start_date": "2024-01-01",
            "end_date": "2024-01-10",
            "performance": {"total_return": 0.12, "sharpe": 1.4},
            "trades": [{"direction": "BUY"}],
        },
        started_at=started,
        finished_at=finished,
    )

    summaries = store.list_backtest_history("single_stock")
    detail = store.read_backtest_history("single-1")

    assert summaries[0]["history_id"] == "single-1"
    assert summaries[0]["backtest_type"] == "single_stock"
    assert summaries[0]["started_at"]
    assert summaries[0]["finished_at"]
    assert summaries[0]["duration_seconds"] == 2.5
    assert summaries[0]["summary"]["performance"]["sharpe"] == 1.4
    assert detail is not None
    assert detail["request"] == {"market": "SH", "code": "600000"}
    assert detail["result"]["trades"] == [{"direction": "BUY"}]
    assert detail["started_at"] == summaries[0]["started_at"]
    assert store.delete_backtest_history("single-1") is True
    assert store.read_backtest_history("single-1") is None


def test_backtest_history_retains_latest_twenty_per_type(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)

    for index in range(22):
        started = base + timedelta(days=index)
        store.save_backtest_history(
            f"portfolio-{index}",
            backtest_type="portfolio",
            request={"max_positions": 5},
            result={"performance": {"total_return": index / 100}},
            started_at=started,
            finished_at=started + timedelta(seconds=1),
        )

    history = store.list_backtest_history("portfolio")

    assert len(history) == 20
    assert [item["history_id"] for item in history] == [
        f"portfolio-{index}" for index in range(21, 1, -1)
    ]
    assert store.read_backtest_history("portfolio-0") is None


def test_strategy_discovery_history_upgrade_backfills_missing_timestamps(tmp_path) -> None:
    database = tmp_path / "market.duckdb"
    with duckdb.connect(str(database)) as connection:
        connection.execute(
            "CREATE TABLE data_meta (key VARCHAR PRIMARY KEY, value VARCHAR, updated_at TIMESTAMP)"
        )
        connection.execute(
            "INSERT INTO data_meta VALUES ('schema_version', '7', CURRENT_TIMESTAMP)"
        )
        connection.execute(
            """
            CREATE TABLE strategy_discovery_history (
                history_id VARCHAR PRIMARY KEY,
                created_at TIMESTAMP NOT NULL,
                request_json VARCHAR NOT NULL,
                search_metadata_json VARCHAR NOT NULL,
                validation_metadata_json VARCHAR NOT NULL,
                algorithm_metadata_json VARCHAR NOT NULL,
                data_fingerprint VARCHAR NOT NULL,
                results_by_sharpe_json VARCHAR NOT NULL,
                results_by_return_json VARCHAR NOT NULL,
                total_trials BIGINT NOT NULL,
                duration_seconds DOUBLE NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO strategy_discovery_history VALUES
            ('old-run', TIMESTAMP '2026-01-01 08:00:00', '{}', '{"evaluated_trials": 1}',
             '{}', '{}', '', '[]', '[]', 1, 12.5)
            """
        )

    store = DuckDbMarketDataStore(database)
    detail = store.read_strategy_discovery_history("old-run")

    assert detail is not None
    assert str(detail["started_at"]).startswith("2026-01-01 08:00:00")
    assert str(detail["finished_at"]).startswith("2026-01-01 08:00:12")
