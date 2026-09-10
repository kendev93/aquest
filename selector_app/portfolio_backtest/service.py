"""Dynamic ranked-slot portfolio backtesting for formula signals."""

from __future__ import annotations

import hashlib
from collections import Counter, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Protocol, cast

import numpy as np
import pandas as pd

from selector_app.backtest.performance import PerformanceAnalyzer
from selector_app.backtest.profiles import TradeProfile, profile_from_config
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
from selector_app.market_data.day_format import classify_board, classify_instrument
from selector_app.market_data.models import InstrumentBoard, InstrumentType, StockRef
from selector_app.market_data.scope import InstrumentScope
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.screening.engine import evaluate_rule_group
from selector_app.screening.rules import RuleGroup
from selector_app.strategy_fitness.rolling import (
    RollingFitnessDecision,
    RollingFitnessFilter,
    RollingFitnessHistory,
)

from .models import (
    PortfolioBacktestConfig,
    PortfolioBacktestReport,
    PortfolioJsonValue,
)

PortfolioBacktestProgressCallback = Callable[[int, int], None]


class PortfolioMarketDataAdapter(Protocol):
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
class _StockSeries:
    market: str
    code: str
    instrument_type: InstrumentType
    board: InstrumentBoard
    dates: np.ndarray
    opens: np.ndarray
    highs: np.ndarray
    lows: np.ndarray
    closes: np.ndarray
    atrs: np.ndarray
    buy_mask: np.ndarray
    sell_mask: np.ndarray
    signals: Mapping[str, np.ndarray]
    values: Mapping[str, np.ndarray]
    date_to_index: Mapping[int, int]
    data_fingerprint: str

    @property
    def symbol(self) -> str:
        return f"{self.market}{self.code}"


@dataclass
class _Position:
    market: str
    code: str
    instrument_type: InstrumentType
    board: InstrumentBoard
    size: float
    entry_price: float
    entry_date: int
    highest_price: float
    buy_commission: float
    buy_slippage: float

    @property
    def symbol(self) -> str:
        return f"{self.market}{self.code}"


@dataclass(frozen=True)
class _PendingSell:
    signal_date: int
    reason: str


@dataclass(frozen=True)
class _PendingBuy:
    symbol: str
    signal_date: int
    budget: float


class PortfolioBacktestService:
    """Select, rank, trade and refill a fixed number of portfolio slots."""

    def __init__(
        self,
        adapter: PortfolioMarketDataAdapter | None = None,
        registry: FormulaRegistry | None = None,
        indicator_cache: IndicatorCache | None = None,
    ) -> None:
        self._adapter = adapter or DuckDbMarketDataAdapter(DuckDbMarketDataStore())
        self._registry = registry or FORMULA_REGISTRY
        self._indicator_cache = indicator_cache

    def run(
        self,
        config: PortfolioBacktestConfig,
        progress_callback: PortfolioBacktestProgressCallback | None = None,
    ) -> PortfolioBacktestReport:
        refs = self._list_refs(config)
        parsed = parse_formula(config.formula_text) if config.formula_text else None
        self._validate_outputs(config, parsed)

        contexts: list[_StockSeries] = []
        failure_reasons: Counter[str] = Counter()
        frames = self._batch_frames(refs)
        for current, ref in enumerate(refs, start=1):
            try:
                context = self._build_context(
                    ref,
                    config,
                    parsed,
                    frame=frames.get((ref.market, ref.code)) if frames is not None else None,
                )
            except Exception as exc:  # noqa: BLE001 - one bad stock must not abort a portfolio
                reason = str(exc).strip()[:200] or type(exc).__name__
                failure_reasons[reason] += 1
                context = None
            if context is not None:
                contexts.append(context)
            if progress_callback is not None:
                progress_callback(current, len(refs))

        if not contexts:
            return self._empty_report(config, len(refs), failure_reasons)
        dates = self._global_dates(contexts)
        if not dates:
            return self._empty_report(config, len(refs), failure_reasons)

        fitness_filter = self._build_fitness_filter(contexts, config)
        simulation = self._simulate(contexts, dates, config, fitness_filter)
        report = self._build_report(
            config,
            dates,
            len(refs),
            len(contexts),
            len(refs) - len(contexts),
            failure_reasons,
            simulation,
            _portfolio_fingerprint(contexts),
        )
        if progress_callback is not None:
            progress_callback(len(refs), len(refs))
        return report

    def run_stock(
        self,
        ref: StockRef,
        config: PortfolioBacktestConfig,
        parsed_formula: ParsedFormula | None = None,
    ) -> PortfolioBacktestReport:
        """Run the same execution rules for one stock reference."""

        parsed = parsed_formula
        if parsed is None and config.formula_text:
            parsed = parse_formula(config.formula_text)
        self._validate_outputs(config, parsed)
        context = self._build_context(ref, config, parsed)
        if context is None:
            raise ValueError("股票没有可用于回测的行情数据")
        dates = self._global_dates([context])
        if not dates:
            raise ValueError("股票没有可用于回测的日期")
        simulation = self._simulate([context], dates, config)
        return self._build_report(
            config,
            dates,
            1,
            1,
            0,
            {},
            simulation,
            context.data_fingerprint,
        )

    def _build_fitness_filter(
        self,
        contexts: list[_StockSeries],
        config: PortfolioBacktestConfig,
    ) -> RollingFitnessFilter | None:
        if not config.fitness_filter_enabled:
            return None
        history_config = replace(config, fitness_filter_enabled=False, max_positions=1)
        histories: dict[str, RollingFitnessHistory] = {}
        for context in contexts:
            simulation = self._simulate(
                [context],
                [int(value) for value in context.dates],
                history_config,
                None,
            )
            histories[context.symbol] = RollingFitnessHistory.from_records(
                trades=cast(list[dict[str, PortfolioJsonValue]], simulation["trades"]),
                equity=cast(list[dict[str, PortfolioJsonValue]], simulation["equity"]),
            )
        return RollingFitnessFilter(
            histories,
            min_score=config.fitness_min_score,
            min_trades=config.fitness_min_trades,
            max_drawdown=config.fitness_max_drawdown,
        )

    def _validate_outputs(
        self,
        config: PortfolioBacktestConfig,
        parsed: ParsedFormula | None,
    ) -> None:
        signal_ids = list(self._rule_signal_ids(config.buy_rule, config.sell_rule))
        value_ids = self._value_ids(config)
        if parsed is not None:
            available_signals = {signal.id for signal in parsed.signals}
            invalid_signals = [signal for signal in signal_ids if signal not in available_signals]
            available_values = {custom_output_id(name) for name in parsed.value_names}
            invalid_values = [value for value in value_ids if value not in available_values]
        else:
            invalid_signals = [
                signal for signal in signal_ids if not self._registry.has_signal(signal)
            ]
            invalid_values = [value for value in value_ids if not self._registry.has_value(value)]
        if invalid_signals:
            raise ValueError(f"未知选股或卖出信号: {', '.join(invalid_signals)}")
        if invalid_values:
            raise ValueError(f"未知指标输出: {', '.join(invalid_values)}")

    @staticmethod
    def _value_ids(config: PortfolioBacktestConfig) -> list[str]:
        values = [config.ranking_value]
        for rule in (config.buy_rule, config.sell_rule):
            if rule is None:
                continue
            for condition in rule.conditions:
                values.append(condition.left_value)
                if condition.right_value is not None:
                    values.append(condition.right_value)
        return list(dict.fromkeys(values))

    @staticmethod
    def _rule_signal_ids(*rules: RuleGroup | None) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(signal_id for rule in rules if rule for signal_id in rule.signals)
        )

    def _build_context(
        self,
        ref: StockRef,
        config: PortfolioBacktestConfig,
        parsed: ParsedFormula | None,
        frame: pd.DataFrame | None = None,
    ) -> _StockSeries | None:
        frame = self._prepare_frame(frame if frame is not None else self._adapter.read_stock(ref))
        if frame.empty:
            raise ValueError("数据为空")
        data_fingerprint = frame_fingerprint(frame)
        results = self._calculate_results(
            frame,
            config,
            parsed,
            market=ref.market,
            code=ref.code,
        )
        minimum_bars = max(result.minimum_bars for result in results.values())
        if len(frame) < minimum_bars:
            raise ValueError(f"数据不足，至少需要 {minimum_bars} 根 K 线")
        indices = self._window_indices(frame, config.start_date, config.end_date)
        if len(indices) == 0:
            raise ValueError("日期范围内没有可用日线")

        signal_arrays = {
            signal_id: self._signal_array(results, signal_id, parsed is not None)[indices].copy()
            for signal_id in self._rule_signal_ids(config.buy_rule, config.sell_rule)
        }
        value_arrays = {
            value_id: self._value_array(results, value_id, parsed is not None)[indices].copy()
            for value_id in self._value_ids(config)
        }
        if config.buy_rule is None:
            raise ValueError("买入规则不能为空")
        buy_mask = evaluate_rule_group(
            config.buy_rule, results, self._registry, custom=parsed is not None
        )[indices].copy()
        sell_mask = (
            evaluate_rule_group(
                config.sell_rule, results, self._registry, custom=parsed is not None
            )[indices].copy()
            if config.sell_rule is not None
            else np.zeros(len(indices), dtype=bool)
        )
        window = frame.iloc[indices].reset_index(drop=True)
        dates = np.asarray(
            [int(pd.Timestamp(value).strftime("%Y%m%d")) for value in window["date"]],
            dtype=np.int64,
        )
        return _StockSeries(
            market=ref.market,
            code=ref.code,
            instrument_type=cast(InstrumentType, getattr(ref, "instrument_type", "stock")),
            board=cast(
                InstrumentBoard,
                classify_board(ref.market, ref.code) or getattr(ref, "board", "main"),
            ),
            dates=dates,
            opens=window["open"].to_numpy(dtype=float, copy=True),
            highs=window["high"].to_numpy(dtype=float, copy=True),
            lows=window["low"].to_numpy(dtype=float, copy=True),
            closes=window["close"].to_numpy(dtype=float, copy=True),
            atrs=_atr_series(frame, config.atr_period)[indices].copy(),
            buy_mask=np.asarray(buy_mask, dtype=bool),
            sell_mask=np.asarray(sell_mask, dtype=bool),
            signals=signal_arrays,
            values=value_arrays,
            date_to_index={int(value): index for index, value in enumerate(dates)},
            data_fingerprint=data_fingerprint,
        )

    def _batch_frames(self, refs: list[StockRef]) -> Mapping[tuple[str, str], pd.DataFrame] | None:
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

    def _list_refs(self, config: PortfolioBacktestConfig) -> list[StockRef]:
        if not config.instrument_types and not config.boards:
            return self._adapter.list_stock_refs(
                config.vipdoc_path,
                config.universe,
                config.universe_file,
            )
        try:
            refs = self._adapter.list_stock_refs(
                config.vipdoc_path,
                config.universe,
                config.universe_file,
                instrument_types=config.instrument_types,
                boards=config.boards,
            )
        except TypeError as exc:
            if "unexpected keyword" not in str(exc):
                raise
            refs = self._adapter.list_stock_refs(
                config.vipdoc_path,
                config.universe,
                config.universe_file,
            )
        scope = InstrumentScope.from_values(
            universe=config.universe,
            instrument_types=config.instrument_types,
            boards=config.boards,
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

    def _calculate_results(
        self,
        frame: pd.DataFrame,
        config: PortfolioBacktestConfig,
        parsed: ParsedFormula | None,
        *,
        market: str = "",
        code: str = "",
    ) -> dict[str, FormulaResult]:
        if parsed is not None:
            return (
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
        formula_ids = {
            self._registry.signal(signal_id).formula_id
            for signal_id in self._rule_signal_ids(config.buy_rule, config.sell_rule)
        }
        formula_ids.update(value_id.split(".", 1)[0] for value_id in self._value_ids(config))
        if self._indicator_cache is not None:
            return self._indicator_cache.get_or_compute(
                frame,
                formula_ids,
                market=market,
                code=code,
            )
        return {
            formula_id: self._registry.formula(formula_id).calculate(frame.copy(deep=True))
            for formula_id in formula_ids
        }

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

    def _signal_array(
        self,
        results: Mapping[str, FormulaResult],
        signal_id: str,
        custom: bool,
    ) -> np.ndarray:
        if custom:
            result = results["custom"]
            key = signal_id
        else:
            definition = self._registry.signal(signal_id)
            result = results[definition.formula_id]
            key = signal_id.split(".", 1)[1]
        return np.asarray(cast(object, result.signals[key]), dtype=bool)

    def _value_array(
        self,
        results: Mapping[str, FormulaResult],
        value_id: str,
        custom: bool,
    ) -> np.ndarray:
        formula_id, _, value_name = value_id.partition(".")
        result = results["custom"] if custom else results[formula_id]
        if custom:
            matches = {custom_output_id(name): values for name, values in result.values.items()}
            value = matches[value_id]
        else:
            value = result.values[value_name]
        return np.asarray(cast(object, value), dtype=float)

    @staticmethod
    def _global_dates(contexts: list[_StockSeries]) -> list[int]:
        return sorted({int(value) for context in contexts for value in context.dates})

    def _simulate(
        self,
        contexts: list[_StockSeries],
        dates: list[int],
        config: PortfolioBacktestConfig,
        fitness_filter: RollingFitnessFilter | None = None,
    ) -> dict[str, object]:
        by_symbol = {context.symbol: context for context in contexts}
        positions: dict[str, _Position] = {}
        pending_sells: dict[str, _PendingSell] = {}
        pending_buys: list[_PendingBuy] = []
        cooldown_until: dict[str, int] = {}
        last_prices: dict[str, float] = {}
        cash = config.initial_cash
        trades: list[dict[str, PortfolioJsonValue]] = []
        equity: list[dict[str, PortfolioJsonValue]] = []
        states: list[dict[str, PortfolioJsonValue]] = []
        ranking_events: list[dict[str, PortfolioJsonValue]] = []

        for date_index, current_date in enumerate(dates):
            cash = self._execute_sells(
                current_date,
                positions,
                pending_sells,
                by_symbol,
                last_prices,
                cash,
                config,
                trades,
            )
            cash = self._execute_risk_sells(
                date_index,
                current_date,
                positions,
                pending_sells,
                by_symbol,
                cash,
                config,
                cooldown_until,
                trades,
            )
            cash = self._execute_buys(
                current_date,
                positions,
                pending_sells,
                pending_buys,
                by_symbol,
                cash,
                config,
                cooldown_until,
                date_index,
                trades,
            )
            pending_buys = [
                pending
                for pending in pending_buys
                if pending.symbol not in positions
                and pending.symbol not in pending_sells
                and date_index >= cooldown_until.get(pending.symbol, -1)
                and pending.signal_date >= current_date
            ]
            for context in contexts:
                index = context.date_to_index.get(current_date)
                if index is not None:
                    last_prices[context.symbol] = float(context.closes[index])
            total_value, position_value = self._portfolio_value(positions, last_prices, cash)
            equity.append(
                {
                    "date": _date_text(current_date),
                    "cash": float(cash),
                    "position_value": float(position_value),
                    "total": float(total_value),
                    "positions_count": len(positions),
                }
            )
            states.append(
                self._state_record(
                    current_date, positions, last_prices, cash, total_value, position_value
                )
            )

            new_exit = self._schedule_exits(
                current_date,
                positions,
                pending_sells,
                by_symbol,
                config,
            )
            if self._should_refresh(date_index, dates, config.rebalance_frequency) or new_exit:
                pending_buys, event = self._select_candidates(
                    current_date,
                    positions,
                    pending_sells,
                    by_symbol,
                    last_prices,
                    cash,
                    total_value,
                    config,
                    fitness_filter,
                    cooldown_until,
                    date_index,
                )
                ranking_events.append(event)

        return {
            "cash": cash,
            "equity": equity,
            "states": states,
            "trades": trades,
            "ranking_events": ranking_events,
        }

    def _execute_sells(
        self,
        current_date: int,
        positions: dict[str, _Position],
        pending_sells: dict[str, _PendingSell],
        by_symbol: Mapping[str, _StockSeries],
        last_prices: Mapping[str, float],
        cash: float,
        config: PortfolioBacktestConfig,
        trades: list[dict[str, PortfolioJsonValue]],
    ) -> float:
        for symbol, pending in list(pending_sells.items()):
            position = positions.get(symbol)
            context = by_symbol[symbol]
            index = context.date_to_index.get(current_date)
            if position is None or index is None:
                continue
            price = self._execution_price(context, index, config)
            size = position.size
            profile = profile_from_config(
                position.instrument_type,
                commission=config.commission,
                min_commission=config.min_commission,
                stamp_tax=config.stamp_tax,
            )
            commission = max(size * price * profile.commission, profile.min_commission)
            stamp_tax = size * price * profile.stamp_tax
            slippage = size * config.slippage
            proceeds = size * price - commission - stamp_tax - slippage
            cash += proceeds
            trades.append(
                {
                    "date": _date_text(current_date),
                    "signal_date": _date_text(pending.signal_date),
                    "market": position.market,
                    "code": position.code,
                    "instrument_type": position.instrument_type,
                    "board": position.board,
                    "direction": "SELL",
                    "size": float(size),
                    "price": float(price),
                    "commission": float(commission),
                    "stamp_tax": float(stamp_tax),
                    "slippage": float(slippage),
                    "pnl": float(
                        (price - position.entry_price) * size
                        - position.buy_commission
                        - position.buy_slippage
                        - commission
                        - stamp_tax
                        - slippage
                    ),
                    "cost_basis": float(
                        position.entry_price * size
                        + position.buy_commission
                        + position.buy_slippage
                    ),
                    "reason": pending.reason,
                    "rejected": False,
                }
            )
            positions.pop(symbol, None)
            pending_sells.pop(symbol, None)
        return cash

    def _execute_risk_sells(
        self,
        date_index: int,
        current_date: int,
        positions: dict[str, _Position],
        pending_sells: Mapping[str, _PendingSell],
        by_symbol: Mapping[str, _StockSeries],
        cash: float,
        config: PortfolioBacktestConfig,
        cooldown_until: dict[str, int],
        trades: list[dict[str, PortfolioJsonValue]],
    ) -> float:
        """Execute OHLC-based risk exits on the trigger bar itself."""

        for symbol in sorted(list(positions)):
            if symbol in pending_sells:
                continue
            position = positions.get(symbol)
            context = by_symbol[symbol]
            index = context.date_to_index.get(current_date)
            if position is None or index is None:
                continue
            trigger = _risk_trigger(context, index, position, config)
            if trigger is None:
                continue
            trigger_name, trigger_price, cooldown_days = trigger
            price = float(trigger_price)
            size = position.size
            profile = profile_from_config(
                position.instrument_type,
                commission=config.commission,
                min_commission=config.min_commission,
                stamp_tax=config.stamp_tax,
            )
            commission = max(size * price * profile.commission, profile.min_commission)
            stamp_tax = size * price * profile.stamp_tax
            slippage = size * config.slippage
            proceeds = size * price - commission - stamp_tax - slippage
            cash += proceeds
            trades.append(
                {
                    "date": _date_text(current_date),
                    "signal_date": _date_text(current_date),
                    "market": position.market,
                    "code": position.code,
                    "instrument_type": position.instrument_type,
                    "board": position.board,
                    "direction": "SELL",
                    "size": float(size),
                    "price": price,
                    "commission": float(commission),
                    "stamp_tax": float(stamp_tax),
                    "slippage": float(slippage),
                    "pnl": float(
                        (price - position.entry_price) * size
                        - position.buy_commission
                        - position.buy_slippage
                        - commission
                        - stamp_tax
                        - slippage
                    ),
                    "cost_basis": float(
                        position.entry_price * size
                        + position.buy_commission
                        + position.buy_slippage
                    ),
                    "reason": _risk_trigger_label(trigger_name),
                    "risk_trigger": trigger_name,
                    "cooldown_days": cooldown_days,
                    "rejected": False,
                }
            )
            positions.pop(symbol, None)
            if cooldown_days > 0:
                cooldown_until[symbol] = date_index + cooldown_days + 1
        return cash

    def _execute_buys(
        self,
        current_date: int,
        positions: dict[str, _Position],
        pending_sells: Mapping[str, _PendingSell],
        pending_buys: list[_PendingBuy],
        by_symbol: Mapping[str, _StockSeries],
        cash: float,
        config: PortfolioBacktestConfig,
        cooldown_until: Mapping[str, int],
        date_index: int,
        trades: list[dict[str, PortfolioJsonValue]],
    ) -> float:
        for pending in pending_buys:
            if len(positions) >= config.max_positions:
                break
            if pending.symbol in positions or pending.symbol in pending_sells:
                continue
            if date_index < cooldown_until.get(pending.symbol, -1):
                continue
            context = by_symbol[pending.symbol]
            index = context.date_to_index.get(current_date)
            if index is None:
                continue
            price = self._execution_price(context, index, config)
            profile = profile_from_config(
                context.instrument_type,
                commission=config.commission,
                min_commission=config.min_commission,
                stamp_tax=config.stamp_tax,
            )
            shares = (
                int(max(pending.budget, 0) / price / profile.lot_size) * profile.lot_size
                if price > 0
                else 0
            )
            shares = self._affordable_shares(shares, price, cash, profile, config.slippage)
            if shares <= 0:
                continue
            commission = max(shares * price * profile.commission, profile.min_commission)
            slippage = shares * config.slippage
            cash -= shares * price + commission + slippage
            positions[pending.symbol] = _Position(
                market=context.market,
                code=context.code,
                instrument_type=context.instrument_type,
                board=context.board,
                size=float(shares),
                entry_price=float(price),
                entry_date=current_date,
                highest_price=float(price),
                buy_commission=float(commission),
                buy_slippage=float(slippage),
            )
            trades.append(
                {
                    "date": _date_text(current_date),
                    "signal_date": _date_text(pending.signal_date),
                    "market": context.market,
                    "code": context.code,
                    "instrument_type": context.instrument_type,
                    "board": context.board,
                    "direction": "BUY",
                    "size": float(shares),
                    "price": float(price),
                    "commission": float(commission),
                    "stamp_tax": 0.0,
                    "slippage": float(slippage),
                    "pnl": 0.0,
                    "cost_basis": 0.0,
                    "reason": "排名入选",
                    "rejected": False,
                }
            )
        return cash

    @staticmethod
    def _affordable_shares(
        shares: int,
        price: float,
        cash: float,
        profile: TradeProfile,
        slippage: float,
    ) -> int:
        while shares >= profile.lot_size:
            commission = max(shares * price * profile.commission, profile.min_commission)
            total_cost = shares * price + commission + shares * slippage
            if total_cost <= cash:
                return shares
            shares -= profile.lot_size
        return 0

    @staticmethod
    def _execution_price(
        context: _StockSeries,
        index: int,
        config: PortfolioBacktestConfig,
    ) -> float:
        return float(
            context.opens[index] if config.execution == "next_open" else context.closes[index]
        )

    def _schedule_exits(
        self,
        current_date: int,
        positions: Mapping[str, _Position],
        pending_sells: dict[str, _PendingSell],
        by_symbol: Mapping[str, _StockSeries],
        config: PortfolioBacktestConfig,
    ) -> bool:
        scheduled = False
        for symbol, position in positions.items():
            if symbol in pending_sells:
                continue
            context = by_symbol[symbol]
            index = context.date_to_index.get(current_date)
            if index is None:
                continue
            reasons = self._sell_reasons(context, index, position, config)
            if reasons:
                pending_sells[symbol] = _PendingSell(current_date, "、".join(reasons))
                scheduled = True
        return scheduled

    def _sell_reasons(
        self,
        context: _StockSeries,
        index: int,
        position: _Position,
        config: PortfolioBacktestConfig,
    ) -> list[str]:
        reasons: list[str] = []
        if bool(context.sell_mask[index]):
            reasons.append("卖出条件")
        return reasons

    def _select_candidates(
        self,
        current_date: int,
        positions: Mapping[str, _Position],
        pending_sells: Mapping[str, _PendingSell],
        by_symbol: Mapping[str, _StockSeries],
        last_prices: Mapping[str, float],
        cash: float,
        total_value: float,
        config: PortfolioBacktestConfig,
        fitness_filter: RollingFitnessFilter | None,
        cooldown_until: Mapping[str, int],
        date_index: int,
    ) -> tuple[list[_PendingBuy], dict[str, PortfolioJsonValue]]:
        excluded = set(positions) | set(pending_sells)
        candidates: list[tuple[float, _StockSeries]] = []
        ranked_candidates: list[tuple[float, _StockSeries, RollingFitnessDecision | None]] = []
        for context in by_symbol.values():
            if context.symbol in excluded:
                continue
            if date_index < cooldown_until.get(context.symbol, -1):
                continue
            index = context.date_to_index.get(current_date)
            if index is None:
                continue
            if not bool(context.buy_mask[index]):
                continue
            score = float(context.values[config.ranking_value][index])
            if not np.isfinite(score):
                continue
            decision = (
                fitness_filter.decide(context.symbol, current_date) if fitness_filter else None
            )
            ranked_candidates.append((score, context, decision))
            if decision is None or decision.eligible:
                candidates.append((score, context))
        ranked_candidates.sort(
            key=lambda item: (
                -item[0] if config.rank_order == "desc" else item[0],
                item[1].symbol,
            )
        )
        candidates.sort(
            key=lambda item: (-item[0] if config.rank_order == "desc" else item[0], item[1].symbol)
        )
        slots = max(0, config.max_positions - len(positions) + len(pending_sells))
        selected = candidates[:slots]
        slot_budget = total_value / config.max_positions if config.max_positions else cash
        pending = [
            _PendingBuy(context.symbol, current_date, slot_budget) for _, context in selected
        ]
        event_candidates: list[dict[str, PortfolioJsonValue]] = []
        selected_symbols = {context.symbol for _, context in selected}
        for rank, (score, context, decision) in enumerate(
            ranked_candidates[: max(slots, 10)], start=1
        ):
            candidate: dict[str, PortfolioJsonValue] = {
                "rank": rank,
                "market": context.market,
                "code": context.code,
                "score": score,
                "selected": context.symbol in selected_symbols,
            }
            if decision is not None:
                candidate.update(
                    {
                        "fitness_score": decision.score,
                        "fitness_trades": decision.trades,
                        "fitness_passed": decision.eligible,
                        "excluded_reason": None if decision.eligible else decision.reason,
                    }
                )
            event_candidates.append(candidate)
        return pending, {
            "date": _date_text(current_date),
            "slots_available": slots,
            "ranking_value": config.ranking_value,
            "candidates": event_candidates,
        }

    @staticmethod
    def _should_refresh(
        index: int,
        dates: list[int],
        frequency: str,
    ) -> bool:
        if index == 0 or frequency == "daily" or index == len(dates) - 1:
            return True
        current = pd.to_datetime(str(dates[index]), format="%Y%m%d")
        following = pd.to_datetime(str(dates[index + 1]), format="%Y%m%d")
        if frequency == "weekly":
            return int(current.isocalendar().week) != int(following.isocalendar().week)
        return (current.year, current.month) != (following.year, following.month)

    @staticmethod
    def _portfolio_value(
        positions: Mapping[str, _Position],
        last_prices: Mapping[str, float],
        cash: float,
    ) -> tuple[float, float]:
        position_value = sum(
            position.size * last_prices.get(symbol, position.entry_price)
            for symbol, position in positions.items()
        )
        return cash + position_value, position_value

    @staticmethod
    def _state_record(
        current_date: int,
        positions: Mapping[str, _Position],
        last_prices: Mapping[str, float],
        cash: float,
        total_value: float,
        position_value: float,
    ) -> dict[str, PortfolioJsonValue]:
        holdings = [
            {
                "market": position.market,
                "code": position.code,
                "instrument_type": position.instrument_type,
                "board": position.board,
                "size": float(position.size),
                "entry_price": float(position.entry_price),
                "close": float(last_prices.get(symbol, position.entry_price)),
                "unrealized_pnl": float(
                    (last_prices.get(symbol, position.entry_price) - position.entry_price)
                    * position.size
                ),
            }
            for symbol, position in sorted(positions.items())
        ]
        return {
            "date": _date_text(current_date),
            "cash": float(cash),
            "position_value": float(position_value),
            "total": float(total_value),
            "positions_count": len(holdings),
            "holdings": holdings,
        }

    def _build_report(
        self,
        config: PortfolioBacktestConfig,
        dates: list[int],
        total_candidates: int,
        processed: int,
        skipped: int,
        failure_reasons: Mapping[str, int],
        simulation: Mapping[str, object],
        data_fingerprint: str = "",
    ) -> PortfolioBacktestReport:
        equity = cast(list[dict[str, PortfolioJsonValue]], simulation["equity"])
        trades = cast(list[dict[str, PortfolioJsonValue]], simulation["trades"])
        states = cast(list[dict[str, PortfolioJsonValue]], simulation["states"])
        ranking_events = cast(list[dict[str, PortfolioJsonValue]], simulation["ranking_events"])
        equity_frame = pd.DataFrame(equity)
        trade_frame = pd.DataFrame(
            [
                {
                    "datetime": _date_int(cast(str, trade["date"])),
                    "direction": trade["direction"],
                    "size": trade["size"],
                    "price": trade["price"],
                    "commission": float(cast(float, trade["commission"]))
                    + float(cast(float, trade["stamp_tax"])),
                    "slippage": trade["slippage"],
                    "pnl": trade["pnl"],
                    "cost_basis": trade["cost_basis"],
                    "rejected": trade["rejected"],
                }
                for trade in trades
            ],
            columns=[
                "datetime",
                "direction",
                "size",
                "price",
                "commission",
                "slippage",
                "pnl",
                "cost_basis",
                "rejected",
            ],
        )
        diagnostic: str | None
        if equity_frame.empty:
            performance: dict[str, float | None] = {}
            diagnostic = "没有可用于回测的组合净值数据"
        else:
            peak = equity_frame["total"].cummax()
            equity_frame["drawdown"] = peak - equity_frame["total"]
            equity_frame["drawdown_pct"] = np.divide(
                equity_frame["drawdown"],
                peak,
                out=np.zeros(len(equity_frame), dtype=float),
                where=peak.to_numpy() != 0,
            )
            analyzer = PerformanceAnalyzer(equity_frame, trade_frame)
            performance = {
                name: _finite_number(value) for name, value in analyzer.compute().items()
            }
            performance["avg_holding_days"] = _average_holding_days(trades)
            diagnostic = analyzer.diagnostic
        serialized_equity = tuple(
            {
                **record,
                "drawdown": float(equity_frame.iloc[index]["drawdown"]),
                "drawdown_pct": float(equity_frame.iloc[index]["drawdown_pct"]),
            }
            for index, record in enumerate(equity)
        )
        return PortfolioBacktestReport(
            universe=config.universe,
            total_candidates=total_candidates,
            processed=processed,
            skipped=skipped,
            errors=sum(failure_reasons.values()),
            bars=len(dates),
            start_date=_date_text(dates[0]),
            end_date=_date_text(dates[-1]),
            max_positions=config.max_positions,
            ranking_value=config.ranking_value,
            rank_order=config.rank_order,
            fitness_filter_enabled=config.fitness_filter_enabled,
            fitness_min_score=config.fitness_min_score,
            fitness_min_trades=config.fitness_min_trades,
            fitness_max_drawdown=config.fitness_max_drawdown,
            performance=performance,
            equity_curve=serialized_equity,
            trades=tuple(trades),
            states=tuple(states),
            ranking_events=tuple(ranking_events),
            failure_reasons=failure_reasons,
            diagnostic=diagnostic,
            data_fingerprint=data_fingerprint,
            risk_controls=_risk_controls(config),
        )

    @staticmethod
    def _empty_report(
        config: PortfolioBacktestConfig,
        total_candidates: int,
        failure_reasons: Mapping[str, int],
    ) -> PortfolioBacktestReport:
        return PortfolioBacktestReport(
            universe=config.universe,
            total_candidates=total_candidates,
            processed=0,
            skipped=total_candidates,
            errors=sum(failure_reasons.values()),
            bars=0,
            start_date=config.start_date.isoformat() if config.start_date else "",
            end_date=config.end_date.isoformat() if config.end_date else "",
            max_positions=config.max_positions,
            ranking_value=config.ranking_value,
            rank_order=config.rank_order,
            fitness_filter_enabled=config.fitness_filter_enabled,
            fitness_min_score=config.fitness_min_score,
            fitness_min_trades=config.fitness_min_trades,
            fitness_max_drawdown=config.fitness_max_drawdown,
            performance={},
            equity_curve=(),
            trades=(),
            states=(),
            ranking_events=(),
            failure_reasons=failure_reasons,
            diagnostic="没有可用于回测的股票数据",
            risk_controls=_risk_controls(config),
        )


def _date_text(value: int) -> str:
    return str(pd.to_datetime(str(value), format="%Y%m%d").strftime("%Y-%m-%d"))


def _portfolio_fingerprint(contexts: list[_StockSeries]) -> str:
    digest = hashlib.sha256()
    for context in sorted(contexts, key=lambda item: item.symbol):
        digest.update(f"{context.symbol}:{context.data_fingerprint}\n".encode("ascii"))
    return digest.hexdigest()


def _date_int(value: str) -> int:
    return int(value.replace("-", ""))


def _finite_number(value: object) -> float | None:
    number = float(cast(float, value))
    return number if np.isfinite(number) else None


def _atr_series(frame: pd.DataFrame, period: int) -> np.ndarray:
    """Calculate a causal ATR series from daily OHLC data."""

    high = pd.to_numeric(frame["high"], errors="coerce")
    low = pd.to_numeric(frame["low"], errors="coerce")
    close = pd.to_numeric(frame["close"], errors="coerce")
    previous_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()],
        axis=1,
    ).max(axis=1)
    return np.asarray(
        true_range.rolling(period, min_periods=period).mean().to_numpy(dtype=float),
        dtype=float,
    )


def _risk_trigger(
    context: _StockSeries,
    index: int,
    position: _Position,
    config: PortfolioBacktestConfig,
) -> tuple[str, float, int] | None:
    """Return the first OHLC risk trigger using deterministic priority."""

    high = float(context.highs[index])
    low = float(context.lows[index])
    open_price = float(context.opens[index])
    if not (np.isfinite(high) and np.isfinite(low) and np.isfinite(open_price)):
        return None
    position.highest_price = max(position.highest_price, high)
    atr = float(context.atrs[index]) if np.isfinite(context.atrs[index]) else None

    stop_level = _risk_level(
        position.entry_price,
        config.stop_loss_mode,
        config.stop_loss_pct,
        config.stop_loss_atr_multiplier,
        atr,
        direction="down",
    )
    if stop_level is not None and low <= stop_level:
        return (
            "stop_loss",
            _risk_fill_price(open_price, stop_level, direction="down"),
            config.stop_loss_cooldown_days,
        )

    trailing_level = _risk_level(
        position.highest_price,
        config.trailing_stop_mode,
        config.trailing_stop_pct,
        config.trailing_stop_atr_multiplier,
        atr,
        direction="down",
    )
    if trailing_level is not None and low <= trailing_level:
        return (
            "trailing_stop",
            _risk_fill_price(open_price, trailing_level, direction="down"),
            config.trailing_stop_cooldown_days,
        )

    take_level = _risk_level(
        position.entry_price,
        config.take_profit_mode,
        config.take_profit_pct,
        config.take_profit_atr_multiplier,
        atr,
        direction="up",
    )
    if take_level is not None and high >= take_level:
        return (
            "take_profit",
            _risk_fill_price(open_price, take_level, direction="up"),
            config.take_profit_cooldown_days,
        )
    return None


def _risk_level(
    base: float,
    mode: str,
    percentage: float | None,
    atr_multiplier: float | None,
    atr: float | None,
    *,
    direction: str,
) -> float | None:
    if mode == "atr":
        if atr_multiplier is None or atr is None:
            return None
        distance = atr * atr_multiplier
    else:
        if percentage is None:
            return None
        distance = base * percentage
    return base - distance if direction == "down" else base + distance


def _risk_fill_price(open_price: float, trigger_price: float, *, direction: str) -> float:
    if direction == "down":
        return open_price if open_price <= trigger_price else trigger_price
    return open_price if open_price >= trigger_price else trigger_price


def _risk_trigger_label(trigger: str) -> str:
    return {
        "stop_loss": "止损",
        "take_profit": "止盈",
        "trailing_stop": "移动止盈",
    }.get(trigger, "风险卖出")


def _risk_controls(config: PortfolioBacktestConfig) -> dict[str, object]:
    return {
        "take_profit_mode": config.take_profit_mode,
        "take_profit_pct": config.take_profit_pct,
        "take_profit_atr_multiplier": config.take_profit_atr_multiplier,
        "take_profit_cooldown_days": config.take_profit_cooldown_days,
        "stop_loss_mode": config.stop_loss_mode,
        "stop_loss_pct": config.stop_loss_pct,
        "stop_loss_atr_multiplier": config.stop_loss_atr_multiplier,
        "stop_loss_cooldown_days": config.stop_loss_cooldown_days,
        "trailing_stop_mode": config.trailing_stop_mode,
        "trailing_stop_pct": config.trailing_stop_pct,
        "trailing_stop_atr_multiplier": config.trailing_stop_atr_multiplier,
        "trailing_stop_cooldown_days": config.trailing_stop_cooldown_days,
        "atr_period": config.atr_period,
    }


def _average_holding_days(trades: list[dict[str, PortfolioJsonValue]]) -> float:
    """Pair buys and sells with FIFO queues kept separately for each stock."""

    buy_queues: dict[str, deque[tuple[date, float]]] = {}
    total_days = 0.0
    total_size = 0.0
    for trade in trades:
        if bool(trade.get("rejected", False)):
            continue
        try:
            timestamp = pd.Timestamp(trade["date"])
            if pd.isna(timestamp):
                continue
            trade_date = timestamp.date()
            size = float(cast(float, trade["size"]))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if size <= 0:
            continue
        symbol = f"{trade.get('market', '')}{trade.get('code', '')}"
        queue = buy_queues.setdefault(symbol, deque())
        if trade.get("direction") == "BUY":
            queue.append((trade_date, size))
            continue
        if trade.get("direction") != "SELL":
            continue
        remaining = size
        while remaining > 0 and queue:
            buy_date, buy_size = queue[0]
            consumed = min(remaining, buy_size)
            total_days += max((trade_date - buy_date).days, 0) * consumed
            total_size += consumed
            remaining -= consumed
            buy_size -= consumed
            if buy_size <= 0:
                queue.popleft()
            else:
                queue[0] = (buy_date, buy_size)
    return total_days / total_size if total_size else 0.0
