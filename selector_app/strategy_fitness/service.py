"""Batch chronological suitability evaluation for one-stock formula strategies."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Protocol

import numpy as np
import pandas as pd

from selector_app.concurrency import MAX_WORKERS
from selector_app.formulas.common import validate_market_data
from selector_app.formulas.custom import ParsedFormula, parse_formula
from selector_app.formulas.registry import FORMULA_REGISTRY, FormulaRegistry
from selector_app.indicators.cache import IndicatorCache, frame_fingerprint
from selector_app.market_data.adapter import DuckDbMarketDataAdapter
from selector_app.market_data.day_format import classify_board, classify_instrument
from selector_app.market_data.models import InstrumentBoard, InstrumentType, StockRef
from selector_app.market_data.scope import InstrumentScope
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.portfolio_backtest.models import PortfolioBacktestReport
from selector_app.portfolio_backtest.service import PortfolioBacktestService

from .models import (
    FitnessLabel,
    FitnessPhaseMetrics,
    StrategyFitnessConfig,
    StrategyFitnessReport,
    StrategyFitnessResult,
)

StrategyFitnessProgressCallback = Callable[[int, int], None]


class FitnessMarketDataAdapter(Protocol):
    def list_stock_refs(
        self,
        vipdoc_path: str | Path,
        universe: str,
        universe_file: str | Path | None = None,
        instrument_types: tuple[InstrumentType, ...] | None = None,
        boards: tuple[InstrumentBoard, ...] | None = None,
    ) -> list[StockRef]: ...

    def read_stock(self, ref: StockRef) -> pd.DataFrame: ...


@dataclass(frozen=True)
class _PhaseWindow:
    name: str
    start_date: date
    end_date: date


class _CachedAdapter:
    """Expose already-read frames to the shared single-stock simulator."""

    def __init__(self, refs: list[StockRef], frames: Mapping[str, pd.DataFrame]) -> None:
        self._refs = tuple(refs)
        self._frames = frames

    def list_stock_refs(
        self,
        vipdoc_path: str | Path,
        universe: str,
        universe_file: str | Path | None = None,
        instrument_types: tuple[InstrumentType, ...] | None = None,
        boards: tuple[InstrumentBoard, ...] | None = None,
    ) -> list[StockRef]:
        return list(self._refs)

    def read_stock(self, ref: StockRef) -> pd.DataFrame:
        return self._frames[ref_key(ref)].copy(deep=True)


class StrategyFitnessService:
    """Evaluate every stock against the same causal strategy in three time windows."""

    def __init__(
        self,
        adapter: FitnessMarketDataAdapter | None = None,
        registry: FormulaRegistry | None = None,
        indicator_cache: IndicatorCache | None = None,
    ) -> None:
        self._adapter = adapter or DuckDbMarketDataAdapter(DuckDbMarketDataStore())
        self._registry = registry or FORMULA_REGISTRY
        self._indicator_cache = indicator_cache

    def run(
        self,
        config: StrategyFitnessConfig,
        progress_callback: StrategyFitnessProgressCallback | None = None,
    ) -> StrategyFitnessReport:
        refs = self._list_refs(config)
        parsed = (
            parse_formula(config.strategy.formula_text) if config.strategy.formula_text else None
        )
        frames, failures = self._read_frames(refs)
        if not frames:
            if progress_callback is not None:
                progress_callback(len(refs), len(refs))
            return self._empty_report(config, len(refs), failures, "没有可用于适配性评估的股票数据")

        evaluation_dates = self._evaluation_dates(
            frames, config.strategy.start_date, config.strategy.end_date
        )
        windows = _split_windows(evaluation_dates, config.train_ratio, config.validation_ratio)
        cached_refs = [ref for ref in refs if ref_key(ref) in frames]
        simulator = PortfolioBacktestService(
            adapter=_CachedAdapter(cached_refs, frames),
            registry=self._registry,
            indicator_cache=self._indicator_cache,
        )
        results: list[StrategyFitnessResult] = []
        skipped = len(refs) - len(cached_refs)
        worker_count = min(MAX_WORKERS, len(cached_refs)) if cached_refs else 1
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="fitness-stock",
        ) as executor:
            futures = {
                executor.submit(
                    self._evaluate_stock,
                    ref,
                    frames[ref_key(ref)],
                    windows,
                    config,
                    parsed,
                    simulator,
                ): ref
                for ref in cached_refs
            }
            completed = 0
            for future in as_completed(futures):
                results.append(future.result())
                completed += 1
                if progress_callback is not None:
                    progress_callback(skipped + completed, len(refs))

        results.sort(key=lambda item: (-item.suitability_score, item.market, item.code))
        return StrategyFitnessReport(
            universe=config.strategy.universe,
            total_candidates=len(refs),
            processed=len(cached_refs),
            skipped=len(refs) - len(cached_refs),
            errors=sum(failures.values()),
            bars=len(evaluation_dates),
            start_date=_date_text(evaluation_dates[0]),
            end_date=_date_text(evaluation_dates[-1]),
            train_end_date=_date_text(windows[0].end_date),
            validation_end_date=_date_text(windows[1].end_date),
            ranking_value=config.strategy.ranking_value,
            train_ratio=config.train_ratio,
            validation_ratio=config.validation_ratio,
            min_trades=config.min_trades,
            max_test_drawdown=config.max_test_drawdown,
            results=tuple(results),
            failure_reasons=failures,
            diagnostic=None,
        )

    def _read_frames(
        self,
        refs: list[StockRef],
    ) -> tuple[dict[str, pd.DataFrame], Counter[str]]:
        frames: dict[str, pd.DataFrame] = {}
        failures: Counter[str] = Counter()
        batch_frames = self._batch_frames(refs)
        for ref in refs:
            try:
                source = (
                    batch_frames.get((ref.market, ref.code))
                    if batch_frames is not None
                    else self._adapter.read_stock(ref)
                )
                frame = self._prepare_frame(source if source is not None else pd.DataFrame())
                if frame.empty:
                    raise ValueError("数据为空")
                frames[ref_key(ref)] = frame
            except Exception as exc:  # noqa: BLE001 - one bad stock must not abort the batch
                reason = str(exc).strip()[:200] or type(exc).__name__
                failures[reason] += 1
        return frames, failures

    def _list_refs(self, config: StrategyFitnessConfig) -> list[StockRef]:
        strategy = config.strategy
        if not strategy.instrument_types and not strategy.boards:
            return self._adapter.list_stock_refs(
                strategy.vipdoc_path,
                strategy.universe,
                strategy.universe_file,
            )
        try:
            refs = self._adapter.list_stock_refs(
                strategy.vipdoc_path,
                strategy.universe,
                strategy.universe_file,
                instrument_types=strategy.instrument_types,
                boards=strategy.boards,
            )
        except TypeError as exc:
            if "unexpected keyword" not in str(exc):
                raise
            refs = self._adapter.list_stock_refs(
                strategy.vipdoc_path,
                strategy.universe,
                strategy.universe_file,
            )
        scope = InstrumentScope.from_values(
            universe=strategy.universe,
            instrument_types=strategy.instrument_types,
            boards=strategy.boards,
        )
        return [
            ref
            for ref in refs
            if scope.matches(
                ref.market,
                str(
                    classify_instrument(ref.market, ref.code)
                    or getattr(ref, "instrument_type", "stock")
                ),
                str(classify_board(ref.market, ref.code) or getattr(ref, "board", "main")),
            )
        ]

    def _batch_frames(
        self,
        refs: list[StockRef],
    ) -> Mapping[tuple[str, str], pd.DataFrame] | None:
        reader = getattr(self._adapter, "read_many_stocks", None)
        if reader is None:
            return None
        combined = reader(refs)
        frames: dict[tuple[str, str], pd.DataFrame] = {}
        if combined.empty:
            return frames
        for key, group in combined.groupby(["market", "code"], sort=False):
            frames[(str(key[0]), str(key[1]))] = group.reset_index(drop=True)
        return frames

    def _evaluate_stock(
        self,
        ref: StockRef,
        frame: pd.DataFrame,
        windows: list[_PhaseWindow],
        config: StrategyFitnessConfig,
        parsed: ParsedFormula | None,
        simulator: PortfolioBacktestService,
    ) -> StrategyFitnessResult:
        phase_reports: list[PortfolioBacktestReport | None] = []
        for window in windows:
            phase_config = replace(
                config.strategy,
                max_positions=1,
                rebalance_frequency="daily",
                start_date=window.start_date,
                end_date=window.end_date,
            )
            try:
                phase_reports.append(simulator.run_stock(ref, phase_config, parsed))
            except ValueError as exc:
                if not _is_missing_phase_data(str(exc)):
                    raise
                phase_reports.append(None)

        metrics = tuple(
            _phase_metrics(window, report) for window, report in zip(windows, phase_reports)
        )
        train, validation, test = metrics
        score, passed, label, passed_checks, checks, positive_periods = _score_result(
            train,
            validation,
            test,
            config,
        )
        return StrategyFitnessResult(
            market=ref.market,
            code=ref.code,
            name=getattr(ref, "name", None),
            bars=sum(item.bars for item in metrics),
            data_start=_date_text(_frame_start(frame, config.strategy.start_date)),
            data_end=_date_text(_frame_end(frame, config.strategy.end_date)),
            suitability_score=score,
            passed=passed,
            label=label,
            passed_checks=passed_checks,
            total_checks=len(checks),
            positive_periods=positive_periods,
            checks=tuple(checks),
            train=train,
            validation=validation,
            test=test,
            data_fingerprint=frame_fingerprint(frame),
        )

    @staticmethod
    def _prepare_frame(frame: pd.DataFrame) -> pd.DataFrame:
        prepared = frame.copy(deep=True)
        validate_market_data(prepared)
        prepared["date"] = pd.to_datetime(prepared["date"], errors="coerce")
        if prepared["date"].isna().any():
            raise ValueError("行情数据包含无效日期")
        return prepared.sort_values("date", kind="stable").reset_index(drop=True)

    @staticmethod
    def _evaluation_dates(
        frames: Mapping[str, pd.DataFrame],
        start_date: date | None,
        end_date: date | None,
    ) -> list[date]:
        dates = {
            date.fromisoformat(str(timestamp.date()))
            for frame in frames.values()
            for timestamp in pd.to_datetime(frame["date"])
        }
        bounded = sorted(
            value
            for value in dates
            if (start_date is None or value >= start_date)
            and (end_date is None or value <= end_date)
        )
        if len(bounded) < 3:
            raise ValueError("适配性评估至少需要 3 个交易日，以划分训练、验证和测试区间")
        return bounded

    @staticmethod
    def _empty_report(
        config: StrategyFitnessConfig,
        total_candidates: int,
        failures: Mapping[str, int],
        diagnostic: str,
    ) -> StrategyFitnessReport:
        return StrategyFitnessReport(
            universe=config.strategy.universe,
            total_candidates=total_candidates,
            processed=0,
            skipped=total_candidates,
            errors=sum(failures.values()),
            bars=0,
            start_date="",
            end_date="",
            train_end_date="",
            validation_end_date="",
            ranking_value=config.strategy.ranking_value,
            train_ratio=config.train_ratio,
            validation_ratio=config.validation_ratio,
            min_trades=config.min_trades,
            max_test_drawdown=config.max_test_drawdown,
            results=(),
            failure_reasons=failures,
            diagnostic=diagnostic,
        )


def ref_key(ref: StockRef) -> str:
    return f"{ref.market}{ref.code}"


def _split_windows(
    dates: list[date],
    train_ratio: float,
    validation_ratio: float,
) -> list[_PhaseWindow]:
    train_count = max(1, int(len(dates) * train_ratio))
    validation_count = max(1, int(len(dates) * validation_ratio))
    if train_count + validation_count >= len(dates):
        validation_count = 1
        train_count = max(1, len(dates) - validation_count - 1)
    validation_start = train_count
    test_start = train_count + validation_count
    return [
        _PhaseWindow("train", dates[0], dates[train_count - 1]),
        _PhaseWindow("validation", dates[validation_start], dates[test_start - 1]),
        _PhaseWindow("test", dates[test_start], dates[-1]),
    ]


def _phase_metrics(
    window: _PhaseWindow,
    report: PortfolioBacktestReport | None,
) -> FitnessPhaseMetrics:
    if report is None:
        return FitnessPhaseMetrics(
            name=window.name,
            start_date=_date_text(window.start_date),
            end_date=_date_text(window.end_date),
            bars=0,
            total_trades=0,
            win_rate=None,
            total_return=None,
            annual_return=None,
            max_drawdown=None,
            sharpe=None,
            profit_factor=None,
            expectancy=None,
            avg_holding_days=None,
            diagnostic="该区间没有可用回测数据",
        )
    performance = report.performance
    return FitnessPhaseMetrics(
        name=window.name,
        start_date=report.start_date,
        end_date=report.end_date,
        bars=report.bars,
        total_trades=int(_metric(performance, "total_trades") or 0),
        win_rate=_metric(performance, "win_rate"),
        total_return=_metric(performance, "total_return"),
        annual_return=_metric(performance, "annual_return"),
        max_drawdown=_metric(performance, "max_drawdown"),
        sharpe=_metric(performance, "sharpe"),
        profit_factor=_metric(performance, "profit_factor"),
        expectancy=_expectancy(performance),
        avg_holding_days=_metric(performance, "avg_holding_days"),
        diagnostic=report.diagnostic,
    )


def _score_result(
    train: FitnessPhaseMetrics,
    validation: FitnessPhaseMetrics,
    test: FitnessPhaseMetrics,
    config: StrategyFitnessConfig,
) -> tuple[float, bool, FitnessLabel, int, list[dict[str, object]], int]:
    checks = [
        {
            "id": "validation_trades",
            "label": "验证期成交数达标",
            "passed": validation.total_trades >= config.min_trades,
        },
        {
            "id": "test_trades",
            "label": "测试期成交数达标",
            "passed": test.total_trades >= config.min_trades,
        },
        {
            "id": "validation_expectancy",
            "label": "验证期期望收益为正",
            "passed": _positive(validation.expectancy),
        },
        {
            "id": "test_expectancy",
            "label": "测试期期望收益为正",
            "passed": _positive(test.expectancy),
        },
        {
            "id": "test_profit_factor",
            "label": "测试期盈亏比大于 1",
            "passed": test.profit_factor is not None and test.profit_factor > 1,
        },
        {
            "id": "test_drawdown",
            "label": "测试期最大回撤在阈值内",
            "passed": test.max_drawdown is not None
            and test.max_drawdown <= config.max_test_drawdown,
        },
        {
            "id": "validation_return",
            "label": "验证期总收益为正",
            "passed": _positive(validation.total_return),
        },
        {
            "id": "test_return",
            "label": "测试期总收益为正",
            "passed": _positive(test.total_return),
        },
    ]
    passed_checks = sum(bool(check["passed"]) for check in checks)
    score = round(passed_checks / len(checks) * 100, 2)
    enough_samples = (
        validation.total_trades >= config.min_trades and test.total_trades >= config.min_trades
    )
    label: FitnessLabel = (
        "insufficient"
        if not enough_samples
        else "strong"
        if score >= 75
        else "watch"
        if score >= 50
        else "weak"
    )
    return (
        score,
        bool(enough_samples and score >= 75),
        label,
        passed_checks,
        checks,
        sum(
            _positive(value)
            for value in (train.total_return, validation.total_return, test.total_return)
        ),
    )


def _metric(performance: Mapping[str, float | None], name: str) -> float | None:
    value = performance.get(name)
    if value is None or not np.isfinite(value):
        return None
    return float(value)


def _expectancy(performance: Mapping[str, float | None]) -> float | None:
    win_rate = _metric(performance, "win_rate")
    avg_win = _metric(performance, "avg_win")
    avg_loss = _metric(performance, "avg_loss")
    if win_rate is None or avg_win is None or avg_loss is None:
        return None
    return win_rate * avg_win + (1 - win_rate) * avg_loss


def _positive(value: float | None) -> bool:
    return value is not None and value > 0


def _is_missing_phase_data(message: str) -> bool:
    return any(
        marker in message
        for marker in ("数据为空", "数据不足", "日期范围内没有可用日线", "没有可用于回测的日期")
    )


def _frame_start(frame: pd.DataFrame, start_date: date | None) -> date:
    values = [
        date.fromisoformat(str(timestamp.date())) for timestamp in pd.to_datetime(frame["date"])
    ]
    bounded = [value for value in values if start_date is None or value >= start_date]
    return min(bounded or values)


def _frame_end(frame: pd.DataFrame, end_date: date | None) -> date:
    values = [
        date.fromisoformat(str(timestamp.date())) for timestamp in pd.to_datetime(frame["date"])
    ]
    bounded = [value for value in values if end_date is None or value <= end_date]
    return max(bounded or values)


def _date_text(value: date) -> str:
    return value.isoformat()
