from __future__ import annotations

import sqlite3
import time
import warnings
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from threading import Lock

import numpy as np
import pandas as pd
import pytest

from aquest.adapters.local_day_adapter import StockRef
from aquest.backtest.models import BacktestConfig
from aquest.backtest.service import BacktestService
from aquest.formulas.custom import evaluate_custom_formula, parse_formula
from aquest.formulas.registry import FORMULA_REGISTRY, FormulaRegistry
from aquest.indicators.cache import IndicatorCache, frame_fingerprint
from aquest.indicators.precompute import (
    IndicatorPrecomputeService,
    PrecomputeCancelledError,
    _year_windows,
)
from aquest.market_data.adapter import DuckDbMarketDataAdapter
from aquest.market_data.store import (
    DuckDbMarketDataStore,
    resolve_indicator_cache_backend,
)
from aquest.market_data.store_indicator_cache import (
    DuckDbIndicatorCacheStore,
    _fingerprint_matches_revision,
)
from aquest.market_data.store_indicator_cache_sqlite import (
    SqliteIndicatorCacheStore,
    default_indicator_cache_path,
)
from aquest.portfolio_backtest.models import PortfolioBacktestConfig
from aquest.portfolio_backtest.service import PortfolioBacktestService
from aquest.screening.engine import ScreenEngine
from aquest.screening.models import ScanConfig
from aquest.strategy_fitness.models import StrategyFitnessConfig
from aquest.strategy_fitness.service import StrategyFitnessService


def _cache_rows(store: DuckDbMarketDataStore, sql: str) -> list[tuple[object, ...]]:
    """查询缓存表本身，不关心当前挂的是 DuckDB 还是 SQLite 后端。

    两个后端只差在 blob 长度函数名上（``octet_length`` vs ``length``），
    其余 SQL 完全一致，所以这里做一次替换就能共用断言。
    """

    backend = store._indicator_cache
    if isinstance(backend, SqliteIndicatorCacheStore):
        return backend._query(sql.replace("octet_length", "length"))
    with store._connect(read_only=True) as connection:
        return connection.execute(sql).fetchall()


def _cache_row(store: DuckDbMarketDataStore, sql: str) -> tuple[object, ...] | None:
    rows = _cache_rows(store, sql)
    return rows[0] if rows else None


@pytest.fixture(params=["duckdb", "sqlite"])
def backend(request: pytest.FixtureRequest) -> str:
    """让每个持久化用例在两条缓存后端上都跑一遍。

    派生缓存既能留在 DuckDB 也能挪到 SQLite，两条实现必须给出同样的结果；
    参数化比写两遍断言更能保证它们不会各自漂移。
    """

    return str(request.param)


def _bars(length: int = 80, *, start: str = "2024-01-01") -> pd.DataFrame:
    close = 10.0 + np.linspace(0.0, 8.0, length) + np.sin(np.arange(length) / 3.0)
    return pd.DataFrame(
        {
            "date": pd.date_range(start, periods=length, freq="D"),
            "open": close - 0.2,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.full(length, 100_000.0),
            "amount": close * 100_000.0,
            "bar_status": "completed",
        }
    )


def _counting_registry(calls: dict[str, int]) -> FormulaRegistry:
    definition = FORMULA_REGISTRY.formula("indicator_three")
    original = definition.calculate

    def calculate(frame: pd.DataFrame):
        calls["indicator_three"] = calls.get("indicator_three", 0) + 1
        return original(frame)

    return FormulaRegistry((replace(definition, calculate=calculate),))


def test_persistent_cache_reuses_formula_result_without_recalculating(
    tmp_path: Path,
    backend: str,
) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb", indicator_cache_backend=backend)
    frame = _bars()
    calls: dict[str, int] = {}
    registry = _counting_registry(calls)

    first = IndicatorCache(store, registry=registry).get_or_compute(
        frame,
        ("indicator_three",),
        market="SH",
        code="600000",
    )["indicator_three"]
    second = IndicatorCache(store, registry=registry).get_or_compute(
        frame.copy(deep=True),
        ("indicator_three",),
        market="SH",
        code="600000",
    )["indicator_three"]

    assert calls["indicator_three"] == 1
    assert np.array_equal(first.signals["accumulation_zone"], second.signals["accumulation_zone"])
    np.testing.assert_allclose(first.values["varo7"], second.values["varo7"], equal_nan=True)


def test_cache_fingerprint_invalidates_after_market_data_changes(
    tmp_path: Path,
    backend: str,
) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb", indicator_cache_backend=backend)
    frame = _bars()
    calls: dict[str, int] = {}
    registry = _counting_registry(calls)
    cache = IndicatorCache(store, registry=registry)

    cache.get_or_compute(frame, ("indicator_three",), market="SH", code="600000")
    changed = frame.copy(deep=True)
    changed.loc[50, "close"] += 1.0
    cache.get_or_compute(changed, ("indicator_three",), market="SH", code="600000")

    assert frame_fingerprint(frame) != frame_fingerprint(changed)
    assert calls["indicator_three"] == 2


def test_formula_version_and_corrupt_payload_are_cache_misses(tmp_path: Path, backend: str) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb", indicator_cache_backend=backend)
    frame = _bars()
    calls: dict[str, int] = {}
    version_one = _counting_registry(calls)
    cache = IndicatorCache(store, registry=version_one)
    cache.get_or_compute(frame, ("indicator_three",), market="SH", code="600000")

    definition = FORMULA_REGISTRY.formula("indicator_three")
    # 刻意不写死下一个版本号：cache_version 会随语义修正往上走，写死就会在下一次升版
    # 时静默变成"版本没变"，这条用例也就不再验证版本失活了。
    bumped_version = f"{definition.cache_version}-bumped"

    def versioned_calculate(data: pd.DataFrame):
        calls["indicator_three"] = calls.get("indicator_three", 0) + 1
        return definition.calculate(data)

    version_two = FormulaRegistry(
        (replace(definition, cache_version=bumped_version, calculate=versioned_calculate),)
    )
    IndicatorCache(store, registry=version_two).get_or_compute(
        frame,
        ("indicator_three",),
        market="SH",
        code="600000",
    )

    fingerprint = frame_fingerprint(frame)
    store.write_indicator_cache(
        market="SH",
        code="600000",
        timeframe="daily",
        price_basis="raw",
        formula_id="indicator_three",
        formula_version=bumped_version,
        data_fingerprint=fingerprint,
        data_start=date(2024, 1, 1),
        data_end=date(2024, 3, 20),
        data_length=len(frame),
        values_blob=b"not-a-numpy-file",
        signals_blob=b"not-a-numpy-file",
    )
    IndicatorCache(store, registry=version_two).get_or_compute(
        frame,
        ("indicator_three",),
        market="SH",
        code="600000",
    )

    assert calls["indicator_three"] == 3


def test_screen_engine_uses_shared_indicator_cache(tmp_path: Path, backend: str) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb", indicator_cache_backend=backend)
    store.replace_local_bars("SH", "600000", "stock", _bars())
    adapter = DuckDbMarketDataAdapter(store)
    calls: dict[str, int] = {}
    registry = _counting_registry(calls)
    cache = IndicatorCache(store, registry=registry)
    engine = ScreenEngine(adapter=adapter, registry=registry, indicator_cache=cache)
    config = ScanConfig(
        selected_signals=("indicator_three.accumulation_zone",),
        combine_mode="any",
        minimum_matches=None,
        universe="all",
        universe_file=None,
        vipdoc_path=str(tmp_path),
        workers=1,
        period="daily",
    )

    first = engine.scan(config)
    second = engine.scan(config)

    assert calls["indicator_three"] == 1
    assert first.summary_dict() == second.summary_dict()
    assert [match.to_dict() for match in first.results] == [
        match.to_dict() for match in second.results
    ]


def test_backtest_and_portfolio_reuse_the_same_persistent_result(
    tmp_path: Path,
    backend: str,
) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb", indicator_cache_backend=backend)
    store.replace_local_bars("SH", "600000", "stock", _bars(120))
    adapter = DuckDbMarketDataAdapter(store)
    calls: dict[str, int] = {}
    registry = _counting_registry(calls)
    cache = IndicatorCache(store, registry=registry)

    backtest_config = BacktestConfig(
        market="SH",
        code="600000",
        vipdoc_path=str(tmp_path),
        buy_signal="indicator_three.prepare_rally",
        sell_signal="indicator_three.end_zone",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 4, 29),
        initial_cash=100_000.0,
    )
    first = BacktestService(adapter=adapter, registry=registry, indicator_cache=cache).run(
        backtest_config
    )
    second = BacktestService(adapter=adapter, registry=registry, indicator_cache=cache).run(
        backtest_config
    )

    assert calls["indicator_three"] == 1
    assert first.to_dict() == second.to_dict()

    portfolio_config = PortfolioBacktestConfig(
        vipdoc_path=str(tmp_path),
        universe="all",
        selected_signals=("indicator_three.accumulation_zone",),
        combine_mode="any",
        minimum_matches=None,
        ranking_value="indicator_three.varo7",
        max_positions=1,
        sell_signal="indicator_three.end_zone",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 4, 29),
        initial_cash=100_000.0,
    )
    portfolio = PortfolioBacktestService(adapter=adapter, registry=registry, indicator_cache=cache)
    portfolio_report = portfolio.run(portfolio_config)

    assert calls["indicator_three"] == 1
    assert len(portfolio_report.data_fingerprint) == 64


def test_strategy_fitness_reuses_cached_formula_across_time_windows(
    tmp_path: Path,
    backend: str,
) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb", indicator_cache_backend=backend)
    store.replace_local_bars("SH", "600000", "stock", _bars(120))
    adapter = DuckDbMarketDataAdapter(store)
    calls: dict[str, int] = {}
    registry = _counting_registry(calls)
    cache = IndicatorCache(store, registry=registry)
    strategy = PortfolioBacktestConfig(
        vipdoc_path=str(tmp_path),
        universe="all",
        selected_signals=("indicator_three.accumulation_zone",),
        combine_mode="any",
        minimum_matches=None,
        ranking_value="indicator_three.varo7",
        max_positions=1,
        stop_loss_pct=0.2,
        start_date=date(2024, 1, 1),
        end_date=date(2024, 4, 29),
        initial_cash=100_000.0,
    )

    report = StrategyFitnessService(
        adapter=adapter,
        registry=registry,
        indicator_cache=cache,
    ).run(
        StrategyFitnessConfig(
            strategy=strategy,
            train_ratio=0.5,
            validation_ratio=0.25,
            min_trades=1,
            max_test_drawdown=1.0,
        )
    )

    assert report.processed == 1
    assert calls["indicator_three"] == 1
    assert len(report.results[0].data_fingerprint) == 64


def test_precompute_service_materializes_all_builtin_formulas(tmp_path: Path, backend: str) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb", indicator_cache_backend=backend)
    store.replace_local_bars("SH", "600000", "stock", _bars(120))
    adapter = DuckDbMarketDataAdapter(store)
    cache = IndicatorCache(store)

    report = IndicatorPrecomputeService(adapter=adapter, cache=cache).run(
        vipdoc_path=str(tmp_path),
        universe="all",
    )

    assert report.total_candidates == 1
    assert report.processed == 1
    formula_count = len(FORMULA_REGISTRY.all_formula_ids())
    assert report.computed == formula_count
    assert report.errors == 0

    cold_cache = IndicatorCache(store)
    cold_cache.get_or_compute(
        store.read_bars("SH", "600000"),
        FORMULA_REGISTRY.all_formula_ids(),
        market="SH",
        code="600000",
    )
    assert cold_cache.metrics()["persistent_hits"] == formula_count
    assert cold_cache.metrics()["computed"] == 0


def test_precompute_offsets_are_history_relative(tmp_path: Path, backend: str) -> None:
    """历史比预计算窗口长时，块偏移必须落在全历史坐标系里。

    读取端要求块链的链尾等于 ``instruments.bar_count``（``IndicatorCache._decode_chunks``）。
    从 0 起算的链尾只走到窗口长度，于是预计算的钱全白花、请求路径照样整段重算；更糟的是
    下一轮预计算自己报 ``cache_hits``，把浪费藏得看不见。
    """

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb", indicator_cache_backend=backend)
    # 2023-01-01 ~ 2024-12-31 共 731 根，只预计算 2024 年：窗口前有 365 根历史。
    store.replace_local_bars("SH", "600000", "stock", _bars(731, start="2023-01-01"))
    adapter = DuckDbMarketDataAdapter(store)

    report = IndicatorPrecomputeService(adapter=adapter, cache=IndicatorCache(store)).run(
        vipdoc_path=str(tmp_path),
        universe="all",
        years=1,
        end_year=2024,
    )
    assert report.errors == 0
    assert report.processed == 1

    window = store.read_bars("SH", "600000").tail(100).reset_index(drop=True)
    assert window["date"].iloc[-1] == pd.Timestamp("2024-12-31")
    cold = IndicatorCache(store)
    cold.get_or_compute(window, ("technical",), market="SH", code="600000")
    metrics = cold.metrics()
    assert metrics["persistent_hits"] == 1, metrics
    assert metrics["computed"] == 0, metrics


def test_precompute_cancellation_during_compute_drains_workers_without_persisting(
    tmp_path: Path,
    backend: str,
) -> None:
    from threading import Event

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb", indicator_cache_backend=backend)
    for code in ("600000", "600001", "600002"):
        store.replace_local_bars("SH", code, "stock", _bars(120))
    cancelled = Event()
    definition = FORMULA_REGISTRY.formula("indicator_three")
    original = definition.calculate

    def calculate(frame: pd.DataFrame):
        computed = original(frame)
        cancelled.set()
        return computed

    registry = FormulaRegistry((replace(definition, calculate=calculate),))
    service = IndicatorPrecomputeService(
        adapter=DuckDbMarketDataAdapter(store),
        cache=IndicatorCache(store, registry=registry),
        registry=registry,
    )

    with pytest.raises(PrecomputeCancelledError, match="已取消"):
        service.run(vipdoc_path=str(tmp_path), abort_check=cancelled.is_set)

    assert _cache_row(store, "SELECT COUNT(*) FROM indicator_cache")[0] == 0


def test_cache_persists_one_chunk_per_calendar_year(tmp_path: Path, backend: str) -> None:
    """缓存按自然年分块，块之间首尾相接且覆盖整个序列。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb", indicator_cache_backend=backend)
    frame = _bars(800)  # 2024-01-01 起 800 天，跨 2024 / 2025 / 2026 三个自然年
    store.replace_local_bars("SH", "600000", "stock", frame)
    IndicatorCache(store).get_or_compute(
        store.read_bars("SH", "600000"), ("indicator_three",), market="SH", code="600000"
    )

    rows = _cache_rows(
        store, "SELECT bar_offset, data_length FROM indicator_cache ORDER BY bar_offset"
    )
    assert len(rows) == 3
    cursor = 0
    for offset, length in rows:
        assert offset == cursor
        cursor += int(length)  # type: ignore[call-overload]
    assert cursor == len(frame)


def test_window_request_reuses_cache_without_recomputing(tmp_path: Path, backend: str) -> None:
    """请求最近若干根时命中缓存，且不必把整个序列重算一遍。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb", indicator_cache_backend=backend)
    store.replace_local_bars("SH", "600000", "stock", _bars(800))
    calls: dict[str, int] = {}
    registry = _counting_registry(calls)
    full = store.read_bars("SH", "600000")

    IndicatorCache(store, registry=registry).get_or_compute(
        full, ("indicator_three",), market="SH", code="600000"
    )
    assert calls["indicator_three"] == 1

    short = full.iloc[-60:].reset_index(drop=True)
    result = IndicatorCache(store, registry=registry).get_or_compute(
        short, ("indicator_three",), market="SH", code="600000"
    )["indicator_three"]

    assert calls["indicator_three"] == 1
    assert result.data_length == 60


def test_partial_request_does_not_persist_or_poison_complete_history(
    tmp_path: Path, backend: str
) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb", indicator_cache_backend=backend)
    store.replace_local_bars("SH", "600000", "stock", _bars(800))
    full = store.read_bars("SH", "600000")
    partial = full.iloc[-60:].reset_index(drop=True)
    calls: dict[str, int] = {}
    cache = IndicatorCache(store, registry=_counting_registry(calls))

    cache.get_or_compute(partial, ("indicator_three",), market="SH", code="600000")
    assert _cache_rows(store, "SELECT bar_offset, data_length FROM indicator_cache") == []

    cache.get_or_compute(full, ("indicator_three",), market="SH", code="600000")
    assert calls["indicator_three"] == 2
    rows = _cache_rows(
        store, "SELECT bar_offset, data_length FROM indicator_cache ORDER BY bar_offset"
    )
    assert sum(int(length) for _, length in rows) == len(full)  # type: ignore[call-overload]


def test_partial_poisoned_persistent_entry_is_rejected(tmp_path: Path, backend: str) -> None:
    """Older window writes sharing the full revision are treated as cache misses."""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb", indicator_cache_backend=backend)
    store.replace_local_bars("SH", "600000", "stock", _bars(800))
    full = store.read_bars("SH", "600000")
    partial = full.iloc[-60:].reset_index(drop=True)
    calls: dict[str, int] = {}
    registry = _counting_registry(calls)
    cache = IndicatorCache(store, registry=registry)
    poisoned = cache.get_or_compute(
        partial, ("indicator_three",), market="SH", code="600000", persist=False
    )["indicator_three"]
    cache.persist_results(
        {("SH", "600000"): partial},
        {("SH", "600000"): {"indicator_three": poisoned}},
        data_fingerprint_overrides={("SH", "600000"): cache.revision_for("SH", "600000", full)},
    )

    cache.get_or_compute(full, ("indicator_three",), market="SH", code="600000")
    assert calls["indicator_three"] == 2
    assert sum(
        int(length)
        for _, length in _cache_rows(
            store, "SELECT bar_offset, data_length FROM indicator_cache ORDER BY bar_offset"
        )
    ) == len(full)  # type: ignore[call-overload]


def test_preload_rejects_poisoned_window_and_accepts_complete_year_chunks(
    tmp_path: Path, backend: str
) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb", indicator_cache_backend=backend)
    store.replace_local_bars("SH", "600000", "stock", _bars(800))
    full = store.read_bars("SH", "600000")
    formula_ids = ("indicator_three",)
    calls: dict[str, int] = {}
    registry = _counting_registry(calls)
    seed_cache = IndicatorCache(store, registry=registry)

    # Recreate a legacy window row keyed with the full instrument revision.
    poisoned_window = full.iloc[-60:].reset_index(drop=True)
    poisoned = seed_cache.get_or_compute(
        poisoned_window,
        formula_ids,
        market="SH",
        code="600000",
        persist=False,
    )
    seed_cache.persist_results(
        {("SH", "600000"): poisoned_window},
        {("SH", "600000"): poisoned},
        data_fingerprint_overrides={
            ("SH", "600000"): seed_cache.revision_for("SH", "600000", full)
        },
    )
    reader = IndicatorCache(store, registry=registry)
    assert reader.preload({("SH", "600000"): full}, formula_ids) == 0
    reader.get_or_compute(
        full.iloc[-10:].reset_index(drop=True),
        formula_ids,
        market="SH",
        code="600000",
    )
    assert calls["indicator_three"] == 2

    # A correctly complete annual chunk set still preloads and serves tail windows.
    store2 = DuckDbMarketDataStore(tmp_path / "complete.duckdb", indicator_cache_backend=backend)
    store2.replace_local_bars("SH", "600000", "stock", full)
    complete_writer = IndicatorCache(store2, registry=registry)
    complete_writer.get_or_compute(full, formula_ids, market="SH", code="600000")
    complete_reader = IndicatorCache(store2, registry=registry)
    assert complete_reader.preload({("SH", "600000"): full}, formula_ids) == 1
    complete_reader.get_or_compute(
        full.iloc[-10:].reset_index(drop=True),
        formula_ids,
        market="SH",
        code="600000",
    )
    assert complete_reader.metrics()["computed"] == 0


def test_values_blob_is_persisted_as_float32(tmp_path: Path, backend: str) -> None:
    """落库用 float32：体积约为 float64 的一半，读回来仍是 float64。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb", indicator_cache_backend=backend)
    store.replace_local_bars("SH", "600000", "stock", _bars(400))
    cache = IndicatorCache(store)
    frame = store.read_bars("SH", "600000")
    computed = cache.get_or_compute(frame, ("technical",), market="SH", code="600000")["technical"]

    columns = len(FORMULA_REGISTRY.formula("technical").value_names)
    bars, blob_bytes = _cache_row(
        store, "SELECT SUM(data_length), SUM(octet_length(values_blob)) FROM indicator_cache"
    )
    expected_float32 = bars * columns * 4
    # 每块有 .npy 头，留一点余量；关键是明显小于 float64 的两倍体积。
    assert expected_float32 <= blob_bytes <= expected_float32 + 1024
    assert blob_bytes < expected_float32 * 1.5
    assert np.asarray(next(iter(computed.values.values()))).dtype == np.float64


def test_precompute_only_changed_skips_fresh_instruments(tmp_path: Path, backend: str) -> None:
    """只补算缓存缺失或过期的标的：第二轮不应重复处理已经算过的。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb", indicator_cache_backend=backend)
    for code in ("600000", "600001"):
        store.replace_local_bars("SH", code, "stock", _bars(400))
    adapter = DuckDbMarketDataAdapter(store)
    cache = IndicatorCache(store)
    service = IndicatorPrecomputeService(adapter=adapter, cache=cache)

    first = service.run(vipdoc_path=str(tmp_path), universe="all", only_changed=True)
    assert first.total_candidates == 2
    assert first.processed == 2

    # 第二轮的候选集合是空的：两个标的的缓存都还在有效期内。
    second = service.run(vipdoc_path=str(tmp_path), universe="all", only_changed=True)
    assert second.total_candidates == 0
    assert second.processed == 0

    # 行情一变，版本号随之变化，该标的重新进入待算集合。
    changed = _bars(410)
    store.replace_local_bars("SH", "600000", "stock", changed)
    third = service.run(vipdoc_path=str(tmp_path), universe="all", only_changed=True)
    assert third.processed == 1


def test_precompute_uses_eight_workers_for_independent_instruments(
    tmp_path: Path,
    backend: str,
) -> None:
    refs = [
        StockRef(
            market="SH",
            code=f"600{index:03d}",
            path=tmp_path,
        )
        for index in range(16)
    ]
    lock = Lock()
    active = 0
    max_active = 0

    class TrackingAdapter:
        def list_stock_refs(self, *_args, **_kwargs):
            return refs

        def read_stock(self, _ref):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.02)
                return _bars(120)
            finally:
                with lock:
                    active -= 1

    report = IndicatorPrecomputeService(
        adapter=TrackingAdapter(),
        cache=IndicatorCache(),
    ).run(vipdoc_path=str(tmp_path))

    assert report.total_candidates == len(refs)
    assert report.processed == len(refs)
    assert report.errors == 0
    assert 1 < max_active <= 8


def test_precompute_stitches_yearly_segments_into_full_history(
    tmp_path: Path,
    backend: str,
) -> None:
    """Year-by-year segments must rejoin into one series equal to a full run."""

    # 需要真实波动：若 high/low 恒为 close ± 常数，RSRS 回归的 beta 近似恒定，
    # 滚动标准差趋近 0，z-score 会把浮点噪声放大成 O(1) 的假差异。
    rng = np.random.default_rng(7)
    length = 900
    # 起点要够高：随机游走一旦被下限截断成常数，RSI 之类的指标会退化成常数，
    # 分段与全量的"冷启动 vs 已收敛"差异就会被放大成假失败。
    close = 25.0 + np.cumsum(rng.normal(0.0, 0.08, length))
    frame = pd.DataFrame(
        {
            "date": pd.bdate_range("2023-01-02", periods=length),
            "open": np.clip(close + rng.normal(0, 0.04, length), close * 0.95, close * 1.05),
            "high": close * (1 + np.abs(rng.normal(0, 0.012, length))),
            "low": close * (1 - np.abs(rng.normal(0, 0.012, length))),
            "close": close,
            "volume": rng.integers(1000, 99999, length).astype(float),
            "amount": close * rng.integers(1000, 99999, length),
            "bar_status": "completed",
        }
    )
    frame["market"] = "SH"
    frame["code"] = "600000"

    class WindowAdapter:
        def list_stock_refs(self, *_args, **_kwargs):
            return [StockRef(market="SH", code="600000", path=tmp_path)]

        def read_many_stocks(self, _refs, *, start_date=None, end_date=None, warmup_bars=0):
            sub = frame
            if start_date is not None:
                sub = sub[sub["date"] >= pd.Timestamp(start_date)]
            if end_date is not None:
                sub = sub[sub["date"] <= pd.Timestamp(end_date)]
            if warmup_bars and not sub.empty:
                position = frame.index.get_loc(sub.index[0])
                head = frame.iloc[max(0, position - warmup_bars) : position]
                # 窗口从第一根 K 线开始时 head 是空的。pandas 拼接含空块时会先给
                # 空块推断一个 timedelta64 泛型 dtype 再取 NaT，numpy 2.5 起对此
                # 发弃用警告、并预告未来会直接抛错；空块本来就不贡献行，跳过它
                # 结果与原先一致（dtype 也不会被空块带偏）。
                if not head.empty:
                    sub = pd.concat([head, sub])
            return sub.reset_index(drop=True)

        def read_stock(self, _ref):
            return frame

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb", indicator_cache_backend=backend)
    store.replace_local_bars("SH", "600000", "stock", frame)
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        report = IndicatorPrecomputeService(WindowAdapter(), IndicatorCache(store)).run(
            vipdoc_path=str(tmp_path), years=4, warmup_bars=268
        )
    assert report.processed == 1
    assert report.errors == 0

    cold = IndicatorCache(store)
    cached = cold.get_or_compute(
        frame.copy(),
        FORMULA_REGISTRY.all_formula_ids(),
        market="SH",
        code="600000",
    )
    assert cold.metrics()["computed"] == 0
    assert cold.metrics()["persistent_hits"] == len(FORMULA_REGISTRY.all_formula_ids())

    for formula_id, result in cached.items():
        expected = FORMULA_REGISTRY.formula(formula_id).calculate(frame.copy(deep=True))
        assert result.data_length == expected.data_length
        for name, array in expected.values.items():
            left = np.asarray(array, dtype=float)[400:]
            right = np.asarray(result.values[name], dtype=float)[400:]
            if not np.isfinite(left).any():  # 合成数据下个别输出恒为 NaN
                continue
            scale = max(float(np.nanmax(np.abs(left))), 1.0)
            drift = float(np.nanmax(np.abs(left - right))) / scale
            assert drift < 1e-6, f"{formula_id}.{name} drift={drift}"


def test_custom_formula_cache_is_separated_by_source_and_parameters(
    tmp_path: Path,
    backend: str,
) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb", indicator_cache_backend=backend)
    frame = _bars(120)
    parsed = parse_formula("N:=5; BUY:CROSS(C,REF(C,N)); RANK:MA(C,N);")
    cache = IndicatorCache(store)

    first = cache.get_or_compute_custom(
        frame,
        parsed,
        {},
        market="SH",
        code="600000",
    )
    second = IndicatorCache(store).get_or_compute_custom(
        frame.copy(deep=True),
        parsed,
        {"N": 5},
        market="SH",
        code="600000",
    )
    changed_parameters = cache.get_or_compute_custom(
        frame,
        parsed,
        {"N": 6},
        market="SH",
        code="600000",
    )
    different_source = cache.get_or_compute_custom(
        frame,
        parse_formula("N:=5; BUY:CROSS(C,REF(C,N)); RANK:MA(C,N)+0;"),
        {},
        market="SH",
        code="600000",
    )

    expected = evaluate_custom_formula(parsed, frame, {})
    assert cache.metrics()["computed"] == 3
    assert np.array_equal(first["custom"].signals["custom.buy"], expected.signals["custom.buy"])
    np.testing.assert_allclose(
        second["custom"].values["RANK"], expected.values["RANK"], equal_nan=True
    )
    assert changed_parameters["custom"].data_length == len(frame)
    assert different_source["custom"].data_length == len(frame)


def test_cache_can_preload_persistent_rows_for_multiple_symbols(
    tmp_path: Path,
    backend: str,
) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb", indicator_cache_backend=backend)
    store.replace_local_bars("SH", "600000", "stock", _bars(120))
    store.replace_local_bars("SH", "600001", "stock", _bars(120))
    ids = FORMULA_REGISTRY.all_formula_ids()
    writer = IndicatorCache(store)
    for code in ("600000", "600001"):
        writer.get_or_compute(store.read_bars("SH", code), ids, market="SH", code=code)

    reader = IndicatorCache(store)
    reader.preload(
        {
            ("SH", "600000"): store.read_bars("SH", "600000"),
            ("SH", "600001"): store.read_bars("SH", "600001"),
        },
        ids,
    )

    assert reader.metrics()["persistent_hits"] == len(ids) * 2


def test_backtest_report_records_the_input_data_fingerprint() -> None:
    frame = _bars(120)

    class Adapter:
        def stock_ref(self, vipdoc_path, market, code):
            return StockRef(market, code, Path(vipdoc_path) / "sh600000.day")

        def read_stock(self, ref):
            return frame.copy(deep=True)

    report = BacktestService(adapter=Adapter()).run(
        BacktestConfig(
            market="SH",
            code="600000",
            vipdoc_path="/tmp/vipdoc",
            buy_signal="custom.buy",
            sell_signal="custom.sell",
            formula_text="BUY:C>0; SELL:C<0;",
        )
    )

    assert isinstance(report.configuration["data_fingerprint"], str)
    assert len(report.configuration["data_fingerprint"]) == 64


def test_year_windows_anchor_on_the_stored_data_not_the_wall_clock() -> None:
    """「最近 N 年」说的是数据的最近 N 年，不是系统时间的。

    行情停在 2024 年而今年是 2026 年时，按墙上时钟算会给出 2022..2026：两个
    空年份白算，真正存着数据的 2020、2021 反而被切掉。
    """

    assert _year_windows(3, 2024) == [
        (date(2022, 1, 1), date(2022, 12, 31)),
        (date(2023, 1, 1), date(2023, 12, 31)),
        (date(2024, 1, 1), date(2024, 12, 31)),
    ]
    with pytest.raises(ValueError, match="年份锚点"):
        _year_windows(3, 0)


def test_replacing_bars_on_the_same_dates_needs_an_explicit_cache_reset(
    tmp_path: Path,
    backend: str,
) -> None:
    """日期不变、数值变了：缓存不会自己察觉，必须靠调用方失效。

    缓存键里带的是标的级版本号（`bar_count|last_date|updated_at`），它在进程
    内是缓存住的。行情重写之后它必然变化，但手上的副本不会——结束日期又没变，
    于是内存命中校验也过得去，回测就这样拿到了上一版行情算出来的指标，且不
    报错。这就是 `clear_memory()` 存在的理由，也是每条写入行情的路径都必须
    调它的原因。
    """

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb", indicator_cache_backend=backend)
    cache = IndicatorCache(store)
    first = _bars(80)
    store.replace_local_bars("SH", "600000", "stock", first)
    cache.get_or_compute(first, ("price",), market="SH", code="600000")

    second = _bars(80)
    second["close"] = second["close"] + 100.0
    store.replace_local_bars("SH", "600000", "stock", second)
    stale = cache.get_or_compute(second, ("price",), market="SH", code="600000")["price"]

    assert np.allclose(stale.values["close"], np.asarray(first["close"], dtype=float))

    cache.clear_memory()
    fresh = cache.get_or_compute(second, ("price",), market="SH", code="600000")["price"]

    assert np.allclose(fresh.values["close"], np.asarray(second["close"], dtype=float))


def test_default_indicator_cache_backend_is_sqlite(tmp_path: Path) -> None:
    """派生缓存默认落在 SQLite：落库快 22.5 倍，批量读只是持平。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    assert store.indicator_cache_backend == "sqlite"
    assert isinstance(store._indicator_cache, SqliteIndicatorCacheStore)
    expected = default_indicator_cache_path(tmp_path / "market.duckdb")
    assert store._indicator_cache.database_path == expected
    # 建表是惰性的：构造 store 不该凭空落一个文件，第一次读缓存才建。
    assert not expected.exists()
    store.indicator_cache_status()
    assert expected.exists()


def test_backend_can_be_pinned_back_to_duckdb(tmp_path: Path) -> None:
    """出问题时要能不改代码退回旧实现。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb", indicator_cache_backend="duckdb")
    assert store.indicator_cache_backend == "duckdb"
    assert isinstance(store._indicator_cache, DuckDbIndicatorCacheStore)


def test_unknown_indicator_cache_backend_is_rejected() -> None:
    """拼错的后端名必须立刻报错，而不是悄悄退回默认值。"""

    with pytest.raises(ValueError, match="sqlite 或 duckdb"):
        resolve_indicator_cache_backend("postgres")


def test_sqlite_rows_carry_real_dates_not_iso_strings(tmp_path: Path) -> None:
    """SQLite 存的是 ISO 文本，读出来必须还原成 date。

    `_row_ends_at` 现在既认 date 也认字符串，所以这里错了也不会立刻报错——
    它属于那种「今天能跑、明天加个调用方就静默失效」的坑。和 DuckDB 保持
    一致的类型，将来任何新代码都拿得到同一形态。
    """

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars(80), price_basis="raw")
    IndicatorCache(store).get_or_compute(
        store.read_bars("SH", "600000"), ("indicator_three",), market="SH", code="600000"
    )

    # 库里确实是文本，类型转换发生在读取层。
    raw = _cache_row(store, "SELECT data_end FROM indicator_cache")
    assert raw is not None and isinstance(raw[0], str)

    rows = store.read_indicator_cache(
        market="SH",
        code="600000",
        timeframe="daily",
        price_basis="raw",
        formula_id="indicator_three",
        formula_version=FORMULA_REGISTRY.formula("indicator_three").cache_version,
        data_fingerprint=store.data_revision("SH", "600000") or "",
    )
    assert rows
    assert isinstance(rows[0]["data_end"], date)
    assert not isinstance(rows[0]["data_end"], str)
    assert isinstance(rows[0]["computed_at"], datetime)


def test_sqlite_backend_reports_status_and_stale_symbols(tmp_path: Path) -> None:
    """运维用到的 status / stale_symbols 在 SQLite 后端上同样成立。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars(80), price_basis="raw")
    IndicatorCache(store).get_or_compute(
        store.read_bars("SH", "600000"), ("indicator_three",), market="SH", code="600000"
    )

    status = store.indicator_cache_status()
    assert status["builtin_entries"] >= 1
    assert status["bytes"] > 0
    assert status["latest_computed_at"]

    assert (
        store.stale_indicator_symbols(
            {"indicator_three": FORMULA_REGISTRY.formula("indicator_three").cache_version}
        )
        == set()
    )
    assert store.stale_indicator_symbols(
        {"technical": FORMULA_REGISTRY.formula("technical").cache_version}
    ) == {("SH", "600000")}

    removed = store.clear_indicator_caches()
    assert removed["builtin_entries"] >= 1
    assert store.indicator_cache_status()["total_entries"] == 0


def test_stale_symbols_compare_formula_version_not_just_fingerprint(
    tmp_path: Path,
    backend: str,
) -> None:
    """改了指标口径（bump `cache_version`）而行情没变，标的必须仍算过期。

    读取路径 `read_indicator_cache` 同时比 `formula_version` 和
    `data_fingerprint`；过期判断只比后者的话，增量预计算会把这类标的一律跳过，
    改完口径的预热白跑。
    """

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb", indicator_cache_backend=backend)
    store.replace_local_bars("SH", "600000", "stock", _bars(80), price_basis="raw")
    IndicatorCache(store).get_or_compute(
        store.read_bars("SH", "600000"), ("indicator_three",), market="SH", code="600000"
    )

    current = FORMULA_REGISTRY.formula("indicator_three").cache_version
    assert store.stale_indicator_symbols({"indicator_three": current}) == set()
    assert store.stale_indicator_symbols({"indicator_three": f"{current}-next"}) == {
        ("SH", "600000")
    }


def test_a_sqlite_outage_degrades_to_computing_instead_of_failing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SQLite 后端的锁冲突/表缺失属于派生缓存故障，不能打断同步或回测。

    ``sqlite3.Error`` 不是 ``RuntimeError`` 的子类，只捕 RuntimeError 会让
    "database is locked" 一路冒到调用方。
    """

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    cache = IndicatorCache(store)
    frame = _bars(80)

    def locked(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store._indicator_cache, "write_indicator_caches", locked)
    monkeypatch.setattr(store._indicator_cache, "read_indicator_caches", locked)

    result = cache.get_or_compute(frame, ("price",), market="SH", code="600000")["price"]

    assert np.allclose(result.values["close"], np.asarray(frame["close"], dtype=float))

    # 自定义公式走另一条读写路径，同样要能降级。
    custom = cache.get_or_compute_custom(
        frame,
        parse_formula("N:=5; BUY:CROSS(C,REF(C,N)); RANK:MA(C,N);"),
        None,
        market="SH",
        code="600000",
    )
    assert "custom" in custom


def test_fingerprint_matches_revision_across_adjust_version_suffix() -> None:
    """带复权事件版本的分块指纹必须仍算命中当前行情版本。

    落库形态是 ``<版本号>~adj:<事件版本>|chunk:<摘要>``，而 ``data_revisions()``
    给的批次版本号不带事件后缀。少了这层剥离，凡是有除权事件的标的都会被判成
    stale，增量预计算静默退化成全量重算。
    """

    revision = "SH|600000|80|2024-01-02|2024-01-02T00:00:00"
    chunked = f"{revision}~adj:abc123|chunk:deadbeef"

    assert _fingerprint_matches_revision(chunked, revision)
    assert _fingerprint_matches_revision(revision, revision)
    assert _fingerprint_matches_revision(f"{revision}|chunk:deadbeef", revision)
    # 版本号本身变了就不该命中。
    assert not _fingerprint_matches_revision(chunked, f"{revision}~other")
    # 没有块标记的指纹（帧指纹降级路径）不该被误判成命中。
    assert not _fingerprint_matches_revision("unrelated-fingerprint", revision)


def test_sqlite_cache_maintenance_prunes_stale_generations_and_vacuums(tmp_path: Path) -> None:
    """过期指纹的缓存行自动清掉、空页多时回收文件；最新一代与别的标的原样不动。

    写入路径只清「这次重算过」的标的；数据变了却没再被重算的标的会留着旧指纹的行，
    而旧指纹永远查不到（读取按当前指纹精确匹配）——所以这里按每个分组的指纹判定过期代。
    同代的各分块各写各的 computed_at，必须整代一起留下。
    """

    from aquest.market_data.store import DuckDbMarketDataStore
    from aquest.market_data.store_indicator_cache_sqlite import SqliteIndicatorCacheStore

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    cache = SqliteIndicatorCacheStore(store, tmp_path / "cache.sqlite")
    connection = cache._connection()
    columns = (
        "market, code, timeframe, price_basis, formula_id, formula_version, "
        "data_fingerprint, bar_offset, data_start, data_end, data_length, "
        "values_blob, signals_blob, computed_at"
    )

    def row(
        code: str,
        fingerprint: str,
        offset: int,
        computed_at: str,
        formula_version: str = "v1",
    ) -> tuple:
        return (
            "SH",
            code,
            "1d",
            "raw",
            "technical.ma5",
            formula_version,
            fingerprint,
            offset,
            "2015-01-05",
            "2026-10-19",
            2065,
            b"values",
            b"signals",
            computed_at,
        )

    rows = [
        # 过期代：两块，时间戳相同
        row("600000", "old", 0, "2026-10-01T00:00:00"),
        row("600000", "old", 1, "2026-10-01T00:00:00"),
        # 当前代：三块，每块各写各的 computed_at——只看时间戳会只剩最后一块
        row("600000", "new", 0, "2026-10-02T00:00:00.100000"),
        row("600000", "new", 1, "2026-10-02T00:00:00.200000"),
        row("600000", "new", 2, "2026-10-02T00:00:00.300000"),
        # 旧公式版本按自己的代判定，不该被新版本连带删掉
        row("600000", "v2only", 0, "2026-09-01T00:00:00", formula_version="v2"),
        row("600001", "only", 0, "2026-10-02T00:00:00"),
    ]
    connection.executemany(
        f"INSERT INTO indicator_cache ({columns}) VALUES ({','.join('?' * 14)})", rows
    )

    stats = cache.maintain(vacuum_min_free_ratio=0.0)

    assert stats["pruned_indicator_cache"] == 2
    assert stats["vacuumed"] is True
    # VACUUM 重建的库页写在 WAL 里，维护结束必须截断，否则文件系统上的占用收不回来。
    assert stats["wal_size_after"] == 0
    remaining = connection.execute(
        "SELECT code, formula_version, data_fingerprint, bar_offset FROM indicator_cache "
        "ORDER BY code, formula_version, data_fingerprint, bar_offset"
    ).fetchall()
    assert remaining == [
        ("600000", "v1", "new", 0),
        ("600000", "v1", "new", 1),
        ("600000", "v1", "new", 2),
        ("600000", "v2", "v2only", 0),
        ("600001", "v1", "only", 0),
    ]

    # 再跑一次没有可清的行，也不该出错
    again = cache.maintain(vacuum_min_free_ratio=0.0)
    assert again["pruned_indicator_cache"] == 0


def test_sqlite_cache_prune_scales_with_rows(tmp_path: Path) -> None:
    """清理的工作量必须随行数线性增长。

    早先的写法 ``rowid IN (窗口子查询)`` 被 SQLite 当成关联子查询，对每一行重跑一次
    全表窗口；5 GB 生产缓存一小时删不完，容器健康检查直接判死。这里用两万行卡住：
    线性扫描是毫秒级，平方级实现会远超 10 秒。
    """

    import time

    from aquest.market_data.store import DuckDbMarketDataStore
    from aquest.market_data.store_indicator_cache_sqlite import SqliteIndicatorCacheStore

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    cache = SqliteIndicatorCacheStore(store, tmp_path / "cache.sqlite")
    connection = cache._connection()
    columns = (
        "market, code, timeframe, price_basis, formula_id, formula_version, "
        "data_fingerprint, bar_offset, data_start, data_end, data_length, "
        "values_blob, signals_blob, computed_at"
    )
    rows = []
    for instrument in range(10_000):
        code = f"{600000 + instrument}"
        for generation in ("old", "new"):
            rows.append(
                (
                    "SH",
                    code,
                    "1d",
                    "raw",
                    "technical.ma5",
                    "v1",
                    generation,
                    0,
                    "2015-01-05",
                    "2026-10-19",
                    2065,
                    b"x" * 64,
                    b"y" * 64,
                    "2026-10-01T00:00:00" if generation == "old" else "2026-10-02T00:00:00",
                )
            )
    connection.executemany(
        f"INSERT INTO indicator_cache ({columns}) VALUES ({','.join('?' * 14)})", rows
    )

    started = time.monotonic()
    stats = cache.prune_stale_generations()
    elapsed = time.monotonic() - started
    assert stats["indicator_cache"] == 10_000
    assert elapsed < 10
