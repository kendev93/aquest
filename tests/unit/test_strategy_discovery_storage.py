from __future__ import annotations

from selector_app.market_data.store import DuckDbMarketDataStore


def test_strategy_discovery_history_round_trip_and_limit(tmp_path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    result = {
        "request": {"max_positions": 3},
        "search_metadata": {"evaluated_trials": 10},
        "algorithm_metadata": {"algorithm": "seeded_random_fallback"},
        "data_fingerprint": "abc123",
        "results_by_sharpe": [{"candidate_hash": "s1"}],
        "results_by_return": [{"candidate_hash": "r1"}],
        "duration_seconds": 1.25,
    }

    store.save_strategy_discovery_history("run-1", result)

    records = store.list_strategy_discovery_history()
    detail = store.read_strategy_discovery_history("run-1")
    assert records[0]["history_id"] == "run-1"
    assert records[0]["total_trials"] == 10
    assert records[0]["duration_seconds"] == 1.25
    assert detail is not None
    assert detail["data_fingerprint"] == "abc123"
    assert detail["duration_seconds"] == 1.25
    assert detail["results_by_sharpe"][0]["candidate_hash"] == "s1"
    assert store.delete_strategy_discovery_history("run-1") is True
    assert store.read_strategy_discovery_history("run-1") is None
