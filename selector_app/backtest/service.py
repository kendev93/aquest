"""Run parsed formula signals through the project's daily-bar trade engine."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
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
from selector_app.market_data.adjustment import CorporateAction, mark_adjusted
from selector_app.market_data.models import MarketCode, StockRef
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.screening.engine import evaluate_rule_group
from selector_app.screening.models import ScreenCondition
from selector_app.screening.rules import RuleGroup

from .engine import SignalBacktestConfig, SignalBacktestResult, run_signal_backtest
from .models import BacktestConfig, BacktestReport, BacktestValue
from .profiles import profile_from_config

BacktestProgressCallback = Callable[[int, int], None]


class BacktestMarketDataAdapter(Protocol):
    def stock_ref(self, vipdoc_path: str | Path, market: MarketCode, code: str) -> StockRef: ...

    def read_stock(self, ref: StockRef) -> pd.DataFrame: ...


@dataclass(frozen=True)
class PreparedBacktest:
    """One stock's loaded indicators and bars reusable across rule variants."""

    config: BacktestConfig
    ref: StockRef
    frame: pd.DataFrame
    formula_results: Mapping[str, FormulaResult]
    custom_formula: bool
    data_fingerprint: str
    feature_values: pd.DataFrame


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
        prepared = self.prepare(config)
        report = self.run_prepared(prepared)
        if progress_callback is not None:
            progress_callback(1, 1)
        return report

    def prepare(
        self,
        config: BacktestConfig,
        *,
        feature_ids: tuple[str, ...] = (),
    ) -> PreparedBacktest:
        """Load a stock and calculate indicators once for repeated rule tests."""

        ref = self._adapter.stock_ref(config.vipdoc_path, config.market, config.code)
        return self.prepare_for_ref(config, ref, feature_ids=feature_ids)

    def prepare_for_ref(
        self,
        config: BacktestConfig,
        ref: StockRef,
        *,
        feature_ids: tuple[str, ...] = (),
    ) -> PreparedBacktest:
        """Prepare an already enumerated stock without rescanning the universe."""

        frame = self._prepare_frame(self._adapter.read_stock(ref))
        if frame.empty:
            raise ValueError(f"{ref.market} {ref.code} 没有可用日线数据")
        # 指标按除权校正后的价格算，成交价保持真实价：除权日的 raw 跳空是价格序列里
        # 的假断崖，留着它算出来的指标是假的；而成交价一旦用复权价，股数与整手就不再
        # 对应现实。数据指纹跟指标口径走，这样事件补录后缓存会自动失效。
        metric_frame = self._metric_frame(ref, frame)
        data_fingerprint = frame_fingerprint(metric_frame)
        parsed_formula, formula_results = self._calculate_formula_results(
            metric_frame,
            config,
            market=ref.market,
            code=ref.code,
            additional_value_ids=feature_ids,
        )
        minimum_bars = max(result.minimum_bars for result in formula_results.values())
        if len(frame) < minimum_bars:
            raise ValueError(
                f"历史数据不足，至少需要 {minimum_bars} 根 K 线，当前只有 {len(frame)} 根"
            )
        window_indices = self._window_indices(frame, config.start_date, config.end_date)
        if len(window_indices) == 0:
            raise ValueError("指定日期范围内没有可用日线数据")
        if config.buy_rule is None and not config.always_in_market:
            raise ValueError("买入规则不能为空，或者开启「持续满仓」由系统在每个交易日尝试入场")
        if config.sell_rule is None and not config.close_at_end:
            raise ValueError("卖出规则不能为空，或者开启「持有到期末」由系统在最后一根 K 线平仓")
        feature_values = self._feature_frame(
            frame,
            formula_results,
            custom=parsed_formula is not None,
            feature_ids=feature_ids,
        )
        return PreparedBacktest(
            config=config,
            ref=ref,
            frame=frame,
            formula_results=formula_results,
            custom_formula=parsed_formula is not None,
            data_fingerprint=data_fingerprint,
            feature_values=feature_values,
        )

    def run_prepared(
        self,
        prepared: PreparedBacktest,
        *,
        buy_rule: RuleGroup | None = None,
        sell_rule: RuleGroup | None = None,
        buy_filter: ScreenCondition | None = None,
        sell_trigger: ScreenCondition | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        summary_only: bool = False,
    ) -> BacktestReport:
        """Simulate a rule variant without rereading bars or recalculating indicators.

        A buy filter is ANDed with the base buy rule. An added sell trigger is
        ORed with the base sell rule, preserving the user's original logic.

        Both sides fall back to their combining operation's identity element when
        the base rule is missing: the buy side to "always" (AND) and the sell side
        to "never" (OR). A candidate added to an empty side is therefore the whole
        rule on that side.
        """

        config = prepared.config
        selected_buy_rule = buy_rule or config.buy_rule
        selected_sell_rule = sell_rule or config.sell_rule
        if selected_buy_rule is None and not config.always_in_market:
            raise ValueError("买入规则不能为空，或者开启「持续满仓」由系统在每个交易日尝试入场")
        if selected_sell_rule is None and not config.close_at_end:
            raise ValueError("卖出规则不能为空，或者开启「持有到期末」由系统在最后一根 K 线平仓")
        if start_date is None and end_date is None:
            start_date = config.start_date
            end_date = config.end_date
        indices = self._window_indices(prepared.frame, start_date, end_date)
        if len(indices) == 0:
            raise ValueError("指定日期范围内没有可用日线数据")
        window = prepared.frame.iloc[indices].reset_index(drop=True)
        buy_mask = self._rule_window(
            prepared.formula_results,
            selected_buy_rule,
            indices,
            prepared.custom_formula,
            default=config.always_in_market,
        )
        sell_mask = self._rule_window(
            prepared.formula_results,
            selected_sell_rule,
            indices,
            prepared.custom_formula,
        )
        if buy_filter is not None:
            buy_mask &= self._condition_window(prepared, buy_filter, indices)
        if sell_trigger is not None:
            sell_mask |= self._condition_window(prepared, sell_trigger, indices)
        variant_config = replace(
            config,
            buy_rule=selected_buy_rule,
            sell_rule=selected_sell_rule,
            buy_signal=config.buy_signal if buy_rule is None else None,
            sell_signal=config.sell_signal if sell_rule is None else None,
            start_date=start_date,
            end_date=end_date,
        )
        result = self._run_engine(
            window,
            buy_mask,
            sell_mask,
            variant_config,
            name=prepared.ref.name,
            summary_only=summary_only,
        )
        return self._build_report(
            result,
            window,
            variant_config,
            prepared.data_fingerprint,
            name=prepared.ref.name,
            summary_only=summary_only,
        )

    def _condition_window(
        self,
        prepared: PreparedBacktest,
        condition: ScreenCondition,
        indices: np.ndarray,
    ) -> np.ndarray:
        rule = RuleGroup(conditions=(condition,), combine_mode="all")
        mask = evaluate_rule_group(
            rule,
            prepared.formula_results,
            self._registry,
            custom=prepared.custom_formula,
        )
        return np.asarray(mask[indices], dtype=bool).copy()

    @staticmethod
    def _feature_frame(
        frame: pd.DataFrame,
        results: Mapping[str, FormulaResult],
        *,
        custom: bool,
        feature_ids: tuple[str, ...],
    ) -> pd.DataFrame:
        selected = set(feature_ids)
        values: dict[str, np.ndarray] = {}
        for formula_id, result in results.items():
            for value_name, array in result.values.items():
                value_id = custom_output_id(value_name) if custom else f"{formula_id}.{value_name}"
                if not selected or value_id in selected:
                    values[value_id] = np.asarray(array, dtype=float)
        return pd.DataFrame({"date": frame["date"].to_numpy(), **values})

    def _calculate_formula_results(
        self,
        frame: pd.DataFrame,
        config: BacktestConfig,
        *,
        market: str = "",
        code: str = "",
        additional_value_ids: tuple[str, ...] = (),
    ) -> tuple[ParsedFormula | None, dict[str, FormulaResult]]:
        if config.formula_text:
            parsed = parse_formula(config.formula_text)
            available = {signal.id for signal in parsed.signals}
            signal_ids = self._rule_signal_ids(config.buy_rule, config.sell_rule)
            invalid_signals = [signal_id for signal_id in signal_ids if signal_id not in available]
            value_ids = (
                *self._rule_value_ids(config.buy_rule, config.sell_rule),
                *additional_value_ids,
            )
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
        value_ids = (
            *self._rule_value_ids(config.buy_rule, config.sell_rule),
            *additional_value_ids,
        )
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
                for condition in (*rule.required_conditions, *rule.conditions)
                for value_id in (condition.left_value, condition.right_value)
                if value_id is not None
            )
        )

    def _metric_frame(self, ref: StockRef, frame: pd.DataFrame) -> pd.DataFrame:
        """指标口径的行情：按分红送转事件校正后的价格，并带上事件版本标记。"""

        reader = getattr(self._adapter, "read_corporate_actions", None)
        if not callable(reader):
            return frame
        return mark_adjusted(frame, tuple(reader(ref)))

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
        # 数组比较替代逐次构建 pandas Series：研究流程会对同一只股票反复切片。
        days = frame["date"].to_numpy(dtype="datetime64[D]")
        mask = np.ones(days.shape, dtype=bool)
        if start_date is not None:
            mask &= days >= np.datetime64(start_date, "D")
        if end_date is not None:
            mask &= days <= np.datetime64(end_date, "D")
        return np.flatnonzero(mask)

    def _rule_window(
        self,
        results: Mapping[str, FormulaResult],
        rule: RuleGroup | None,
        indices: np.ndarray,
        custom: bool,
        *,
        default: bool = False,
    ) -> np.ndarray:
        if rule is None:
            # 空规则取合并运算的单位元：卖出侧是「恒不卖」（OR 的单位元 False，
            # 只等收官平仓），买入侧在「持续满仓」下是「天天买」（AND 的单位元 True）。
            return np.full(len(indices), default, dtype=bool)
        mask = evaluate_rule_group(rule, results, self._registry, custom=custom)
        return np.asarray(mask[indices], dtype=bool).copy()

    @staticmethod
    def _run_engine(
        frame: pd.DataFrame,
        buy_mask: np.ndarray,
        sell_mask: np.ndarray,
        config: BacktestConfig,
        *,
        name: str | None = None,
        summary_only: bool = False,
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
                close_at_end=config.close_at_end,
            ),
            market=config.market,
            code=config.code,
            name=name,
            summary_only=summary_only,
        )

    @staticmethod
    def _build_report(
        result: SignalBacktestResult,
        frame: pd.DataFrame,
        config: BacktestConfig,
        data_fingerprint: str,
        *,
        name: str | None = None,
        summary_only: bool = False,
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
            name=name,
            bars=len(frame),
            start_date=dates.iloc[0].strftime("%Y-%m-%d"),
            end_date=dates.iloc[-1].strftime("%Y-%m-%d"),
            buy_signal=config.buy_signal,
            sell_signal=config.sell_signal,
            performance={name: _number(value) for name, value in result.performance.items()},
            equity_curve=() if summary_only else tuple(_frame_records(result.equity_curve)),
            trades=tuple(_frame_records(result.trades)),
            positions=() if summary_only else tuple(_frame_records(result.positions)),
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
