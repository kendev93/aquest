"""Market-data and indicator context preparation for portfolio backtests."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import NamedTuple, cast

import numpy as np
import pandas as pd

from selector_app.formulas.common import validate_market_data
from selector_app.formulas.custom import (
    ParsedFormula,
    custom_output_id,
    evaluate_custom_formula,
)
from selector_app.formulas.registry import FORMULA_REGISTRY
from selector_app.formulas.types import FormulaResult
from selector_app.indicators.cache import frame_fingerprint
from selector_app.market_data.adapter import parse_universe_code
from selector_app.market_data.day_format import classify_board, classify_instrument
from selector_app.market_data.models import InstrumentBoard, InstrumentType, MarketCode, StockRef
from selector_app.market_data.risk_warning import is_risk_warning_symbol
from selector_app.market_data.scope import InstrumentScope
from selector_app.screening.engine import evaluate_rule_group
from selector_app.screening.rules import RuleGroup

from .models import SINGLE_SECURITY_RANKING_VALUE, PortfolioBacktestConfig
from .risk import _atr_series
from .state import PortfolioMarketDataAdapter, _PortfolioServiceState

_CONTEXT_PRELOAD_CHUNK_SIZE = 32
DEFAULT_BASE_CONTEXT_CACHE_LIMIT = 256
DEFAULT_DATE_INDEX_CACHE_LIMIT = 512

#: How much history to load *before* the backtest window so rolling indicators
#: have something to roll over. Without it every stock was loaded from its first
#: ever bar - thousands of bars of indicator work for a few hundred days of
#: backtest, which is why shortening the range never made a run any faster.
_WARMUP_BARS = max(
    (cast(int, item["recommended_bars"]) for item in FORMULA_REGISTRY.metadata()),
    default=0,
)


class _ResolvedRefs(NamedTuple):
    """解析出来的标的，外加没能进入回测的三类代码。"""

    refs: list[StockRef]
    #: 行情库里找不到的代码。
    missing: tuple[str, ...] = ()
    #: 找到了但被板块/类型范围过滤掉的代码。
    filtered: tuple[str, ...] = ()
    #: 打开了 exclude_st 后被当作 ST / *ST / 退市剔除的代码。
    excluded_st: tuple[str, ...] = ()


class SortedDateIndex(Mapping[int, int]):
    """Compact mapping facade over sorted integer dates."""

    def __init__(self, dates: np.ndarray) -> None:
        values = np.asarray(dates, dtype=np.int64)
        if values.ndim != 1:
            raise ValueError("日期索引必须是一维数组")
        self._dates = values

    def __getitem__(self, key: int) -> int:
        try:
            value = int(key)
        except (TypeError, ValueError, OverflowError) as exc:
            raise KeyError(key) from exc
        position = int(np.searchsorted(self._dates, value, side="left"))
        if position >= len(self._dates) or int(self._dates[position]) != value:
            raise KeyError(key)
        return position

    def __iter__(self) -> Iterator[int]:
        return (int(value) for value in self._dates)

    def __len__(self) -> int:
        return int(self._dates.size)


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
    name: str | None = None

    @property
    def symbol(self) -> str:
        return f"{self.market}{self.code}"


class PortfolioContextMixin(_PortfolioServiceState):
    """Build validated per-instrument arrays consumed by the simulator."""

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
        values = [] if config.single_security_mode else [config.ranking_value]
        for rule in (config.buy_rule, config.sell_rule):
            if rule is None:
                continue
            for condition in (*rule.required_conditions, *rule.conditions):
                values.append(condition.left_value)
                if condition.right_value is not None:
                    values.append(condition.right_value)
        return list(dict.fromkeys(values))

    @staticmethod
    def _rule_signal_ids(*rules: RuleGroup | None) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(signal_id for rule in rules if rule for signal_id in rule.signals)
        )

    def _formula_ids_for_config(self, config: PortfolioBacktestConfig) -> tuple[str, ...]:
        formula_ids = {
            self._registry.signal(signal_id).formula_id
            for signal_id in self._rule_signal_ids(config.buy_rule, config.sell_rule)
        }
        formula_ids.update(value_id.split(".", 1)[0] for value_id in self._value_ids(config))
        return tuple(sorted(formula_ids))

    def _preload_indicator_cache(
        self,
        frames: Mapping[tuple[str, str], pd.DataFrame] | None,
        config: PortfolioBacktestConfig,
        parsed: ParsedFormula | None,
    ) -> None:
        """Batch-load built-in cache rows before parallel context construction."""

        if self._indicator_cache is None or parsed is not None or not frames:
            return
        formula_ids = self._formula_ids_for_config(config)
        if not formula_ids:
            return
        items = list(frames.items())
        for offset in range(0, len(items), _CONTEXT_PRELOAD_CHUNK_SIZE):
            self._indicator_cache.preload(
                dict(items[offset : offset + _CONTEXT_PRELOAD_CHUNK_SIZE]),
                formula_ids,
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
        if config.single_security_mode:
            value_arrays[SINGLE_SECURITY_RANKING_VALUE] = np.zeros(len(indices), dtype=float)
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
            name=getattr(ref, "name", None),
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
            date_to_index=SortedDateIndex(dates),
            data_fingerprint=data_fingerprint,
        )

    def _supports_batch_frames(self) -> bool:
        """Whether the adapter can serve many instruments in one round trip."""

        return getattr(self._adapter, "read_many_stocks", None) is not None

    def _batch_frames(
        self,
        refs: list[StockRef],
        config: PortfolioBacktestConfig,
    ) -> Mapping[tuple[str, str], pd.DataFrame] | None:
        reader = getattr(self._adapter, "read_many_stocks", None)
        if reader is None:
            return None
        combined = reader(
            refs,
            start_date=config.start_date,
            end_date=config.end_date,
            warmup_bars=_WARMUP_BARS if config.start_date is not None else 0,
        )
        frames: dict[tuple[str, str], pd.DataFrame] = {}
        if combined.empty:
            return frames
        for key, group in combined.groupby(["market", "code"], sort=False):
            frames[(str(key[0]), str(key[1]))] = group.reset_index(drop=True)
        return frames

    def _list_refs(self, config: PortfolioBacktestConfig) -> _ResolvedRefs:
        if config.universe == "codes":
            return self._list_refs_by_codes(config)
        refs = self._list_universe_refs(config)
        if not config.exclude_st:
            return _ResolvedRefs(refs=refs)
        kept: list[StockRef] = []
        excluded: list[str] = []
        for ref in refs:
            if is_risk_warning_symbol(ref):
                excluded.append(f"{ref.market}{ref.code}")
            else:
                kept.append(ref)
        return _ResolvedRefs(refs=kept, excluded_st=tuple(excluded))

    def _list_universe_refs(self, config: PortfolioBacktestConfig) -> list[StockRef]:
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

    def _list_refs_by_codes(self, config: PortfolioBacktestConfig) -> _ResolvedRefs:
        """Resolve an explicit code list, preserving the user's ordering.

        找不到或被范围过滤掉的代码不能悄悄丢掉：用户明确点名的标的少了几只，
        回测照样跑完、报告照样好看，只有 `total_candidates` 会小一点——而没人
        会去数它。所以两者都一并返回，交给报告显示。
        """

        requested: list[tuple[MarketCode, str]] = []
        seen: set[tuple[MarketCode, str]] = set()
        for raw in config.codes or ():
            parsed = parse_universe_code(raw)
            if parsed is None or parsed in seen:
                continue
            seen.add(parsed)
            requested.append(parsed)
        if not requested:
            raise ValueError("指定标的范围必须提供至少一个有效代码")
        refs: list[StockRef] = []
        missing: list[str] = []
        for market, code in requested:
            try:
                refs.append(self._adapter.stock_ref(config.vipdoc_path, market, code))
            except (ValueError, KeyError, LookupError):
                missing.append(f"{market}{code}")
        if missing and not refs:
            raise ValueError(f"找不到已导入的行情: {', '.join(missing)}")
        scope = InstrumentScope.from_values(
            universe=config.universe,
            instrument_types=config.instrument_types,
            boards=config.boards,
        )
        kept: list[StockRef] = []
        filtered: list[str] = []
        excluded: list[str] = []
        for ref in refs:
            if config.exclude_st and is_risk_warning_symbol(ref):
                excluded.append(f"{ref.market}{ref.code}")
                continue
            if scope.matches(
                ref.market,
                str(
                    classify_instrument(ref.market, ref.code)
                    or getattr(ref, "instrument_type", "stock")
                ),
                str(classify_board(ref.market, ref.code) or getattr(ref, "board", "main")),
            ):
                kept.append(ref)
            else:
                filtered.append(f"{ref.market}{ref.code}")
        return _ResolvedRefs(
            refs=kept,
            missing=tuple(missing),
            filtered=tuple(filtered),
            excluded_st=tuple(excluded),
        )

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
        formula_ids = self._formula_ids_for_config(config)
        if self._indicator_cache is not None:
            # 只读：回测请求的是窗口，写回缓存只会留下无法复用的碎片，
            # 而且每个线程都要抢同一把写锁。
            return self._indicator_cache.get_or_compute(
                frame,
                formula_ids,
                market=market,
                code=code,
                persist=False,
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

    @staticmethod
    def _has_preflight_candidates(
        contexts: Sequence[_StockSeries], config: PortfolioBacktestConfig
    ) -> bool:
        """Check for a logically executable buy before entering the state machine."""

        for context in contexts:
            ranking_values = context.values.get(config.ranking_value)
            if ranking_values is None:
                continue
            execution_prices = context.opens if config.execution == "next_open" else context.closes
            ranking_array = np.asarray(ranking_values, dtype=float)
            execution_array = np.asarray(execution_prices, dtype=float)
            candidate_mask = (
                np.asarray(context.buy_mask, dtype=bool)
                & np.isfinite(ranking_array)
                & np.isfinite(execution_array)
                & (execution_array > 0)
            )
            if bool(np.any(candidate_mask)):
                return True
        return False


__all__ = [
    "PortfolioContextMixin",
    "PortfolioMarketDataAdapter",
    "SortedDateIndex",
    "_CONTEXT_PRELOAD_CHUNK_SIZE",
    "_StockSeries",
]
