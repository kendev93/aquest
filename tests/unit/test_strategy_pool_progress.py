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
    monkeypatch.setattr("selector_app.strategy_pool.service.time.time", lambda: 1_790_000_000.0)

    coordinator.ticker_progress(1, "SH:600000", 0.2, "search", "first", name="浦发银行")
    timestamp = 11.0
    coordinator.ticker_progress(1, "SH:600000", 0.4, "search", "second", name="浦发银行")
    timestamp = 11.6
    coordinator.ticker_progress(1, "SH:600000", 0.6, "search", "third", name="浦发银行")

    assert len(emitted) == 2
    assert emitted[-1][3]["phase_label"] == "third"
    assert emitted[-1][3]["active_symbols"] == [
        {
            "symbol": "SH:600000",
            "name": "浦发银行",
            "started_at": 1_790_000_000.0,
            "progress": 0.6,
            "phase": "search",
            "phase_label": "third",
            "work_details": {},
        }
    ]


def test_ticker_start_time_is_preserved_across_research_phases(monkeypatch) -> None:
    from selector_app.strategy_pool.service import _TickerProgress

    now = 1_790_000_000.0
    emitted = []
    monkeypatch.setattr("selector_app.strategy_pool.service.time.time", lambda: now)
    progress = _TickerProgress(
        index=1,
        total=1,
        symbol="SH:600000",
        name="浦发银行",
        callback=lambda current, total, phase, metadata: emitted.append(metadata),
    )
    progress.start()
    now += 65
    progress.report_detail({"phase": "风控寻优", "phase_label": "规则 2/5"})
    progress(60, 100)

    assert emitted[0]["ticker_started_at"] == 1_790_000_000.0
    assert emitted[-1]["ticker_started_at"] == 1_790_000_000.0
    assert emitted[-1]["ticker_name"] == "浦发银行"
    assert emitted[-1]["phase_label"] == "规则 2/5"


def test_concurrent_stocks_report_independent_start_times(monkeypatch) -> None:
    emitted = []
    monotonic = 10.0
    monkeypatch.setattr("selector_app.strategy_pool.service.time.monotonic", lambda: monotonic)
    coordinator = _PoolProgressCoordinator(
        total_symbols=2,
        total_work=2,
        maximum_workers=2,
        callback=lambda current, total, phase, metadata: emitted.append(metadata),
    )
    coordinator.ticker_progress(1, "SH:600000", 0.1, "train", "first", started_at=1_790_000_000.0)
    monotonic += 2
    coordinator.ticker_progress(2, "SH:600004", 0.1, "train", "second", started_at=1_790_000_010.0)
    assert [item["started_at"] for item in emitted[-1]["active_symbols"]] == [
        1_790_000_000.0,
        1_790_000_010.0,
    ]


def test_ticker_progress_preserves_structured_work_details():
    from selector_app.strategy_pool.service import _TickerProgress

    messages = []
    progress = _TickerProgress(
        index=1,
        total=1,
        symbol="SH600000",
        name="test",
        callback=lambda *args: messages.append(args),
        coordinator=None,
    )
    progress.report_detail(
        {
            "phase": "寻优",
            "phase_label": "完整精度复核",
            "work_details": {"current": 12, "total": 40},
        }
    )
    progress(60, 100)
    assert messages[-1][3]["work_details"] == {"current": 12, "total": 40}
