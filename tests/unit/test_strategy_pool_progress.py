from __future__ import annotations

from selector_app.strategy_pool.service import _PoolProgressCoordinator


def test_pool_progress_coalesces_updates_to_reduce_job_store_writes(monkeypatch) -> None:
    timestamp = 10.0
    emitted: list[tuple[int, int, str, object]] = []
    coordinator = _PoolProgressCoordinator(
        total_symbols=10,
        total_work=10,
        callback=lambda current, total, phase, metadata: emitted.append(
            (current, total, phase, metadata)
        ),
        maximum_workers=4,
    )
    monkeypatch.setattr("selector_app.strategy_pool.service.time.monotonic", lambda: timestamp)

    coordinator.ticker_progress(1, "SH:600000", 0.2, "search", "first")
    timestamp = 11.0
    coordinator.ticker_progress(1, "SH:600000", 0.4, "search", "second")
    timestamp = 11.6
    coordinator.ticker_progress(1, "SH:600000", 0.6, "search", "third")

    assert len(emitted) == 2
    assert emitted[-1][3]["phase_label"] == "third"
    assert emitted[-1][3]["active_symbols"] == [
        {
            "symbol": "SH:600000",
            "progress": 0.6,
            "phase": "search",
            "phase_label": "third",
        }
    ]
