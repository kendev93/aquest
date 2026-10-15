"""Market-data and indicator context preparation for portfolio backtests."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from threading import RLock
from typing import NamedTuple, cast

import numpy as np
import pandas as pd

from aquest.formulas.common import validate_market_data
from aquest.formulas.custom import (
    ParsedFormula,
    custom_output_id,
    evaluate_custom_formula,
)
from aquest.formulas.registry import FORMULA_REGISTRY
from aquest.formulas.types import FormulaResult
from aquest.indicators.cache import frame_fingerprint
from aquest.market_data.adapter import parse_universe_code
from aquest.market_data.adjustment import CorporateAction, mark_adjusted
from aquest.market_data.day_format import classify_board, classify_instrument
from aquest.market_data.models import InstrumentBoard, InstrumentType, MarketCode, StockRef
from aquest.market_data.risk_warning import is_risk_warning_symbol
from aquest.market_data.scope import InstrumentScope
from aquest.screening.engine import evaluate_rule_group
from aquest.screening.rules import RuleGroup

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


@dataclass(frozen=True)
class _ContextBase:
    """规则无关的行情底座：一次构建、同股同窗口的逐候选评估复用。

    买卖规则对初筛（每股约 450 次 ``run_stock``）里，frame 预处理、复权、
    全帧指纹、窗口切片、ATR、日期换算都只取决于 (标的, 窗口, ATR 周期,
    公司行动)，与具体规则无关——逐候选重做是单股最大的确定性浪费。
    指标 ``results`` 不进底座：它的公式集合随规则变化，且 IndicatorCache
    已经按公式粒度复用。
    """

    frame: pd.DataFrame
    metric_frame: pd.DataFrame
    data_fingerprint: str
    indices: np.ndarray
    opens: np.ndarray
    highs: np.ndarray
    lows: np.ndarray
    closes: np.ndarray
    atrs: np.ndarray
    metric_closes: np.ndarray
    dates: np.ndarray


#: 底座缓存的条目上限。同股规则对初筛只有训练/验证两个窗口，2 即可全热；
#: 留 4 是给「同进程顺序研究多只」的场景留一层缓冲，代价是几份行情帧。
_CONTEXT_BASE_CACHE_SIZE = 4
_CONTEXT_BASE_CACHE_LOCK = RLock()


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
    buy_rule: RuleGroup | None = None
    sell_rule: RuleGroup | None = None
    strategy_config: Mapping[str, object] = field(default_factory=dict)
    priority_score: float | None = None
    name: str | None = None
    #: 复权后的收盘价，长度与 :attr:`closes` 相同，仅供买入持有基准使用。
    #:
    #: 为什么单列一个字段而不是就地改 :attr:`closes`：``closes`` 是成交价、整手
    #: 取整和 ATR 的输入，必须保持不复权（见 :meth:`_build_context` 里两口径的
    #: 分工说明）。而基准回答的是"这批股票一直拿着会怎样"——持有者会收到分红，
    #: 漏掉它们会让基准被系统性低估：浦发银行 2015~2026 这 11 年有 26 次派息，
    #: 不复权口径是 -41.01%，按持有者现金流算是 +31.15%。用这个数当基准，跑输
    #: 的策略会显示成跑赢。
    #:
    #: 缺省 None 是给不传该字段的构造点留退路，调用方一律用
    #: :attr:`closes` 兜底，基准不会因为拿不到复权价就算不出来。
    adjusted_closes: np.ndarray | None = None

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
        rules = self._all_rules(config)
        signal_ids = list(self._rule_signal_ids(*rules))
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
        rules = [config.buy_rule, config.sell_rule]
        rules.extend(
            cast(RuleGroup | None, strategy.get("buy_rule"))
            for strategy in config.symbol_strategies.values()
        )
        rules.extend(
            cast(RuleGroup | None, strategy.get("sell_rule"))
            for strategy in config.symbol_strategies.values()
        )
        for rule in rules:
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
            for signal_id in self._rule_signal_ids(*self._all_rules(config))
        }
        formula_ids.update(value_id.split(".", 1)[0] for value_id in self._value_ids(config))
        return tuple(sorted(formula_ids))

    @staticmethod
    def _all_rules(config: PortfolioBacktestConfig) -> tuple[RuleGroup | None, ...]:
        rules: list[RuleGroup | None] = [config.buy_rule, config.sell_rule]
        for strategy in config.symbol_strategies.values():
            rules.append(cast(RuleGroup | None, strategy.get("buy_rule")))
            rules.append(cast(RuleGroup | None, strategy.get("sell_rule")))
        return tuple(rules)

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
        # 标的覆盖必须在建底座之前解析：ATR 序列的周期是标的策略自己的参数，
        # 底座一旦按全局周期算完就来不及改了（此前正是这里把 per-symbol atr_period
        # 静默吃掉）。
        strategy = config.symbol_strategies.get(ref.market + ref.code)
        if strategy is None:
            strategy = config.symbol_strategies.get(f"{ref.market}:{ref.code}", {})
        base = self._context_base(ref, config, frame, self._effective_atr_period(config, strategy))
        results = self._calculate_results(
            base.metric_frame,
            config,
            parsed,
            market=ref.market,
            code=ref.code,
        )
        minimum_bars = max(result.minimum_bars for result in results.values())
        if len(base.frame) < minimum_bars:
            raise ValueError(f"数据不足，至少需要 {minimum_bars} 根 K 线")
        indices = base.indices
        if len(indices) == 0:
            raise ValueError("日期范围内没有可用日线")

        buy_rule = strategy.get("buy_rule") if isinstance(strategy, Mapping) else None
        sell_rule = strategy.get("sell_rule") if isinstance(strategy, Mapping) else None
        selected_buy_rule = buy_rule if isinstance(buy_rule, RuleGroup) else config.buy_rule
        selected_sell_rule = sell_rule if isinstance(sell_rule, RuleGroup) else config.sell_rule
        if selected_buy_rule is None:
            raise ValueError(f"{ref.market}{ref.code} 没有买入规则")
        signal_arrays = {
            signal_id: self._signal_array(results, signal_id, parsed is not None)[indices].copy()
            for signal_id in self._rule_signal_ids(*self._all_rules(config))
        }
        value_arrays = {
            value_id: self._value_array(results, value_id, parsed is not None)[indices].copy()
            for value_id in self._value_ids(config)
        }
        if config.single_security_mode:
            value_arrays[SINGLE_SECURITY_RANKING_VALUE] = np.zeros(len(indices), dtype=float)
        buy_mask = evaluate_rule_group(
            selected_buy_rule, results, self._registry, custom=parsed is not None
        )[indices].copy()
        sell_mask = (
            evaluate_rule_group(
                selected_sell_rule, results, self._registry, custom=parsed is not None
            )[indices].copy()
            if selected_sell_rule is not None
            else np.zeros(len(indices), dtype=bool)
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
            dates=base.dates,
            opens=base.opens,
            highs=base.highs,
            lows=base.lows,
            closes=base.closes,
            atrs=base.atrs,
            buy_mask=np.asarray(buy_mask, dtype=bool),
            sell_mask=np.asarray(sell_mask, dtype=bool),
            signals=signal_arrays,
            values=value_arrays,
            date_to_index=SortedDateIndex(base.dates),
            data_fingerprint=base.data_fingerprint,
            buy_rule=selected_buy_rule,
            sell_rule=selected_sell_rule,
            strategy_config=strategy if isinstance(strategy, Mapping) else {},
            priority_score=(
                float(config.symbol_priority_scores[ref.market + ref.code])
                if ref.market + ref.code in config.symbol_priority_scores
                else None
            ),
            adjusted_closes=base.metric_closes,
        )

    @staticmethod
    def _effective_atr_period(config: PortfolioBacktestConfig, strategy: object) -> int:
        """标的策略声明的 ATR 周期优先，缺失或非法时回落到全局配置。

        风控线的 ATR 是策略参数（每标的寻优时各带一个），不是目录里那个固定 14 的
        ``atr_pct`` 指标；两者只在周期都为 14 时数值相同。
        """

        if isinstance(strategy, Mapping):
            value = strategy.get("atr_period")
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return value
        return int(config.atr_period)

    def _context_base(
        self,
        ref: StockRef,
        config: PortfolioBacktestConfig,
        frame: pd.DataFrame | None,
        atr_period: int,
    ) -> _ContextBase:
        """取规则无关底座；同股同窗口同 ATR 周期命中缓存，公司行动变化时自然重建。

        缓存键里带公司行动的 repr：``run_stock`` 每次顶层调用都会刷新行动缓存并
        重新查询（「每次运行都能看到最新除权价」是既有契约），行动变了键就变，
        底座随之重建——脏数据进不了缓存，缓存因此不需要主动失效。行情帧本身在
        适配器线程缓存里本来就是整个研究期间一份，底座复用不引入新的失效面。
        """

        actions = self._corporate_actions(ref)
        key = (
            ref.market,
            ref.code,
            config.start_date,
            config.end_date,
            atr_period,
            repr(actions),
        )
        use_cache = frame is None
        cache: OrderedDict[tuple[object, ...], _ContextBase] | None = None
        if use_cache:
            with _CONTEXT_BASE_CACHE_LOCK:
                cache = getattr(self, "_context_base_cache", None)
                if cache is None:
                    cache = OrderedDict()
                    self._context_base_cache = cache
                cached: _ContextBase | None = cache.get(key)
                if cached is not None:
                    cache.move_to_end(key)
                    return cached
        base = self._build_context_base(ref, config, frame, actions, atr_period)
        if use_cache and cache is not None:
            with _CONTEXT_BASE_CACHE_LOCK:
                cache[key] = base
                while len(cache) > _CONTEXT_BASE_CACHE_SIZE:
                    cache.popitem(last=False)
        return base

    def _build_context_base(
        self,
        ref: StockRef,
        config: PortfolioBacktestConfig,
        frame: pd.DataFrame | None,
        actions: tuple[CorporateAction, ...],
        atr_period: int,
    ) -> _ContextBase:
        """构建规则无关底座。批量路径传入的窗口帧（带 warmup）不进缓存。"""

        prepared = self._prepare_frame(
            frame if frame is not None else self._adapter.read_stock(ref)
        )
        if prepared.empty:
            raise ValueError("数据为空")
        # 同一份行情在这里分成两个口径，各司其职：
        #
        # * ``metric_frame`` —— 指标、选股条件、排序值。除权日的 raw 跳空是价格
        #   序列里的一个假断崖（浦发银行 2017-05-25 一天 -24%），留着它算出来的
        #   ATR、动量、均线全是假的。
        # * ``frame`` —— 成交价、整手取整、ATR。ATR 的单位是"元"，会和成交价直接
        #   加减运算（``risk._risk_level``），一旦用复权价算，所有 ATR 止损/止盈
        #   的阈值会完全错乱；除权当天开盘大幅低开是真实市场现象，ATR 反映它是
        #   合理的。成交价更不能碰复权价：后复权价会随分红累积无限变大，股数和
        #   整手全都不再对应现实。
        metric_frame = mark_adjusted(prepared, actions) if actions else prepared
        data_fingerprint = frame_fingerprint(metric_frame)
        indices = self._window_indices(prepared, config.start_date, config.end_date)
        window = prepared.iloc[indices].reset_index(drop=True)
        metric_closes = metric_frame["close"].to_numpy(dtype=float)[indices].copy()
        dates = np.asarray(
            [int(pd.Timestamp(value).strftime("%Y%m%d")) for value in window["date"]],
            dtype=np.int64,
        )
        return _ContextBase(
            frame=prepared,
            metric_frame=metric_frame,
            data_fingerprint=data_fingerprint,
            indices=indices,
            opens=window["open"].to_numpy(dtype=float, copy=True),
            highs=window["high"].to_numpy(dtype=float, copy=True),
            lows=window["low"].to_numpy(dtype=float, copy=True),
            closes=window["close"].to_numpy(dtype=float, copy=True),
            atrs=_atr_series(prepared, atr_period)[indices].copy(),
            metric_closes=metric_closes,
            dates=dates,
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
        # 事件跟着行情一起批量取。逐标的查会在全市场 5000+ 只的规模下变成 5000 次
        # 查询；指标复权要用它们，成交与 ATR 仍然走 raw。
        self._preload_corporate_actions(frames.keys())
        return frames

    def _preload_corporate_actions(self, keys: Iterable[tuple[str, str]]) -> None:
        loader = getattr(self._adapter, "load_corporate_actions", None)
        if not callable(loader):
            return
        key_list = list(keys)
        cache = getattr(self, "_corporate_action_cache", None)
        if cache is None:
            cache = {}
        loaded = loader(key_list)
        cache.update({key: loaded.get(key, ()) for key in key_list})
        self._corporate_action_cache = cache

    def _refresh_corporate_action_cache(self) -> None:
        """Start a top-level replay with fresh action rows and reuse them within that run."""

        self._corporate_action_cache = {}

    def _corporate_actions(self, ref: StockRef) -> tuple[CorporateAction, ...]:
        """该标的的分红送转事件；适配器不支持时退化成"无事件"。

        非批量适配器（测试里的 FakeAdapter 之类）走不到 preload，这里按需单查并
        累积进缓存，避免同一标的被重复查。
        """

        cache: dict[tuple[str, str], tuple[CorporateAction, ...]] | None = getattr(
            self, "_corporate_action_cache", None
        )
        if cache is None:
            cache = {}
            self._corporate_action_cache = cache
        key = (ref.market, ref.code)
        if key not in cache:
            reader = getattr(self._adapter, "read_corporate_actions", None)
            cache[key] = tuple(reader(ref)) if callable(reader) else ()
        return cache[key]

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
