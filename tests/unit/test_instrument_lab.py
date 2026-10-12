"""The workbench's readings and its single-instrument condition test.

这里最重要的不是「算得对」，而是**该闭嘴的时候闭嘴**：命中太少要标样本不足，序
列太短不做置换，一次检验多条要 BH 校正，没命中就不能给差值。这些护栏是这个页
面唯一的可信度来源。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from aquest.backtest.models import BacktestConfig
from aquest.backtest.service import BacktestService, PreparedBacktest
from aquest.formulas.types import FormulaResult
from aquest.instrument_lab.conditions import (
    ConditionCheckInput,
    check_conditions,
    condition_from_mapping,
    condition_key,
    condition_label,
    significant_conditions,
)
from aquest.instrument_lab.models import (
    DEFAULT_ALPHA,
    DEFAULT_HORIZON,
    LOW_SAMPLE_NOTE,
    MAX_VALUE_IDS,
    MIN_HIT_SAMPLES,
    NO_HIT_NOTE,
    SHORT_SERIES_NOTE,
    thin_indices,
)
from aquest.instrument_lab.service import InstrumentLabService
from aquest.instrument_lab.values import build_instrument_values
from aquest.market_data.models import StockRef
from aquest.screening.models import ScreenCondition

_SAW = np.array([0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 4.0, 3.0, 2.0, 1.0])


def sawtooth_frame(length: int = 120) -> pd.DataFrame:
    """Prices that rise for five days and fall for five, with a mild drift.

    十天一个来回，于是「前半程命中」这个条件正好对着正的前瞻收益——用它验证置
    换检验能认出对齐，也能认出没对齐。
    """

    positions = np.arange(length)
    close = 100.0 + 0.05 * positions + _SAW[positions % 10]
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=length, freq="D"),
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.full(length, 100_000.0),
            "amount": close * 100_000.0,
        }
    )


def regime_frame(length: int = 240) -> pd.DataFrame:
    """A stock that rises for its first 40 days and then drifts down.

    命中前 40 天这个条件对着一个**连续的收益区间**，而不是周期性的锯齿：块置换
    搬动的是整段日期，正好能把这种「押中了某段行情」的假象拆穿。
    """

    positions = np.arange(length)
    close = np.where(
        positions < 40,
        100.0 + 0.3 * positions,
        112.0 - 0.05 * (positions - 40),
    )
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=length, freq="D"),
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.full(length, 100_000.0),
            "amount": close * 100_000.0,
        }
    )


class FakeAdapter:
    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame

    def stock_ref(self, vipdoc_path: str | Path, market: str, code: str) -> StockRef:
        return StockRef(
            market=market,  # type: ignore[arg-type]
            code=code,
            path=Path(vipdoc_path) / f"{market.lower()}{code}.day",
            name="测试股份",
        )

    def read_stock(self, ref: StockRef) -> pd.DataFrame:
        return self.frame.copy(deep=True)


def _prepared(
    values: dict[str, np.ndarray],
    *,
    length: int = 120,
    frame: pd.DataFrame | None = None,
) -> PreparedBacktest:
    frame = frame if frame is not None else sawtooth_frame(length)
    length = len(frame)
    result = FormulaResult.build(
        formula_id="technical",
        display_name="技术指标",
        minimum_bars=1,
        recommended_bars=length,
        data_length=length,
        values=values,
        signals={},
    )
    return PreparedBacktest(
        config=BacktestConfig(
            market="SH",
            code="600000",
            vipdoc_path="/tmp/vipdoc",
            always_in_market=True,
            close_at_end=True,
        ),
        ref=StockRef(
            market="SH", code="600000", path=Path("/tmp/vipdoc/sh600000.day"), name="测试股份"
        ),
        frame=frame,
        formula_results={"technical": result},
        custom_formula=False,
        data_fingerprint="fingerprint",
        feature_values=pd.DataFrame(
            {"date": frame["date"].to_numpy(), **{f"technical.{k}": v for k, v in values.items()}}
        ),
    )


def _rising_half(length: int = 120) -> np.ndarray:
    positions = np.arange(length)
    return np.where(positions % 10 < 5, 1.0, 0.0)


def _input(condition: ScreenCondition, key: str = "cond") -> ConditionCheckInput:
    return ConditionCheckInput(key=key, label=condition_label(condition), condition=condition)


def test_timeline_reads_values_and_self_percentile() -> None:
    service = InstrumentLabService(
        None,
        backtest_service=BacktestService(adapter=FakeAdapter(sawtooth_frame(120))),
    )

    values = service.values(market="SH", code="600000", value_ids=["rsi14"])

    assert values.bars == 120
    series = values.series[0]
    assert series.value_id == "technical.rsi14"
    assert len(series.dates) == len(series.values) == 120
    assert series.last_value is not None
    assert 0.0 <= (series.percentile or -1.0) <= 100.0
    # 分位口径必须写清楚：是跟自己比，不是跟全市场比。
    assert any("自身历史分位" in note for note in values.notes)


def test_timeline_thins_long_history_but_keeps_both_ends() -> None:
    service = InstrumentLabService(
        None,
        backtest_service=BacktestService(adapter=FakeAdapter(sawtooth_frame(1600))),
    )

    values = service.values(market="SH", code="600000", value_ids=["rsi14"])

    assert values.bars == 1600
    assert values.thinned is True
    assert len(values.series[0].dates) == 1500
    assert values.series[0].dates[0] == values.start_date
    assert values.series[0].dates[-1] == values.end_date
    assert thin_indices(10, 4) == (0, 3, 6, 9)


def test_timeline_rejects_unknown_and_too_many_values() -> None:
    service = InstrumentLabService(
        None,
        backtest_service=BacktestService(adapter=FakeAdapter(sawtooth_frame(80))),
    )
    with pytest.raises(ValueError, match="至少选择一个指标"):
        service.values(market="SH", code="600000", value_ids=[])
    with pytest.raises(ValueError, match="未知指标"):
        service.values(market="SH", code="600000", value_ids=["not_an_indicator"])
    with pytest.raises(ValueError, match=f"一次最多查看 {MAX_VALUE_IDS}"):
        service.values(
            market="SH",
            code="600000",
            value_ids=[f"technical.v{index}" for index in range(MAX_VALUE_IDS + 1)],
        )


def test_check_finds_alignment_and_reports_difference() -> None:
    length = 240
    hits = np.where(np.arange(length) < 40, 1.0, 0.0)
    prepared = _prepared({"rsi14": hits}, frame=regime_frame(length))
    condition = ScreenCondition(left_value="technical.rsi14", operator="gte", right_constant=0.5)

    report = check_conditions(
        prepared, [_input(condition)], horizon=DEFAULT_HORIZON, permutations=200
    )

    item = report.items[0]
    assert item.hit_count >= MIN_HIT_SAMPLES
    assert item.difference is not None and item.difference > 0
    assert item.hit_mean_return is not None and item.base_mean_return is not None
    assert item.p_value is not None and item.p_value <= 0.05
    assert item.significant is True
    assert item.hit_dates


def test_noise_does_not_come_out_significant_twice_as_often_as_alpha() -> None:
    """纯噪声下的误报率必须接近 alpha，而不是它的两倍。

    与因子归因那条同源：置换计数若"先看观测值符号、再只数同向更极端的置换"，
    p 就恒等于双侧的一半。这里没有可事先声明的方向（一个条件既可能对应跑赢也
    可能对应跑输），所以只能双侧计数。
    """

    alpha = DEFAULT_ALPHA
    length = 400
    rng = np.random.default_rng(20260923)
    condition = ScreenCondition(left_value="technical.rsi14", operator="gte", right_constant=0.5)

    flagged = 0
    trials = 0
    for trial in range(200):
        # 几何随机游走：收益之间没有可预测结构。
        steps = rng.normal(0.0, 0.015, size=length)
        close = 100.0 * np.exp(np.cumsum(steps))
        frame = pd.DataFrame(
            {
                "date": pd.date_range("2022-01-01", periods=length, freq="D"),
                "open": close,
                "high": close * 1.005,
                "low": close * 0.995,
                "close": close,
                "volume": np.full(length, 100_000.0),
                "amount": close * 100_000.0,
            }
        )
        # 命中标记与价格独立，但成块出现——贴近真实条件的行为。
        flag = np.zeros(length)
        for start in rng.integers(0, length - 5, size=20):
            flag[start : start + 5] = 1.0
        if flag.sum() < 20:
            continue

        report = check_conditions(
            _prepared({"rsi14": flag}, frame=frame),
            [_input(condition)],
            horizon=DEFAULT_HORIZON,
            permutations=120,
            seed=trial,
        )
        item = report.items[0]
        if item.p_value is None or item.note is not None:
            continue
        trials += 1
        if item.p_value <= alpha:
            flagged += 1

    assert trials >= 150, trials
    rate = flagged / trials
    assert rate < 0.09, f"误报率 {rate:.3f} 偏离 alpha={alpha} 太远，检查置换是否只数了一侧"


def test_check_reports_no_hit_without_a_difference() -> None:
    prepared = _prepared({"rsi14": _rising_half()})
    condition = ScreenCondition(left_value="technical.rsi14", operator="gte", right_constant=99.0)

    report = check_conditions(prepared, [_input(condition)], horizon=DEFAULT_HORIZON)

    item = report.items[0]
    assert item.hit_count == 0
    assert item.difference is None
    assert item.note == NO_HIT_NOTE
    assert item.significant is None


def test_check_marks_low_sample_even_when_p_is_small() -> None:
    sparse = np.zeros(120)
    sparse[[0, 1, 2]] = 1.0
    prepared = _prepared({"rsi14": sparse})
    condition = ScreenCondition(left_value="technical.rsi14", operator="gte", right_constant=0.5)

    report = check_conditions(prepared, [_input(condition)], horizon=DEFAULT_HORIZON)

    item = report.items[0]
    assert item.hit_count == 3
    assert item.note == LOW_SAMPLE_NOTE
    # 样本不足就不许判显著：再小的 p 值也不作数。
    assert item.significant is None
    assert item.p_value is not None


def test_check_skips_permutation_on_short_series() -> None:
    # 每天都命中，于是「样本不足」不会抢在前面：要验证的是序列太短时不做置换。
    length = 50
    prepared = _prepared({"rsi14": np.ones(length)}, length=length)
    condition = ScreenCondition(left_value="technical.rsi14", operator="gte", right_constant=0.5)

    report = check_conditions(prepared, [_input(condition)], horizon=DEFAULT_HORIZON)

    assert report.permutations == 0
    item = report.items[0]
    assert item.p_value is None
    assert item.note == SHORT_SERIES_NOTE
    assert any(SHORT_SERIES_NOTE in note for note in report.notes)


def test_check_adjusts_p_values_when_several_conditions_run() -> None:
    prepared = _prepared(
        {
            "rsi14": _rising_half(),
            "ma20": _rising_half(),
            "bias20": np.zeros(120),
        }
    )
    checks = [
        _input(
            ScreenCondition(left_value="technical.rsi14", operator="gte", right_constant=0.5),
            key="a",
        ),
        _input(
            ScreenCondition(left_value="technical.ma20", operator="gte", right_constant=0.5),
            key="b",
        ),
    ]

    report = check_conditions(prepared, checks, horizon=DEFAULT_HORIZON, permutations=100)

    assert len(report.items) == 2
    for item in report.items:
        assert item.adjusted_p_value is not None
        assert item.adjusted_p_value >= (item.p_value or 0.0)
    assert any("BH" in note for note in report.notes)


def test_check_rejects_empty_window() -> None:
    prepared = _prepared({"rsi14": _rising_half()})
    condition = ScreenCondition(left_value="technical.rsi14", operator="gte", right_constant=0.5)
    with pytest.raises(ValueError, match="没有可用日线数据"):
        check_conditions(
            prepared,
            [_input(condition)],
            horizon=DEFAULT_HORIZON,
            start_date=date(2030, 1, 1),
        )


def test_build_instrument_values_uses_requested_window() -> None:
    prepared = _prepared({"rsi14": _rising_half()})
    values = build_instrument_values(
        prepared,
        ("technical.rsi14",),
        start_date=date(2024, 1, 11),
        end_date=date(2024, 2, 9),
    )
    assert values.bars == 30
    assert values.start_date == "2024-01-11"
    assert values.end_date == "2024-02-09"


def test_condition_label_and_key() -> None:
    condition = ScreenCondition(left_value="technical.rsi14", operator="lte", right_constant=30.0)
    assert "≤" in condition_label(condition)
    assert "30" in condition_label(condition)
    assert condition_key(condition) == "technical.rsi14|lte||30"


def test_condition_from_mapping_rejects_malformed() -> None:
    with pytest.raises(ValueError, match="缺少左值"):
        condition_from_mapping({"operator": "gt", "right_constant": 1.0})
    with pytest.raises(ValueError, match="必须且只能设置一个右值"):
        condition_from_mapping(
            {
                "left_value": "technical.rsi14",
                "operator": "gt",
                "right_value": "technical.ma20",
                "right_constant": 1.0,
            }
        )
    with pytest.raises(ValueError, match="必须是数字"):
        condition_from_mapping(
            {"left_value": "technical.rsi14", "operator": "gt", "right_constant": "x"}
        )


class FakeStore:
    def __init__(self, records: list[dict[str, object]]) -> None:
        self._records = records

    def list_backtest_history(self, backtest_type: str) -> list[dict[str, object]]:
        assert backtest_type == "condition_research"
        return [{"history_id": record["history_id"]} for record in self._records]

    def read_backtest_histories(self, history_ids: object) -> list[dict[str, object]]:
        return list(self._records)


def _candidate(
    candidate_id: str,
    *,
    significant: bool,
    lift: float,
    left: str = "technical.rsi14",
    constant: float = 30.0,
) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "side": "buy",
        "condition": {
            "left_value": left,
            "operator": "lte",
            "right_value": None,
            "right_constant": constant,
        },
        "mining": {
            "lift": lift,
            "permutation_pvalue": 0.01,
            "significant": significant,
            "support_count": 120,
            "instrument_count": 40,
        },
    }


def test_library_keeps_only_significant_conditions_and_dedupes() -> None:
    store = FakeStore(
        [
            {
                "history_id": "h1",
                "finished_at": "2026-09-01T10:00:00",
                "summary": {"scanned_stocks": 500},
                "result": {
                    "candidates": [
                        _candidate("a", significant=True, lift=1.4),
                        _candidate("b", significant=False, lift=3.0),
                        _candidate("c", significant=True, lift=1.9, constant=30.0),
                        _candidate("d", significant=True, lift=1.1, left="technical.wr14"),
                    ]
                },
            }
        ]
    )

    items = significant_conditions(store, limit=10)

    # b 没通过检验被丢掉；a 与 c 是同一条条件，留 lift 高的那条。
    assert [item.key for item in items] == [
        "technical.rsi14|lte||30",
        "technical.wr14|lte||30",
    ]
    assert items[0].lift == 1.9
    assert items[0].scanned_stocks == 500
    assert items[0].history_id == "h1"


def test_library_without_store_returns_nothing() -> None:
    assert InstrumentLabService(None).condition_library() == []


def test_service_validates_check_parameters() -> None:
    service = InstrumentLabService(
        None,
        backtest_service=BacktestService(adapter=FakeAdapter(sawtooth_frame(120))),
    )
    condition = {
        "left_value": "technical.rsi14",
        "operator": "gte",
        "right_constant": 0.5,
    }
    with pytest.raises(ValueError, match="至少选择一条条件"):
        service.check_conditions(market="SH", code="600000", conditions=[])
    with pytest.raises(ValueError, match="前瞻天数"):
        service.check_conditions(market="SH", code="600000", conditions=[condition], horizon=0)
    with pytest.raises(ValueError, match="置换次数"):
        service.check_conditions(market="SH", code="600000", conditions=[condition], permutations=1)
    with pytest.raises(ValueError, match="显著性门槛"):
        service.check_conditions(market="SH", code="600000", conditions=[condition], alpha=0.9)


def test_service_runs_condition_check_end_to_end_with_real_indicators() -> None:
    service = InstrumentLabService(
        None,
        backtest_service=BacktestService(adapter=FakeAdapter(sawtooth_frame(120))),
    )

    report = service.check_conditions(
        market="SH",
        code="600000",
        conditions=[{"left_value": "technical.rsi14", "operator": "gte", "right_constant": 50.0}],
        horizon=5,
        permutations=50,
    )

    assert report.market == "SH"
    assert report.code == "600000"
    assert report.sample_days > 0
    assert len(report.items) == 1


def test_library_falls_back_to_processed_for_scan_size() -> None:
    store = FakeStore(
        [
            {
                "history_id": "h2",
                "finished_at": "2026-09-02T10:00:00",
                "summary": {"processed": 5045},
                "result": {"candidates": [_candidate("a", significant=True, lift=1.2)]},
            }
        ]
    )

    items = significant_conditions(store, limit=10)

    assert items[0].scanned_stocks == 5045
