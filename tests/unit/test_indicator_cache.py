from __future__ import annotations

import time
from dataclasses import replace
from datetime import date
from pathlib import Path
from threading import Lock

import numpy as np
import pandas as pd
import pytest

from selector_app.adapters.local_day_adapter import StockRef
from selector_app.backtest.models import BacktestConfig
from selector_app.backtest.service import BacktestService
from selector_app.formulas.custom import evaluate_custom_formula, parse_formula
from selector_app.formulas.registry import FORMULA_REGISTRY, FormulaRegistry
from selector_app.indicators.cache import IndicatorCache, frame_fingerprint
from selector_app.indicators.precompute import IndicatorPrecomputeService, _year_windows
from selector_app.market_data.adapter import DuckDbMarketDataAdapter
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.portfolio_backtest.models import PortfolioBacktestConfig
from selector_app.portfolio_backtest.service import PortfolioBacktestService
from selector_app.screening.engine import ScreenEngine
from selector_app.screening.models import ScanConfig
from selector_app.strategy_fitness.models import StrategyFitnessConfig
from selector_app.strategy_fitness.service import StrategyFitnessService


def _bars(length: int = 80) -> pd.DataFrame:
    close = 10.0 + np.linspace(0.0, 8.0, length) + np.sin(np.arange(length) / 3.0)
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=length, freq="D"),
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


def test_persistent_cache_reuses_formula_result_without_recalculating(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
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


def test_cache_fingerprint_invalidates_after_market_data_changes(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
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


def test_formula_version_and_corrupt_payload_are_cache_misses(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    frame = _bars()
    calls: dict[str, int] = {}
    version_one = _counting_registry(calls)
    cache = IndicatorCache(store, registry=version_one)
    cache.get_or_compute(frame, ("indicator_three",), market="SH", code="600000")

    definition = FORMULA_REGISTRY.formula("indicator_three")

    def versioned_calculate(data: pd.DataFrame):
        calls["indicator_three"] = calls.get("indicator_three", 0) + 1
        return definition.calculate(data)

    version_two = FormulaRegistry(
        (replace(definition, cache_version="2", calculate=versioned_calculate),)
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
        formula_version="2",
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


def test_screen_engine_uses_shared_indicator_cache(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
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


def test_backtest_and_portfolio_reuse_the_same_persistent_result(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
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


def test_strategy_fitness_reuses_cached_formula_across_time_windows(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
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


def test_precompute_service_materializes_all_builtin_formulas(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars(120))
    adapter = DuckDbMarketDataAdapter(store)
    cache = IndicatorCache(store)

    report = IndicatorPrecomputeService(adapter=adapter, cache=cache).run(
        vipdoc_path=str(tmp_path),
        universe="all",
    )

    assert report.total_candidates == 1
    assert report.processed == 1
    assert report.computed == 5
    assert report.errors == 0

    cold_cache = IndicatorCache(store)
    cold_cache.get_or_compute(
        store.read_bars("SH", "600000"),
        FORMULA_REGISTRY.all_formula_ids(),
        market="SH",
        code="600000",
    )
    assert cold_cache.metrics()["persistent_hits"] == 5
    assert cold_cache.metrics()["computed"] == 0


def test_cache_persists_one_chunk_per_calendar_year(tmp_path: Path) -> None:
    """缓存按自然年分块，块之间首尾相接且覆盖整个序列。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    frame = _bars(800)  # 2024-01-01 起 800 天，跨 2024 / 2025 / 2026 三个自然年
    store.replace_local_bars("SH", "600000", "stock", frame)
    IndicatorCache(store).get_or_compute(
        store.read_bars("SH", "600000"), ("indicator_three",), market="SH", code="600000"
    )

    with store._connect(read_only=True) as connection:
        rows = connection.execute(
            "SELECT bar_offset, data_length FROM indicator_cache ORDER BY bar_offset"
        ).fetchall()
    assert len(rows) == 3
    cursor = 0
    for offset, length in rows:
        assert offset == cursor
        cursor += length
    assert cursor == len(frame)


def test_window_request_reuses_cache_without_recomputing(tmp_path: Path) -> None:
    """请求最近若干根时命中缓存，且不必把整个序列重算一遍。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
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


def test_values_blob_is_persisted_as_float32(tmp_path: Path) -> None:
    """落库用 float32：体积约为 float64 的一半，读回来仍是 float64。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.replace_local_bars("SH", "600000", "stock", _bars(400))
    cache = IndicatorCache(store)
    frame = store.read_bars("SH", "600000")
    computed = cache.get_or_compute(frame, ("technical",), market="SH", code="600000")[
        "technical"
    ]

    columns = len(FORMULA_REGISTRY.formula("technical").value_names)
    with store._connect(read_only=True) as connection:
        bars, blob_bytes = connection.execute(
            "SELECT SUM(data_length), SUM(octet_length(values_blob)) FROM indicator_cache"
        ).fetchone()
    expected_float32 = bars * columns * 4
    # 每块有 .npy 头，留一点余量；关键是明显小于 float64 的两倍体积。
    assert expected_float32 <= blob_bytes <= expected_float32 + 1024
    assert blob_bytes < expected_float32 * 1.5
    assert np.asarray(next(iter(computed.values.values()))).dtype == np.float64


def test_precompute_only_changed_skips_fresh_instruments(tmp_path: Path) -> None:
    """只补算缓存缺失或过期的标的：第二轮不应重复处理已经算过的。"""

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
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


def test_precompute_uses_eight_workers_for_independent_instruments(tmp_path: Path) -> None:
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


def test_precompute_stitches_yearly_segments_into_full_history(tmp_path: Path) -> None:
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
                sub = pd.concat([head, sub])
            return sub.reset_index(drop=True)

        def read_stock(self, _ref):
            return frame

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
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


def test_custom_formula_cache_is_separated_by_source_and_parameters(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
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


def test_cache_can_preload_persistent_rows_for_multiple_symbols(tmp_path: Path) -> None:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
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

    assert reader.metrics()["persistent_hits"] == 10


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
) -> None:
    """日期不变、数值变了：缓存不会自己察觉，必须靠调用方失效。

    缓存键里带的是标的级版本号（`bar_count|last_date|updated_at`），它在进程
    内是缓存住的。行情重写之后它必然变化，但手上的副本不会——结束日期又没变，
    于是内存命中校验也过得去，回测就这样拿到了上一版行情算出来的指标，且不
    报错。这就是 `clear_memory()` 存在的理由，也是每条写入行情的路径都必须
    调它的原因。
    """

    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
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
