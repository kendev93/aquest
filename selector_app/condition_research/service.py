"""Mine and replay buy/sell condition candidates across independent stocks."""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import as_completed
from concurrent.futures.process import BrokenProcessPool
from datetime import date
from pathlib import Path
from typing import Protocol, TypeVar, cast

import pandas as pd

from selector_app.backtest.models import BacktestConfig, BacktestReport
from selector_app.backtest.profiles import profile_from_overrides
from selector_app.backtest.service import (
    BacktestMarketDataAdapter,
    BacktestService,
    PreparedBacktest,
)
from selector_app.formulas.custom import custom_output_id, parse_formula
from selector_app.formulas.registry import FORMULA_REGISTRY, FormulaRegistry
from selector_app.market_data.adapter import DuckDbMarketDataAdapter
from selector_app.market_data.models import InstrumentBoard, InstrumentType, StockRef
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.screening.jobs import TaskCancelledError, TaskUserError, report_progress_detail
from selector_app.screening.models import ScreenCondition
from selector_app.screening.rules import RuleGroup
from selector_app.strategy_discovery.catalog import default_indicator_catalog

from . import parallel
from .metrics import summarize_backtests
from .mining import MinedCondition, equivalent_condition_signatures, mine_binary_conditions
from .models import ConditionResearchConfig
from .parallel import (
    CandidateWindowOutcome,
    CandidateWindowPayload,
    HoldoutOutcome,
    HoldoutPayload,
    TrainOutcome,
    TrainPayload,
)
from .resources import (
    ResearchResourceBudgetError,
    ResearchResourceEstimate,
    ResearchWorkerPlan,
    estimate_research_resources,
    plan_research_workers,
)
from .samples import (
    MiningSamples,
    build_buy_trade_samples,
    build_early_exit_samples,
    feature_rows_by_date,
)
from .time_split import ResearchSplits, split_calendar_range

ProgressCallback = Callable[[int, int], None]
CancelCheck = Callable[[], bool]


class _KeyedOutcome(Protocol):
    @property
    def key(self) -> str: ...


OutcomeT = TypeVar("OutcomeT", bound=_KeyedOutcome)


class ResearchMarketDataAdapter(Protocol):
    def list_stock_refs(
        self,
        vipdoc_path: str | Path,
        universe: str,
        universe_file: str | Path | None = None,
        instrument_types: tuple[InstrumentType, ...] | None = None,
        boards: tuple[InstrumentBoard, ...] | None = None,
    ) -> list[StockRef]: ...


def _backtest_config(config: ConditionResearchConfig, ref: StockRef) -> BacktestConfig:
    instrument_type: InstrumentType = getattr(ref, "instrument_type", "stock")
    profile = profile_from_overrides(
        instrument_type,
        commission=config.commission,
        min_commission=config.min_commission,
        stamp_tax=config.stamp_tax,
    )
    return BacktestConfig(
        market=ref.market,
        code=ref.code,
        vipdoc_path=config.vipdoc_path,
        buy_rule=config.buy_rule,
        sell_rule=config.sell_rule,
        formula_text=config.formula_text,
        formula_parameters=config.formula_parameters,
        start_date=config.start_date,
        end_date=config.end_date,
        initial_cash=config.initial_cash,
        commission=profile.commission,
        min_commission=profile.min_commission,
        stamp_tax=profile.stamp_tax,
        slippage=config.slippage,
        execution=config.execution,
        position_mode=config.position_mode,
        fixed_size=config.fixed_size,
        instrument_type=instrument_type,
        board=ref.board,
    )


def _prepare_stock(
    service: BacktestService,
    config: ConditionResearchConfig,
    ref: StockRef,
    feature_ids: tuple[str, ...],
) -> PreparedBacktest:
    return service.prepare_for_ref(_backtest_config(config, ref), ref, feature_ids=feature_ids)


def _window_report(
    service: BacktestService,
    prepared: PreparedBacktest,
    start_date: date,
    end_date: date,
) -> BacktestReport:
    return service.run_prepared(
        prepared,
        start_date=start_date,
        end_date=end_date,
        summary_only=True,
    )


def _candidate_report(
    service: BacktestService,
    prepared: PreparedBacktest,
    condition: ScreenCondition,
    side: str,
    start_date: date,
    end_date: date,
) -> BacktestReport:
    return service.run_prepared(
        prepared,
        buy_filter=condition if side == "buy" else None,
        sell_trigger=condition if side == "sell" else None,
        start_date=start_date,
        end_date=end_date,
        summary_only=True,
    )


def _full_candidate_report(
    service: BacktestService,
    prepared: PreparedBacktest,
    condition: ScreenCondition,
    side: str,
) -> BacktestReport:
    return service.run_prepared(
        prepared,
        buy_filter=condition if side == "buy" else None,
        sell_trigger=condition if side == "sell" else None,
        summary_only=True,
    )


def _verify_fingerprint(
    prepared: PreparedBacktest,
    ref: StockRef,
    fingerprints: Mapping[str, str],
) -> None:
    previous = fingerprints.get(_symbol_key(ref))
    if previous is None:
        raise ValueError("该股票在寻优期没有有效行情，跳过后续复验")
    if previous != prepared.data_fingerprint:
        raise TaskUserError("行情数据在研究期间发生变化，请重新运行以保证各阶段一致")


def _train_task(ref: StockRef) -> TrainOutcome:
    """Worker entry point: training-window baseline plus mining samples."""

    payload = cast(TrainPayload, parallel.worker_payload())
    service = parallel.worker_service()
    key = _symbol_key(ref)
    try:
        prepared = _prepare_stock(service, payload.config, ref, payload.feature_ids)
        baseline = _window_report(service, prepared, payload.train_start, payload.train_end)
        full_period = service.run_prepared(prepared, summary_only=True)
        rows = feature_rows_by_date(prepared.feature_values)
        buy_samples = None
        sell_samples = None
        if payload.config.search_sides in {"buy", "both"}:
            buy_samples = build_buy_trade_samples(
                baseline.trades,
                prepared.feature_values,
                market=ref.market,
                code=ref.code,
                feature_rows=rows,
            )
        if payload.config.search_sides in {"sell", "both"}:
            sell_samples = build_early_exit_samples(
                baseline.trades,
                prepared.feature_values,
                prepared.frame,
                prepared.config,
                market=ref.market,
                code=ref.code,
                feature_rows=rows,
            )
    except (ValueError, OSError) as exc:
        return TrainOutcome(key=key, failure=_reason(exc))
    return TrainOutcome(
        key=key,
        fingerprint=prepared.data_fingerprint,
        train_report=_compact_report(baseline),
        full_report=_compact_report(full_period),
        buy_samples=buy_samples,
        sell_samples=sell_samples,
    )


def _candidate_window_task(ref: StockRef) -> CandidateWindowOutcome:
    """Worker entry point: one window's baseline plus every candidate replay."""

    payload = cast(CandidateWindowPayload, parallel.worker_payload())
    service = parallel.worker_service()
    key = _symbol_key(ref)
    try:
        prepared = _prepare_stock(service, payload.config, ref, payload.feature_ids)
        _verify_fingerprint(prepared, ref, payload.fingerprints)
        baseline = _compact_report(
            _window_report(service, prepared, payload.start_date, payload.end_date)
        )
        reports = {
            candidate_id: _compact_report(
                _candidate_report(
                    service,
                    prepared,
                    condition,
                    side,
                    payload.start_date,
                    payload.end_date,
                )
            )
            for candidate_id, side, condition in payload.candidates
        }
    except (ValueError, OSError) as exc:
        return CandidateWindowOutcome(key=key, failure=_reason(exc))
    return CandidateWindowOutcome(
        key=key,
        baseline=baseline,
        candidates=parallel.phase_candidates(payload.candidates, reports),
    )


def _holdout_task(ref: StockRef) -> HoldoutOutcome:
    """Worker entry point: holdout replay and the candidates' full-period replay."""

    payload = cast(HoldoutPayload, parallel.worker_payload())
    service = parallel.worker_service()
    key = _symbol_key(ref)
    try:
        prepared = _prepare_stock(service, payload.config, ref, payload.feature_ids)
        _verify_fingerprint(prepared, ref, payload.fingerprints)
        baseline = _compact_report(
            _window_report(service, prepared, payload.start_date, payload.end_date)
        )
        holdout = {
            candidate_id: _compact_report(
                _candidate_report(
                    service,
                    prepared,
                    condition,
                    side,
                    payload.start_date,
                    payload.end_date,
                )
            )
            for candidate_id, side, condition in payload.candidates
        }
        full_period = {
            candidate_id: _compact_report(
                _full_candidate_report(service, prepared, condition, side)
            )
            for candidate_id, side, condition in payload.candidates
        }
    except (ValueError, OSError) as exc:
        return HoldoutOutcome(key=key, failure=_reason(exc))
    return HoldoutOutcome(
        key=key,
        baseline=baseline,
        holdout=parallel.phase_candidates(payload.candidates, holdout),
        full_period=parallel.phase_candidates(payload.candidates, full_period),
    )


def _candidates_per_stock(config: ConditionResearchConfig) -> int:
    """Compact reports the parent keeps per stock: both sides plus holdout replays."""

    return 2 * config.candidate_limit + 4 * config.holdout_candidates


def _estimated_bars_per_stock(config: ConditionResearchConfig) -> int:
    """Rough trading-bar count for the requested window (calendar days x 5/7)."""

    window_days = (config.end_date - config.start_date).days + 1
    return max(1, int(window_days * 5 / 7))


class ConditionResearchService:
    """Run fixed base rules, mine outcome-contrast rules, then replay candidates."""

    def __init__(
        self,
        *,
        adapter: ResearchMarketDataAdapter | None = None,
        store: DuckDbMarketDataStore | None = None,
        backtest_service: BacktestService | None = None,
        registry: FormulaRegistry | None = None,
        workers: int | None = None,
        memory_budget_bytes: int | None = None,
    ) -> None:
        selected_store = store
        if adapter is None and selected_store is None:
            selected_store = DuckDbMarketDataStore()
        self._store = selected_store
        self._adapter = (
            adapter
            if adapter is not None
            else DuckDbMarketDataAdapter(cast(DuckDbMarketDataStore, selected_store))
        )
        self._registry = registry or FORMULA_REGISTRY
        self._backtest_service = backtest_service or BacktestService(
            adapter=cast(BacktestMarketDataAdapter, self._adapter),
            registry=self._registry,
        )
        self._memory_budget_bytes = memory_budget_bytes
        self._worker_plan: ResearchWorkerPlan = plan_research_workers(
            workers, memory_budget_bytes=memory_budget_bytes
        )
        # Workers rebuild the pipeline from a database path and always use the
        # default registry, so anything else stays on the in-process path.
        self._parallel_target = (
            parallel.adapter_worker_target(self._adapter)
            if self._registry is FORMULA_REGISTRY
            else None
        )

    def run(
        self,
        config: ConditionResearchConfig,
        progress_callback: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> dict[str, object]:
        refs = self._list_stock_refs(config)
        if not refs:
            raise ValueError("所选市场没有可用于研究的本地股票行情")
        splits = split_calendar_range(
            config.start_date,
            config.end_date,
            train_ratio=config.train_ratio,
            validation_ratio=config.validation_ratio,
        )
        feature_ids = self._feature_ids(config)
        plan = self._plan_for(refs, config)
        work_total = max(1, len(refs) * 3)
        self._progress(progress_callback, 0, work_total, "准备训练样本", 0, len(refs))

        train_reports: list[Mapping[str, object]] = []
        full_period_reports: list[Mapping[str, object]] = []
        buy_samples: list[MiningSamples] = []
        sell_samples: list[MiningSamples] = []
        failures: Counter[str] = Counter()
        processed: set[str] = set()
        fingerprints: dict[str, str] = {}

        train_payload = TrainPayload(
            config=config,
            feature_ids=feature_ids,
            train_start=splits.train.start_date,
            train_end=splits.train.end_date,
        )
        for train_outcome in self._run_phase(
            train_payload,
            refs,
            _train_task,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
            progress_offset=0,
            work_total=work_total,
            phase_label="挖掘盈利/亏损样本特征",
            plan=plan,
        ):
            if train_outcome.failure is not None:
                failures[train_outcome.failure] += 1
                continue
            train_reports.append(cast(Mapping[str, object], train_outcome.train_report))
            full_period_reports.append(cast(Mapping[str, object], train_outcome.full_report))
            fingerprints[train_outcome.key] = cast(str, train_outcome.fingerprint)
            processed.add(train_outcome.key)
            if train_outcome.buy_samples is not None:
                buy_samples.append(train_outcome.buy_samples)
            if train_outcome.sell_samples is not None:
                sell_samples.append(train_outcome.sell_samples)

        mined = self._mine_candidates(config, buy_samples, sell_samples)
        sample_totals = _sample_totals(buy_samples, sell_samples)
        baseline_train = summarize_backtests(train_reports)
        if not mined:
            validation_reports: list[Mapping[str, object]] = []
            holdout_reports: list[Mapping[str, object]] = []
            for validation_outcome in self._run_phase(
                CandidateWindowPayload(
                    config=config,
                    feature_ids=feature_ids,
                    start_date=splits.validation.start_date,
                    end_date=splits.validation.end_date,
                    fingerprints=dict(fingerprints),
                    candidates=(),
                ),
                refs,
                _candidate_window_task,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
                progress_offset=len(refs),
                work_total=work_total,
                phase_label="计算基础规则验证结果",
                plan=plan,
            ):
                if validation_outcome.failure is not None:
                    failures[validation_outcome.failure] += 1
                    continue
                validation_reports.append(cast(Mapping[str, object], validation_outcome.baseline))
            for holdout_outcome in self._run_phase(
                HoldoutPayload(
                    config=config,
                    feature_ids=feature_ids,
                    start_date=splits.holdout.start_date,
                    end_date=splits.holdout.end_date,
                    fingerprints=dict(fingerprints),
                    candidates=(),
                ),
                refs,
                _holdout_task,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
                progress_offset=len(refs) * 2,
                work_total=work_total,
                phase_label="计算基础规则留出结果",
                plan=plan,
            ):
                if holdout_outcome.failure is not None:
                    failures[holdout_outcome.failure] += 1
                    continue
                holdout_reports.append(cast(Mapping[str, object], holdout_outcome.baseline))
            empty_result = self._empty_result(
                config,
                splits,
                len(refs),
                processed,
                feature_ids,
                baseline_train,
                failures,
                fingerprints,
                sample_totals,
            )
            baseline_payload = cast(dict[str, object], empty_result["baseline"])
            baseline_payload["validation"] = _without_stock_rows(
                summarize_backtests(validation_reports)
            )
            baseline_payload["full_period"] = summarize_backtests(full_period_reports)
            baseline_payload["holdout"] = summarize_backtests(holdout_reports)
            return empty_result

        validation_candidates: dict[str, list[Mapping[str, object]]] = {
            candidate_id: [] for candidate_id, _, _ in mined
        }
        validation_baseline: list[Mapping[str, object]] = []
        candidate_specs = tuple(
            (candidate_id, side, candidate.condition) for candidate_id, side, candidate in mined
        )
        for validation_outcome in self._run_phase(
            CandidateWindowPayload(
                config=config,
                feature_ids=feature_ids,
                start_date=splits.validation.start_date,
                end_date=splits.validation.end_date,
                fingerprints=dict(fingerprints),
                candidates=candidate_specs,
            ),
            refs,
            _candidate_window_task,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
            progress_offset=len(refs),
            work_total=work_total,
            phase_label="验证候选条件",
            plan=plan,
        ):
            if validation_outcome.failure is not None:
                failures[validation_outcome.failure] += 1
                continue
            validation_baseline.append(cast(Mapping[str, object], validation_outcome.baseline))
            for candidate_id, report in validation_outcome.candidates:
                validation_candidates[candidate_id].append(report)

        validation_baseline_summary = summarize_backtests(validation_baseline)
        results: list[dict[str, object]] = []
        for candidate_id, side, candidate in mined:
            summary = summarize_backtests(
                validation_candidates[candidate_id],
                baseline_reports=validation_baseline,
            )
            reasons = candidate_constraint_failures(
                summary,
                validation_baseline_summary,
                min_trades=config.min_trades,
                min_instruments=config.min_instruments,
            )
            results.append(
                {
                    "candidate_id": candidate_id,
                    "side": side,
                    "condition": _condition_dict(candidate.condition),
                    "mining": _mining_dict(candidate),
                    "validation": _without_stock_rows(summary),
                    "passed_constraints": not reasons,
                    "constraint_failures": list(reasons),
                    "full_period": None,
                    "holdout": None,
                }
            )
        results.sort(
            key=lambda item: (
                not bool(item.get("passed_constraints")),
                -_summary_number(item, "win_rate"),
                -_summary_number(item, "improved_stock_ratio"),
                -_mining_number(item, "lift"),
            )
        )
        holdout_ids = _select_holdout_candidates(results, config.holdout_candidates)

        holdout_candidates: dict[str, list[Mapping[str, object]]] = {
            candidate_id: [] for candidate_id in holdout_ids
        }
        full_period_candidates: dict[str, list[Mapping[str, object]]] = {
            candidate_id: [] for candidate_id in holdout_ids
        }
        holdout_baseline: list[Mapping[str, object]] = []
        holdout_specs = tuple(spec for spec in candidate_specs if spec[0] in holdout_ids)
        for holdout_outcome in self._run_phase(
            HoldoutPayload(
                config=config,
                feature_ids=feature_ids,
                start_date=splits.holdout.start_date,
                end_date=splits.holdout.end_date,
                fingerprints=dict(fingerprints),
                candidates=holdout_specs,
            ),
            refs,
            _holdout_task,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
            progress_offset=len(refs) * 2,
            work_total=work_total,
            phase_label="复验最终留出期",
            plan=plan,
        ):
            if holdout_outcome.failure is not None:
                failures[holdout_outcome.failure] += 1
                continue
            holdout_baseline.append(cast(Mapping[str, object], holdout_outcome.baseline))
            for candidate_id, report in holdout_outcome.holdout:
                holdout_candidates[candidate_id].append(report)
            for candidate_id, report in holdout_outcome.full_period:
                full_period_candidates[candidate_id].append(report)

        for result in results:
            candidate_id = cast(str, result["candidate_id"])
            if candidate_id in holdout_ids:
                result["full_period"] = summarize_backtests(
                    full_period_candidates[candidate_id],
                    baseline_reports=full_period_reports,
                )
                result["holdout"] = summarize_backtests(
                    holdout_candidates[candidate_id],
                    baseline_reports=holdout_baseline,
                )

        return {
            "universe": config.universe,
            "start_date": config.start_date.isoformat(),
            "end_date": config.end_date.isoformat(),
            "search_sides": config.search_sides,
            "data_fingerprint": _combined_fingerprint(fingerprints),
            "feature_count": len(feature_ids),
            "min_trades": config.min_trades,
            "min_instruments": config.min_instruments,
            "sample_totals": sample_totals,
            "disclosure": _disclosure(config.universe, len(refs), len(processed)),
            "total_candidates": len(refs),
            "processed": len(processed),
            "skipped": max(0, len(refs) - len(processed)),
            "errors": sum(failures.values()),
            "failure_reasons": dict(failures),
            "splits": _splits_dict(splits),
            "baseline": {
                "train": _without_stock_rows(baseline_train),
                "validation": _without_stock_rows(validation_baseline_summary),
                "full_period": summarize_backtests(full_period_reports),
                "holdout": summarize_backtests(holdout_baseline),
            },
            "candidates": results,
            "algorithm": {
                "mining": "contrastive_threshold_and_pairwise_rules",
                "search": "single_condition_then_full_stock_replay",
                "holdout_candidates_per_side": config.holdout_candidates,
            },
        }

    def estimate_resources(self, config: ConditionResearchConfig) -> ResearchResourceEstimate:
        """Pre-flight memory estimate for one configuration, without running it."""

        refs = self._list_stock_refs(config)
        return estimate_research_resources(
            len(refs),
            candidates_per_stock=_candidates_per_stock(config),
            bars_per_stock=_estimated_bars_per_stock(config),
            requested_workers=self._worker_plan.requested_workers,
            memory_budget_bytes=self._memory_budget_bytes,
        )

    def _plan_for(
        self,
        refs: Sequence[StockRef],
        config: ConditionResearchConfig,
    ) -> ResearchResourceEstimate:
        """Data-aware worker plan; refuses to start when even one worker cannot fit."""

        estimate = estimate_research_resources(
            len(refs),
            candidates_per_stock=_candidates_per_stock(config),
            bars_per_stock=_estimated_bars_per_stock(config),
            requested_workers=self._worker_plan.requested_workers,
            memory_budget_bytes=self._memory_budget_bytes,
        )
        if not estimate.within_budget:
            raise ResearchResourceBudgetError(estimate)
        return estimate

    def _list_stock_refs(self, config: ConditionResearchConfig) -> list[StockRef]:
        wanted = config.instrument_types or ("stock",)
        try:
            refs = self._adapter.list_stock_refs(
                config.vipdoc_path,
                config.universe,
                instrument_types=wanted,
                boards=config.boards or None,
            )
        except TypeError as exc:
            if "unexpected keyword" not in str(exc):
                raise
            refs = self._adapter.list_stock_refs(config.vipdoc_path, config.universe)
        return [
            ref
            for ref in refs
            if getattr(ref, "instrument_type", "stock") in wanted
            and (not config.boards or getattr(ref, "board", None) in config.boards)
        ]

    def _feature_ids(self, config: ConditionResearchConfig) -> tuple[str, ...]:
        if config.formula_text:
            parsed = parse_formula(config.formula_text)
            price_scale = parsed.price_scale_names
            value_ids = tuple(
                custom_output_id(name) for name in parsed.value_names if name not in price_scale
            )
            if not value_ids:
                raise ValueError(
                    "自定义公式的数值输出都是原始价格/成交量量纲，"
                    "据此挖出的绝对阈值无法跨股票比较；"
                    "请改用比值类输出，例如 SCORE:C/MA(C,20);"
                )
            return value_ids
        return tuple(
            item.id for item in default_indicator_catalog(self._registry).values if item.rankable
        )

    def _run_phase(
        self,
        payload: object,
        refs: Sequence[StockRef],
        task: Callable[[StockRef], OutcomeT],
        *,
        progress_callback: ProgressCallback | None,
        cancel_check: CancelCheck | None,
        progress_offset: int,
        work_total: int,
        phase_label: str,
        plan: ResearchResourceEstimate,
    ) -> list[OutcomeT]:
        """Run one per-stock phase, in worker processes when parallel is enabled.

        Outcomes come back in universe order so summaries stay reproducible
        regardless of completion order.
        """

        order = {_symbol_key(ref): index for index, ref in enumerate(refs)}
        outcomes: list[OutcomeT] = []
        if self._parallel_target is None or plan.selected_workers <= 1:
            parallel.init_local_worker(self._backtest_service, payload)
            for index, ref in enumerate(refs):
                self._raise_if_cancelled(cancel_check)
                outcomes.append(task(ref))
                self._progress(
                    progress_callback,
                    progress_offset + index + 1,
                    work_total,
                    phase_label,
                    index + 1,
                    len(refs),
                )
        else:
            adapter_kind, database_path = self._parallel_target
            try:
                with parallel.research_executor(
                    plan, adapter_kind, database_path, payload
                ) as executor:
                    futures = {executor.submit(task, ref): ref for ref in refs}
                    for completed, future in enumerate(as_completed(futures), start=1):
                        self._raise_if_cancelled(cancel_check)
                        outcomes.append(future.result())
                        self._progress(
                            progress_callback,
                            progress_offset + completed,
                            work_total,
                            phase_label,
                            completed,
                            len(refs),
                        )
            except BrokenProcessPool as exc:
                raise TaskUserError("买卖条件研究工作进程意外退出，请重试或减少并发数") from exc
        outcomes.sort(key=lambda outcome: order.get(outcome.key, len(order)))
        return outcomes

    def _mine_candidates(
        self,
        config: ConditionResearchConfig,
        buy_samples: Sequence[MiningSamples],
        sell_samples: Sequence[MiningSamples],
    ) -> list[tuple[str, str, MinedCondition]]:
        results: list[tuple[str, str, MinedCondition]] = []
        if config.search_sides in {"buy", "both"}:
            results.extend(
                (f"buy-{index + 1}", "buy", candidate)
                for index, candidate in enumerate(
                    _mine_sample_sets(
                        buy_samples,
                        min_support=config.min_trades,
                        min_instruments=config.min_instruments,
                        limit=config.candidate_limit,
                        excluded=_rule_condition_signatures(config.buy_rule),
                    )
                )
            )
        if config.search_sides in {"sell", "both"}:
            results.extend(
                (f"sell-{index + 1}", "sell", candidate)
                for index, candidate in enumerate(
                    _mine_sample_sets(
                        sell_samples,
                        min_support=config.min_trades,
                        min_instruments=config.min_instruments,
                        limit=config.candidate_limit,
                        excluded=_rule_condition_signatures(config.sell_rule),
                    )
                )
            )
        return results

    @staticmethod
    def _progress(
        callback: ProgressCallback | None,
        current: int,
        total: int,
        phase: str,
        phase_current: int,
        phase_total: int,
    ) -> None:
        if callback is not None:
            callback(current, total)
            report_progress_detail(
                callback,
                {
                    "phase": phase,
                    "phase_current": phase_current,
                    "phase_total": phase_total,
                },
            )

    @staticmethod
    def _raise_if_cancelled(cancel_check: CancelCheck | None) -> None:
        if cancel_check is not None and cancel_check():
            raise TaskCancelledError("买卖条件研究任务已取消")

    @staticmethod
    def _empty_result(
        config: ConditionResearchConfig,
        splits: ResearchSplits,
        total: int,
        processed: set[str],
        feature_ids: tuple[str, ...],
        baseline: Mapping[str, object],
        failures: Counter[str],
        fingerprints: Mapping[str, str],
        sample_totals: Mapping[str, Mapping[str, object]],
    ) -> dict[str, object]:
        return {
            "universe": config.universe,
            "start_date": config.start_date.isoformat(),
            "end_date": config.end_date.isoformat(),
            "search_sides": config.search_sides,
            "data_fingerprint": _combined_fingerprint(fingerprints),
            "feature_count": len(feature_ids),
            "min_trades": config.min_trades,
            "min_instruments": config.min_instruments,
            "sample_totals": dict(sample_totals),
            "disclosure": _disclosure(config.universe, total, len(processed)),
            "total_candidates": total,
            "processed": len(processed),
            "skipped": max(0, total - len(processed)),
            "errors": sum(failures.values()),
            "failure_reasons": dict(failures),
            "splits": _splits_dict(splits),
            "baseline": {"train": _without_stock_rows(baseline)},
            "candidates": [],
            "diagnostic": "基础交易样本不足，未发现满足样本和覆盖要求的候选条件",
            "algorithm": {
                "mining": "contrastive_threshold_and_pairwise_rules",
                "search": "single_condition_then_full_stock_replay",
                "holdout_candidates_per_side": config.holdout_candidates,
            },
        }


def candidate_constraint_failures(
    candidate_summary: Mapping[str, object],
    baseline_summary: Mapping[str, object],
    *,
    min_trades: int,
    min_instruments: int,
) -> tuple[str, ...]:
    candidate = _pooled(candidate_summary)
    baseline = _pooled(baseline_summary)
    failures: list[str] = []
    if (_number(candidate.get("closed_trades")) or 0.0) < min_trades:
        failures.append("交易样本不足")
    if (_number(candidate.get("stocks_with_trades")) or 0.0) < min_instruments:
        failures.append("覆盖股票不足")
    candidate_win = _number(candidate.get("win_rate"))
    baseline_win = _number(baseline.get("win_rate"))
    if candidate_win is None or baseline_win is None:
        failures.append("基础规则样本不足，无法比较胜率")
    elif candidate_win <= baseline_win:
        failures.append("胜率未高于基础规则")
    for key, label in (
        ("payoff_ratio", "盈亏比低于基础规则"),
        ("mean_trade_return", "平均单笔收益低于基础规则"),
    ):
        candidate_metric = _number(candidate.get(key))
        baseline_metric = _number(baseline.get(key))
        if baseline_metric is not None and (
            candidate_metric is None or candidate_metric + 1e-12 < baseline_metric
        ):
            failures.append(label)
    return tuple(failures)


def _mine_sample_sets(
    samples: Sequence[MiningSamples],
    *,
    min_support: int,
    min_instruments: int,
    limit: int,
    excluded: frozenset[str] = frozenset(),
) -> tuple[MinedCondition, ...]:
    usable = [sample for sample in samples if len(sample)]
    if not usable:
        return ()
    return mine_binary_conditions(
        pd.concat([sample.features for sample in usable], ignore_index=True),
        pd.concat([sample.outcomes for sample in usable], ignore_index=True),
        pd.concat([sample.instruments for sample in usable], ignore_index=True),
        sample_groups=pd.concat([sample.groups for sample in usable], ignore_index=True),
        min_support=min_support,
        min_instruments=min_instruments,
        limit=limit,
        excluded=excluded,
    )


def _rule_condition_signatures(rule: RuleGroup | None) -> frozenset[str]:
    """Signatures of a base rule, so mining never re-proposes its own conditions.

    A buy candidate is ANDed with the base buy rule and a sell candidate is ORed
    with the base sell rule; a condition already present on the same side can
    therefore only produce a no-op candidate.
    """

    if rule is None:
        return frozenset()
    return frozenset(
        signature
        for condition in (*rule.required_conditions, *rule.conditions)
        for signature in equivalent_condition_signatures(condition)
    )


_COMPACT_TRADE_KEYS = ("direction", "pnl", "cost_basis", "rejected")


def _compact_report(report: BacktestReport) -> dict[str, object]:
    """Research keeps only trade outcomes, so build the compact dict directly.

    Going through ``report.to_dict()`` materialized every trade's full record
    (13 fields) and immediately replaced it with a 4-field copy.
    """

    return {
        "market": report.market,
        "code": report.code,
        "name": report.name,
        "bars": report.bars,
        "start_date": report.start_date,
        "end_date": report.end_date,
        "buy_signal": report.buy_signal,
        "sell_signal": report.sell_signal,
        "performance": dict(report.performance),
        "equity_curve": [],
        "trades": [{key: trade.get(key) for key in _COMPACT_TRADE_KEYS} for trade in report.trades],
        "positions": [],
        "configuration": dict(report.configuration),
        "diagnostic": report.diagnostic,
    }


def _condition_dict(condition: ScreenCondition) -> dict[str, object]:
    return {
        "left_value": condition.left_value,
        "operator": condition.operator,
        "right_value": condition.right_value,
        "right_constant": condition.right_constant,
    }


def _mining_dict(candidate: MinedCondition) -> dict[str, object]:
    return {
        "support_count": candidate.support_count,
        "support_ratio": candidate.support_ratio,
        "instrument_count": candidate.instrument_count,
        "outcome_rate": candidate.outcome_rate,
        "baseline_rate": candidate.baseline_rate,
        "lift": candidate.lift,
    }


def _disclosure(
    universe: str,
    total: int,
    processed: int,
) -> dict[str, object]:
    """Data-coverage and survivorship-bias disclosure required by the contract.

    The universe is whatever the current market-data store lists, so delisted
    symbols and historical index membership simply are not there.  The product
    contract forbids presenting that as a complete historical market without
    saying so, so every report carries the limitation explicitly.
    """

    return {
        "universe_scope": "current_listed_only",
        "universe": universe,
        "survivorship_bias": True,
        "requested_instruments": total,
        "processed_instruments": processed,
        "coverage_ratio": (processed / total) if total else 0.0,
        "notes": [
            "标的范围取自当前行情库中的在市标的，无法还原历史时点成分或退市标的，"
            "结果存在幸存者偏差，不应视为完整历史全市场。",
            f"本次仅 {processed}/{total} 只标的进入统计，未覆盖标的未参与任何汇总。",
        ],
    }


def _sample_totals(
    buy_samples: Sequence[MiningSamples],
    sell_samples: Sequence[MiningSamples],
) -> dict[str, dict[str, object]]:
    """Per-side mined sample totals, so the UI can show a correct hit ratio.

    The two sides count different units.  A buy sample is taken on the entry
    signal date, so ``support_count / buy total`` really is an entry hit rate.
    A sell sample is taken on every early-exit decision point inside a holding
    period, so several samples can belong to one trade; dividing those by the
    closed-trade count would overstate the ratio and could exceed 100%.
    """

    return {
        "buy": {"count": sum(len(samples) for samples in buy_samples), "unit": "entry"},
        "sell": {
            "count": sum(len(samples) for samples in sell_samples),
            "unit": "decision_point",
        },
    }


def _splits_dict(splits: ResearchSplits) -> dict[str, dict[str, str]]:
    return {
        name: {
            "start_date": window.start_date.isoformat(),
            "end_date": window.end_date.isoformat(),
        }
        for name, window in (
            ("train", splits.train),
            ("validation", splits.validation),
            ("holdout", splits.holdout),
        )
    }


def _without_stock_rows(summary: Mapping[str, object]) -> dict[str, object]:
    result = dict(summary)
    result.pop("stocks", None)
    return result


def _pooled(summary: Mapping[str, object]) -> Mapping[str, object]:
    value = summary.get("pooled_trades")
    return value if isinstance(value, Mapping) else {}


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    resolved = float(value)
    return resolved if math.isfinite(resolved) else None


def _symbol_key(ref: StockRef) -> str:
    return f"{ref.market}{ref.code}"


def _combined_fingerprint(fingerprints: Mapping[str, str]) -> str | None:
    if not fingerprints:
        return None
    hasher = hashlib.sha256()
    for symbol, fingerprint in sorted(fingerprints.items()):
        hasher.update(symbol.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(fingerprint.encode("ascii"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def _summary_number(result: Mapping[str, object], key: str) -> float:
    validation = result.get("validation")
    if not isinstance(validation, Mapping):
        return float("-inf")
    if key == "improved_stock_ratio":
        per_stock = validation.get("per_stock")
        value = per_stock.get(key) if isinstance(per_stock, Mapping) else None
    else:
        value = _pooled(validation).get(key)
    number = _number(value)
    return number if number is not None else float("-inf")


def _mining_number(result: Mapping[str, object], key: str) -> float:
    mining = result.get("mining")
    value = mining.get(key) if isinstance(mining, Mapping) else None
    number = _number(value)
    return number if number is not None else float("-inf")


def _select_holdout_candidates(
    results: Sequence[Mapping[str, object]],
    limit_per_side: int,
) -> set[str]:
    by_side: dict[str, list[Mapping[str, object]]] = {}
    for result in results:
        if result.get("passed_constraints") is True:
            by_side.setdefault(str(result.get("side")), []).append(result)
    selected: set[str] = set()
    for candidates in by_side.values():
        candidates.sort(
            key=lambda item: (
                -_summary_number(item, "win_rate"),
                -_summary_number(item, "improved_stock_ratio"),
                -_mining_number(item, "lift"),
            )
        )
        selected.update(
            str(candidate["candidate_id"])
            for candidate in candidates[:limit_per_side]
            if isinstance(candidate.get("candidate_id"), str)
        )
    return selected


def _reason(exc: Exception) -> str:
    text = str(exc).strip()
    return text[:200] if text else type(exc).__name__


__all__ = ["ConditionResearchService", "candidate_constraint_failures"]
