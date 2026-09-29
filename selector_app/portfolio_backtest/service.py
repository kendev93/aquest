"""Dynamic ranked-slot portfolio backtesting service facade."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from typing import cast

import numpy as np

from selector_app.concurrency import MAX_WORKERS
from selector_app.formulas.custom import ParsedFormula, parse_formula
from selector_app.formulas.registry import FORMULA_REGISTRY, FormulaRegistry
from selector_app.indicators.cache import IndicatorCache
from selector_app.market_data.adapter import DuckDbMarketDataAdapter
from selector_app.market_data.models import StockRef
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.strategy_fitness.rolling import RollingFitnessFilter, RollingFitnessHistory

from .checkpoint import SimulationCheckpoint
from .context import (
    _CONTEXT_PRELOAD_CHUNK_SIZE,
    PortfolioContextMixin,
    PortfolioMarketDataAdapter,
    _StockSeries,
)
from .models import PortfolioBacktestConfig, PortfolioBacktestReport, PortfolioJsonValue
from .report import PortfolioReportMixin, _portfolio_fingerprint
from .risk import _atr_series
from .simulation import PortfolioSimulationMixin

PortfolioBacktestProgressCallback = Callable[[int, int], None]


class PortfolioBacktestService(
    PortfolioContextMixin,
    PortfolioSimulationMixin,
    PortfolioReportMixin,
):
    """Coordinate context preparation, simulation, and report assembly."""

    def __init__(
        self,
        adapter: PortfolioMarketDataAdapter | None = None,
        registry: FormulaRegistry | None = None,
        indicator_cache: IndicatorCache | None = None,
    ) -> None:
        self._adapter = adapter or DuckDbMarketDataAdapter(DuckDbMarketDataStore())
        self._registry = registry or FORMULA_REGISTRY
        self._indicator_cache = indicator_cache
        self._risk_ranked_context_source: Sequence[_StockSeries] | None = None
        self._risk_ranked_context_key: tuple[str, str] | None = None
        self._risk_ranked_candidate_index: dict[int, np.ndarray] | None = None

    def run(
        self,
        config: PortfolioBacktestConfig,
        progress_callback: PortfolioBacktestProgressCallback | None = None,
    ) -> PortfolioBacktestReport:
        resolved = self._list_refs(config)
        refs = resolved.refs
        parsed = parse_formula(config.formula_text) if config.formula_text else None
        self._validate_outputs(config, parsed)

        contexts: list[_StockSeries] = []
        failure_reasons: Counter[str] = Counter()
        # 一次只装一批。曾经是整批读进来再慢慢算：全市场 5000 只 × 全历史常驻在内存里
        # 是十几 GB，回测会把整个后端进程一起带下水（容器直接被 VM 回收，前端只看到
        # 一句含糊的报错）。分批后峰值只跟批次大小有关，算完的批次可以立刻丢掉。
        batched = self._supports_batch_frames()
        parallel_contexts = batched and len(refs) > 1
        executor = (
            ThreadPoolExecutor(
                max_workers=min(MAX_WORKERS, len(refs)),
                thread_name_prefix="portfolio-context",
            )
            if parallel_contexts
            else None
        )
        try:
            for offset in range(0, len(refs), _CONTEXT_PRELOAD_CHUNK_SIZE):
                chunk = refs[offset : offset + _CONTEXT_PRELOAD_CHUNK_SIZE]
                frames = self._batch_frames(chunk, config) if batched else None
                self._preload_indicator_cache(
                    {
                        (ref.market, ref.code): frames[(ref.market, ref.code)]
                        for ref in chunk
                        if frames is not None and (ref.market, ref.code) in frames
                    },
                    config,
                    parsed,
                )

                def build(ref: StockRef) -> _StockSeries | None:
                    return self._build_context(
                        ref,
                        config,
                        parsed,
                        frame=frames.get((ref.market, ref.code)) if frames is not None else None,
                    )

                if executor is None:
                    for current, ref in enumerate(chunk, start=offset + 1):
                        try:
                            context = build(ref)
                        except Exception as exc:  # noqa: BLE001 - one bad stock must not abort a portfolio
                            reason = str(exc).strip()[:200] or type(exc).__name__
                            failure_reasons[reason] += 1
                            context = None
                        if context is not None:
                            contexts.append(context)
                        if progress_callback is not None:
                            progress_callback(current, len(refs))
                    continue

                futures = {executor.submit(build, ref): ref for ref in chunk}
                completed = offset
                for future in as_completed(futures):
                    ref = futures[future]
                    try:
                        context = future.result()
                    except Exception as exc:  # noqa: BLE001 - one bad stock must not abort a portfolio
                        reason = str(exc).strip()[:200] or type(exc).__name__
                        failure_reasons[reason] += 1
                        context = None
                    if context is not None:
                        contexts.append(context)
                    completed += 1
                    if progress_callback is not None:
                        progress_callback(completed, len(refs))
        finally:
            if executor is not None:
                executor.shutdown(wait=True)

        if not contexts:
            return self._empty_report(config, len(refs), failure_reasons, resolved)
        dates = self._global_dates(contexts)
        if not dates:
            return self._empty_report(config, len(refs), failure_reasons, resolved)

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
            resolved,
            contexts,
        )
        if progress_callback is not None:
            progress_callback(len(refs), len(refs))
        return report

    def run_prepared_contexts(
        self,
        config: PortfolioBacktestConfig,
        contexts: Sequence[_StockSeries],
        *,
        total_candidates: int | None = None,
        include_details: bool = True,
        checkpoint: SimulationCheckpoint | None = None,
        checkpoint_callback: Callable[[SimulationCheckpoint], None] | None = None,
        precompute_ranked_candidates: bool = False,
    ) -> PortfolioBacktestReport:
        return cast(
            PortfolioBacktestReport,
            self._run_prepared_contexts(
                config,
                contexts,
                total_candidates=total_candidates,
                include_details=include_details,
                checkpoint=checkpoint,
                checkpoint_callback=checkpoint_callback,
                precompute_ranked_candidates=precompute_ranked_candidates,
                metrics_only=False,
            ),
        )

    def run_prepared_metrics(
        self,
        config: PortfolioBacktestConfig,
        contexts: Sequence[_StockSeries],
        *,
        total_candidates: int | None = None,
        checkpoint: SimulationCheckpoint | None = None,
        checkpoint_callback: Callable[[SimulationCheckpoint], None] | None = None,
        precompute_ranked_candidates: bool = False,
    ) -> dict[str, object]:
        """Run an optimizer candidate and return only metrics and replay identity."""

        return cast(
            dict[str, object],
            self._run_prepared_contexts(
                config,
                contexts,
                total_candidates=total_candidates,
                include_details=False,
                checkpoint=checkpoint,
                checkpoint_callback=checkpoint_callback,
                precompute_ranked_candidates=precompute_ranked_candidates,
                metrics_only=True,
            ),
        )

    def _run_prepared_contexts(
        self,
        config: PortfolioBacktestConfig,
        contexts: Sequence[_StockSeries],
        *,
        total_candidates: int | None = None,
        include_details: bool = True,
        checkpoint: SimulationCheckpoint | None = None,
        checkpoint_callback: Callable[[SimulationCheckpoint], None] | None = None,
        precompute_ranked_candidates: bool = False,
        metrics_only: bool,
    ) -> PortfolioBacktestReport | dict[str, object]:
        """Run the simulator on candidate-specific contexts prepared upstream.

        Strategy discovery uses a read-only shared market snapshot to avoid
        loading and calculating the same full universe for every candidate.
        The contexts still flow through this service's canonical simulator and
        report builder, so ordinary backtest semantics remain the source of
        truth.
        """

        if config.formula_text:
            raise ValueError("共享行情快照不支持自定义公式")
        self._validate_outputs(config, None)
        if contexts is not self._risk_ranked_context_source:
            self._risk_ranked_context_source = None
            self._risk_ranked_context_key = None
            self._risk_ranked_candidate_index = None
        prepared = list(contexts)
        candidate_total = max(len(prepared), total_candidates or len(prepared))
        if not prepared:
            report = self._empty_report(
                config,
                candidate_total,
                {"共享行情快照没有可用标的": candidate_total},
            )
            return self._search_summary_from_report(report) if metrics_only else report
        dates = self._global_dates(prepared)
        if not dates:
            report = self._empty_report(
                config,
                candidate_total,
                {"共享行情快照没有可用日期": candidate_total},
            )
            return self._search_summary_from_report(report) if metrics_only else report
        data_fingerprint = _portfolio_fingerprint(prepared)
        if not self._has_preflight_candidates(prepared, config):
            simulation = self._no_trade_simulation(dates, config, include_details=include_details)
            if metrics_only:
                return self._build_search_summary(config, simulation, data_fingerprint)
            return self._build_report(
                config,
                dates,
                candidate_total,
                len(prepared),
                candidate_total - len(prepared),
                {},
                simulation,
                data_fingerprint,
                None,
                prepared,
            )
        ranked_candidate_index: dict[int, np.ndarray] | None = None
        if precompute_ranked_candidates and not include_details and config.fitness_rank_weight == 0:
            context_key = (config.ranking_value, config.rank_order)
            if (
                contexts is self._risk_ranked_context_source
                and context_key == self._risk_ranked_context_key
                and self._risk_ranked_candidate_index is not None
            ):
                ranked_candidate_index = self._risk_ranked_candidate_index
            else:
                self._risk_ranked_context_source = contexts
                self._risk_ranked_context_key = context_key
                self._risk_ranked_candidate_index = self._build_ranked_candidate_index(
                    prepared,
                    dates,
                    config,
                )
                ranked_candidate_index = self._risk_ranked_candidate_index
        else:
            self._risk_ranked_context_source = None
            self._risk_ranked_context_key = None
            self._risk_ranked_candidate_index = None
        fitness_filter = self._build_fitness_filter(prepared, config)
        simulation = self._simulate(
            prepared,
            dates,
            config,
            fitness_filter,
            include_details=include_details,
            checkpoint=checkpoint,
            data_fingerprint=data_fingerprint,
            ranked_candidate_index=ranked_candidate_index,
        )
        restored_checkpoint = simulation.get("checkpoint")
        if checkpoint_callback is not None and isinstance(
            restored_checkpoint, SimulationCheckpoint
        ):
            checkpoint_callback(restored_checkpoint)
        if metrics_only:
            return self._build_search_summary(config, simulation, data_fingerprint)
        return self._build_report(
            config,
            dates,
            candidate_total,
            len(prepared),
            candidate_total - len(prepared),
            {},
            simulation,
            data_fingerprint,
            None,
            prepared,
        )

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
            None,
            [context],
        )

    def _build_fitness_filter(
        self,
        contexts: list[_StockSeries],
        config: PortfolioBacktestConfig,
    ) -> RollingFitnessFilter | None:
        if config.fitness_rank_weight <= 0:
            return None
        history_config = replace(config, fitness_rank_weight=0.0, max_positions=1)
        histories: dict[str, RollingFitnessHistory] = {}
        for context in contexts:
            simulation = self._simulate(
                [context],
                [int(value) for value in context.dates],
                history_config,
                None,
                include_details=False,
            )
            histories[context.symbol] = RollingFitnessHistory.from_records(
                trades=cast(list[dict[str, PortfolioJsonValue]], simulation["trades"]),
                equity=cast(list[dict[str, PortfolioJsonValue]], simulation["equity"]),
            )
        return RollingFitnessFilter(
            histories,
            min_trades=config.fitness_min_trades,
            max_drawdown=config.fitness_max_drawdown,
        )


__all__ = [
    "PortfolioBacktestProgressCallback",
    "PortfolioBacktestService",
    "PortfolioMarketDataAdapter",
    "SimulationCheckpoint",
    "_StockSeries",
    "_atr_series",
]
