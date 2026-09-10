"""Run parsed formula signals through the project's daily-bar trade engine."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from pathlib import Path
from typing import Protocol, cast

import numpy as np
import pandas as pd

from selector_app.formulas.common import validate_market_data
from selector_app.formulas.custom import (
    ParsedFormula,
    custom_output_id,
    evaluate_custom_formula,
    parse_formula,
)
from selector_app.formulas.registry import FORMULA_REGISTRY, FormulaRegistry
from selector_app.formulas.types import FormulaResult
from selector_app.indicators.cache import IndicatorCache, frame_fingerprint
from selector_app.market_data.adapter import DuckDbMarketDataAdapter
from selector_app.market_data.models import MarketCode, StockRef
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.screening.engine import evaluate_rule_group
from selector_app.screening.rules import RuleGroup

from .engine import SignalBacktestConfig, SignalBacktestResult, run_signal_backtest
from .models import BacktestConfig, BacktestReport, BacktestValue
from .profiles import profile_from_config

BacktestProgressCallback = Callable[[int, int], None]


class BacktestMarketDataAdapter(Protocol):
    def stock_ref(self, vipdoc_path: str | Path, market: MarketCode, code: str) -> StockRef: ...

    def read_stock(self, ref: StockRef) -> pd.DataFrame: ...


class BacktestService:
    """Calculate causal formula signals and simulate their trades."""

    def __init__(
        self,
        adapter: BacktestMarketDataAdapter | None = None,
        registry: FormulaRegistry | None = None,
        indicator_cache: IndicatorCache | None = None,
    ) -> None:
        self._adapter = adapter or DuckDbMarketDataAdapter(DuckDbMarketDataStore())
        self._registry = registry or FORMULA_REGISTRY
        self._indicator_cache = indicator_cache

    def run(
        self,
        config: BacktestConfig,
        progress_callback: BacktestProgressCallback | None = None,
    ) -> BacktestReport:
        if progress_callback is not None:
            progress_callback(0, 1)
        ref = self._adapter.stock_ref(config.vipdoc_path, config.market, config.code)
        frame = self._prepare_frame(self._adapter.read_stock(ref))
        if frame.empty:
            raise ValueError(f"{config.market} {config.code} 没有可用日线数据")
        data_fingerprint = frame_fingerprint(frame)

        parsed_formula, formula_results = self._calculate_formula_results(
            frame, config, market=config.market, code=config.code
        )
        minimum_bars = max(result.minimum_bars for result in formula_results.values())
        if len(frame) < minimum_bars:
            raise ValueError(
                f"历史数据不足，至少需要 {minimum_bars} 根 K 线，当前只有 {len(frame)} 根"
            )

        window_indices = self._window_indices(frame, config.start_date, config.end_date)
        if len(window_indices) == 0:
            raise ValueError("指定日期范围内没有可用日线数据")
        window = frame.iloc[window_indices].reset_index(drop=True)
        if config.buy_rule is None or config.sell_rule is None:
            raise ValueError("买入和卖出规则不能为空")
        buy_mask = self._rule_window(
            formula_results, config.buy_rule, window_indices, parsed_formula is not None
        )
        sell_mask = self._rule_window(
            formula_results, config.sell_rule, window_indices, parsed_formula is not None
        )
        result = self._run_engine(window, buy_mask, sell_mask, config)
        report = self._build_report(result, window, config, data_fingerprint)
        if progress_callback is not None:
            progress_callback(1, 1)
        return report

    def _calculate_formula_results(
        self,
        frame: pd.DataFrame,
        config: BacktestConfig,
        *,
        market: str = "",
        code: str = "",
    ) -> tuple[ParsedFormula | None, dict[str, FormulaResult]]:
        if config.formula_text:
            parsed = parse_formula(config.formula_text)
            available = {signal.id for signal in parsed.signals}
            signal_ids = self._rule_signal_ids(config.buy_rule, config.sell_rule)
            invalid_signals = [signal_id for signal_id in signal_ids if signal_id not in available]
            value_ids = self._rule_value_ids(config.buy_rule, config.sell_rule)
            available_values = {custom_output_id(name) for name in parsed.value_names}
            invalid_values = [
                value_id for value_id in value_ids if value_id not in available_values
            ]
            if invalid_signals:
                raise ValueError(
                    f"买入或卖出信号不属于当前自定义公式: {', '.join(invalid_signals)}"
                )
            if invalid_values:
                raise ValueError(f"未知指标输出: {', '.join(invalid_values)}")
            return parsed, (
                self._indicator_cache.get_or_compute_custom(
                    frame,
                    parsed,
                    config.formula_parameters,
                    market=market,
                    code=code,
                )
                if self._indicator_cache is not None
                else {"custom": evaluate_custom_formula(parsed, frame, config.formula_parameters)}
            )

        signal_ids = self._rule_signal_ids(config.buy_rule, config.sell_rule)
        for signal_id in signal_ids:
            if not self._registry.has_signal(signal_id):
                raise ValueError(f"未知选股信号: {signal_id}")
        value_ids = self._rule_value_ids(config.buy_rule, config.sell_rule)
        definitions_by_id = {
            definition.id: definition
            for definition in (
                *self._registry.formulas_for_signals(tuple(signal_ids)),
                *self._registry.formulas_for_values(tuple(value_ids)),
            )
        }
        definitions = tuple(definitions_by_id.values())
        for value_id in value_ids:
            if not self._registry.has_value(value_id):
                raise ValueError(f"未知指标输出: {value_id}")
        results = (
            self._indicator_cache.get_or_compute(
                frame,
                (definition.id for definition in definitions),
                market=market,
                code=code,
            )
            if self._indicator_cache is not None
            else {
                definition.id: definition.calculate(frame.copy(deep=True))
                for definition in definitions
            }
        )
        return None, results

    @staticmethod
    def _rule_signal_ids(*rules: RuleGroup | None) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(signal_id for rule in rules if rule for signal_id in rule.signals)
        )

    @staticmethod
    def _rule_value_ids(*rules: RuleGroup | None) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                value_id
                for rule in rules
                if rule
                for condition in rule.conditions
                for value_id in (condition.left_value, condition.right_value)
                if value_id is not None
            )
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
    def _window_indices(
        frame: pd.DataFrame,
        start_date: date | None,
        end_date: date | None,
    ) -> np.ndarray:
        dates = frame["date"].dt.date
        mask = pd.Series(True, index=frame.index)
        if start_date is not None:
            mask &= dates >= start_date
        if end_date is not None:
            mask &= dates <= end_date
        return np.flatnonzero(mask.to_numpy())

    def _rule_window(
        self,
        results: Mapping[str, FormulaResult],
        rule: RuleGroup,
        indices: np.ndarray,
        custom: bool,
    ) -> np.ndarray:
        mask = evaluate_rule_group(rule, results, self._registry, custom=custom)
        return np.asarray(mask[indices], dtype=bool).copy()

    @staticmethod
    def _run_engine(
        frame: pd.DataFrame,
        buy_mask: np.ndarray,
        sell_mask: np.ndarray,
        config: BacktestConfig,
    ) -> SignalBacktestResult:
        return run_signal_backtest(
            frame,
            buy_mask,
            sell_mask,
            SignalBacktestConfig(
                initial_cash=config.initial_cash,
                commission=config.commission,
                min_commission=config.min_commission,
                stamp_tax=config.stamp_tax,
                slippage=config.slippage,
                execution=config.execution,
                position_mode=config.position_mode,
                fixed_size=config.fixed_size,
                instrument_type=config.instrument_type or "stock",
            ),
            market=config.market,
            code=config.code,
        )

    @staticmethod
    def _build_report(
        result: SignalBacktestResult,
        frame: pd.DataFrame,
        config: BacktestConfig,
        data_fingerprint: str,
    ) -> BacktestReport:
        # Keep serialization here so pandas/numpy values never leak into the
        # JSON response contract.
        dates = frame["date"]
        profile = profile_from_config(
            config.instrument_type or "stock",
            commission=config.commission,
            min_commission=config.min_commission,
            stamp_tax=config.stamp_tax,
        )
        return BacktestReport(
            market=config.market,
            code=config.code,
            bars=len(frame),
            start_date=dates.iloc[0].strftime("%Y-%m-%d"),
            end_date=dates.iloc[-1].strftime("%Y-%m-%d"),
            buy_signal=config.buy_signal,
            sell_signal=config.sell_signal,
            performance={name: _number(value) for name, value in result.performance.items()},
            equity_curve=tuple(_frame_records(result.equity_curve)),
            trades=tuple(_frame_records(result.trades)),
            positions=tuple(_frame_records(result.positions)),
            configuration={
                "initial_cash": config.initial_cash,
                "commission": profile.commission,
                "min_commission": profile.min_commission,
                "stamp_tax": profile.stamp_tax,
                "slippage": config.slippage,
                "execution": config.execution,
                "position_mode": config.position_mode,
                "fixed_size": config.fixed_size,
                "instrument_type": profile.instrument_type,
                "board": config.board or "main",
                "virtual": profile.virtual,
                "data_fingerprint": data_fingerprint,
            },
            diagnostic=result.diagnostic,
        )


def _number(value: object) -> float | None:
    if value is None or pd.isna(value):
        return None
    return float(cast(float, value))


def _date_value(value: object) -> str:
    if isinstance(value, (int, np.integer)):
        timestamp = pd.to_datetime(str(int(value)), format="%Y%m%d")
        return str(timestamp.strftime("%Y-%m-%d"))
    return str(pd.Timestamp(value).strftime("%Y-%m-%d"))


def _json_value(value: object) -> BacktestValue:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return float(value)
    return str(value)


def _frame_records(frame: pd.DataFrame) -> list[dict[str, BacktestValue]]:
    records: list[dict[str, BacktestValue]] = []
    for source in frame.to_dict(orient="records"):
        record: dict[str, BacktestValue] = {}
        for name, value in source.items():
            if name == "datetime":
                record["date"] = _date_value(value)
            else:
                record[name] = _json_value(value)
        records.append(record)
    return records
