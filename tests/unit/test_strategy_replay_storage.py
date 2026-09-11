from __future__ import annotations

from datetime import datetime, timedelta, timezone

from selector_app.market_data.store import DuckDbMarketDataStore


def _experiment() -> dict[str, object]:
    return {
        "request": {"base_config": {"start_date": "2024-01-01", "end_date": "2024-12-31"}},
        "search_metadata": {"evaluated_trials": 1},
        "validation_metadata": {},
        "algorithm_metadata": {},
        "data_fingerprint": "source-fingerprint",
        "results_by_sharpe": [],
        "results_by_return": [],
        "duration_seconds": 1.0,
    }


def test_strategy_replay_history_round_trips_and_is_removed_with_experiment(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.save_strategy_discovery_history("experiment-1", _experiment())
    started = datetime(2026, 9, 11, 8, 30, tzinfo=timezone.utc)
    finished = started + timedelta(seconds=3)
    replay = {
        "experiment_id": "experiment-1",
        "leaderboard": "sharpe",
        "data_start": "2024-01-01",
        "data_end": "2026-09-11",
        "data_fingerprint": "replay-fingerprint",
        "data_changed": True,
        "candidate_count": 1,
        "candidates": [{"candidate_hash": "candidate-1"}],
    }

    store.save_strategy_discovery_replay_history(
        "replay-1", replay, started_at=started, finished_at=finished
    )

    record = store.read_strategy_discovery_replay_history("experiment-1", "sharpe")
    assert record is not None
    assert record["replay_id"] == "replay-1"
    assert record["duration_seconds"] == 3.0
    assert record["result"]["candidates"][0]["candidate_hash"] == "candidate-1"

    assert store.delete_strategy_discovery_history("experiment-1") is True
    assert store.read_strategy_discovery_replay_history("experiment-1", "sharpe") is None


def test_strategy_history_retention_removes_orphaned_replays(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    base_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index in range(20):
        store.save_strategy_discovery_history(
            f"experiment-{index}",
            _experiment(),
            created_at=base_time + timedelta(days=index),
        )
    store.save_strategy_discovery_replay_history(
        "replay-1",
        {
            "experiment_id": "experiment-0",
            "leaderboard": "sharpe",
            "data_start": "2024-01-01",
            "data_end": "2024-12-31",
            "data_fingerprint": "fingerprint",
            "candidates": [],
        },
        started_at=base_time,
        finished_at=base_time + timedelta(seconds=1),
    )
    store.save_strategy_discovery_history(
        "experiment-20",
        _experiment(),
        created_at=base_time + timedelta(days=20),
    )

    assert store.read_strategy_discovery_history("experiment-0") is None
    assert store.read_strategy_discovery_replay_history("experiment-0", "sharpe") is None
