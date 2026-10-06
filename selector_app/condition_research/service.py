"""Mine and replay buy/sell condition candidates across independent stocks."""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from collections.abc import Callable, Collection, Mapping, MutableMapping, Sequence
from concurrent.futures import FIRST_COMPLETED, wait
from concurrent.futures.process import BrokenProcessPool
from datetime import date, datetime
from pathlib import Path
from typing import Protocol, TypeVar, cast

from selector_app.backtest.service import (
    BacktestMarketDataAdapter,
    BacktestService,
)
from selector_app.formulas.custom import custom_output_id, parse_formula
from selector_app.formulas.registry import FORMULA_REGISTRY, FormulaRegistry
from selector_app.market_data.adapter import DuckDbMarketDataAdapter
from selector_app.market_data.models import InstrumentBoard, InstrumentType, StockRef
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.screening.jobs import TaskCancelledError, TaskUserError, report_progress_detail
from selector_app.screening.models import ScreenCondition
from selector_app.strategy_discovery.catalog import default_indicator_catalog

from . import parallel
from .metrics import summarize_backtests
from .mining import (
    MinedCondition,
)
from .models import ConditionResearchConfig
from .parallel import (
    CandidateWindowPayload,
    HoldoutPayload,
    TrainPayload,
)
from .ranking import assess_ranking_values
from .reporting import (
    _candidate_evidence_key,
    _combined_fingerprint,
    _condition_dict,
    _disclosure,
    _empty_diagnostic,
    _mine_sample_sets,
    _mining_dict,
    _resolved_ranking,
    _rolling_note,
    _rule_condition_signatures,
    _sample_totals,
    _select_holdout_candidates,
    _splits_dict,
    _top_n_context,
    _trial_context,
    _without_stock_rows,
    assign_validation_objective_scores,
    candidate_constraint_failures,
    candidate_performance_warnings,
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
)
from .sensitivity import build_sensitivity_for_samples, sensitivity_dict
from .time_split import (
    DateWindow,
    ResearchSplits,
    split_calendar_range,
    split_windows,
)
from .trial_ledger import (
    TrialRecord,
    cumulative_tested_conditions,
    effective_trial_count,
)
from .windows import (
    build_baseline_window_payload,
    build_consistency,
    build_window_payload,
)
from .worker_tasks import (
    _candidate_window_task,
    _candidates_per_stock,
    _estimated_bars_per_stock,
    _holdout_task,
    _symbol_key,
    _train_task,
)

ProgressCallback = Callable[[int, int], None]
CancelCheck = Callable[[], bool]

logger = logging.getLogger(__name__)


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
        read_trading_dates = getattr(self._store, "read_completed_trading_dates", None)
        calendar_source = "completed_default_basis_bars"
        if not callable(read_trading_dates):
            read_trading_dates = getattr(self._store, "read_trading_dates", None)
            calendar_source = "observed_bars" if callable(read_trading_dates) else "calendar_days"
        trading_dates = read_trading_dates() if callable(read_trading_dates) else None
        splits = split_calendar_range(
            config.start_date,
            config.end_date,
            train_ratio=config.train_ratio,
            validation_ratio=config.validation_ratio,
            trading_dates=trading_dates,
        )
        feature_ids = self._feature_ids(config)
        plan = self._plan_for(refs, config)
        # 滚动复验：留出段再切几段逐个回放。每多一个窗口就多一轮逐股回放，
        # 进度条的总量要跟着涨，否则后半程进度会卡在不动。
        rolling_windows = (
            split_windows(
                splits.holdout.start_date,
                splits.holdout.end_date,
                config.walk_forward_windows,
            )
            if config.walk_forward_windows > 1
            else ()
        )
        # 窗口数后来还会被「有没有候选进留出期」再收一次：没有候选可比时，
        # 逐窗基线没有对照对象，跑 K 轮逐股回放纯属浪费（见下面的 rolling）。
        rolling = len(rolling_windows) > 1
        work_total = max(1, len(refs) * (3 + len(rolling_windows)))
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

        # The ranking advice needs only the signal samples, not the mined
        # candidates, so it is computed first: that lets mining score every
        # candidate under the slot limit the portfolio will actually face.
        ranking = self._ranking_assessment(config, buy_samples, feature_ids)
        ranking_value, rank_order = _resolved_ranking(config, ranking)
        self._raise_if_cancelled(cancel_check)
        trial_stats: dict[str, int] = {}
        mined = self._mine_candidates(
            config,
            buy_samples,
            sell_samples,
            ranking_value=ranking_value,
            rank_order=rank_order,
            stats=trial_stats,
            cancel_check=cancel_check,
        )
        trial_context = self._record_trial(
            config,
            fingerprints=fingerprints,
            tested_conditions=trial_stats.get("tested_conditions", 0),
        )
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
            baseline_payload["windows"] = []
            empty_result["ranking"] = ranking
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
            mining_payload = _mining_dict(
                candidate,
                trial_count=cast(int, trial_context["trial_count"]),
                quantile=config.significance_quantile,
            )
            # 参数敏感性在挖掘样本（寻优期）上重算邻域，所以每个候选都能算，
            # 不需要等它进留出期。口径与 mining_payload 同源，可逐个数字核对。
            sensitivity_payload = sensitivity_dict(
                build_sensitivity_for_samples(
                    candidate.condition,
                    buy_samples if side == "buy" else sell_samples,
                    min_support=config.min_trades,
                )
            )
            raw_significant = mining_payload["significant"]
            reasons = candidate_constraint_failures(
                summary,
                validation_baseline_summary,
                min_trades=config.min_trades,
                min_instruments=config.min_instruments,
                significant=raw_significant if isinstance(raw_significant, bool) else None,
            )
            performance_warnings = candidate_performance_warnings(
                summary,
                validation_baseline_summary,
            )
            results.append(
                {
                    "candidate_id": candidate_id,
                    "side": side,
                    "condition": _condition_dict(candidate.condition),
                    "mining": mining_payload,
                    # 邻域曲线：阈值往两边挪一点，优势还在不在。
                    "sensitivity": sensitivity_payload,
                    "validation": _without_stock_rows(summary),
                    "passed_constraints": not reasons,
                    "constraint_failures": list(reasons),
                    "performance_warnings": list(performance_warnings),
                    "validation_objective_score": None,
                    "full_period": None,
                    "holdout": None,
                    "windows": [],
                    "consistency": None,
                }
            )
        assign_validation_objective_scores(results)
        results.sort(
            key=lambda item: (
                not bool(item.get("passed_constraints")),
                *_candidate_evidence_key(item),
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

        baseline_holdout_summary = summarize_backtests(holdout_baseline)
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
                result["holdout_evaluation"] = _holdout_evaluation(
                    cast(Mapping[str, object], result["holdout"]),
                    baseline_holdout_summary,
                    trading_day_count=(
                        sum(
                            splits.holdout.start_date <= day <= splits.holdout.end_date
                            for day in trading_dates
                        )
                        if trading_dates is not None
                        else None
                    ),
                    min_trades=config.min_trades,
                    min_instruments=config.min_instruments,
                )

        baseline_windows: list[dict[str, object]] = []
        if rolling and not holdout_ids:
            # 没有候选进留出期，逐窗基线没有对照对象，不必再跑 K 轮。
            rolling = False
        if rolling:
            baseline_windows = self._rolling_windows(
                config,
                rolling_windows,
                refs=refs,
                feature_ids=feature_ids,
                fingerprints=fingerprints,
                holdout_ids=holdout_ids,
                holdout_specs=holdout_specs,
                results=results,
                failures=failures,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
                plan=plan,
                work_total=work_total,
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
            # 同义条件合并：门槛 + 本次一共合并掉多少条。合并是"换了别条进榜"
            # 不是"少给几条"，所以数量要能被用户看见。
            "duplicate_correlation": config.duplicate_correlation,
            "merged_duplicate_count": sum(len(candidate.duplicates) for _, _, candidate in mined),
            "sample_totals": sample_totals,
            "disclosure": _disclosure(config.universe, len(refs), len(processed)),
            "total_candidates": len(refs),
            "processed": len(processed),
            "skipped": max(0, len(refs) - len(processed)),
            "errors": sum(failures.values()),
            "failure_reasons": dict(failures),
            "splits": _splits_dict(splits),
            "split_calendar_source": calendar_source,
            "baseline": {
                "train": _without_stock_rows(baseline_train),
                "validation": _without_stock_rows(validation_baseline_summary),
                "full_period": summarize_backtests(full_period_reports),
                "holdout": summarize_backtests(holdout_baseline),
                "windows": baseline_windows,
            },
            "rolling": {
                "enabled": rolling,
                "requested": config.walk_forward_windows,
                "used": len(rolling_windows) if rolling else 1,
                "note": _rolling_note(
                    config.walk_forward_windows,
                    used=len(rolling_windows),
                    holdout=splits.holdout,
                    has_candidates=bool(holdout_ids),
                ),
            },
            "candidates": results,
            "ranking": ranking,
            # 累计试验记账：跑过多少轮独立数据、本次挖了多少条件、判定有没有被校正。
            "trials": trial_context,
            "algorithm": {
                "mining": "contrastive_threshold_and_pairwise_rules",
                "search": "single_condition_then_full_stock_replay",
                "holdout_candidates_per_side": config.holdout_candidates,
            },
        }

    def _record_trial(
        self,
        config: ConditionResearchConfig,
        *,
        fingerprints: Mapping[str, str],
        tested_conditions: int,
    ) -> dict[str, object]:
        """Log this study in the trial ledger and summarise the running total.

        Returns the context the report carries so the UI can say how many
        independent rounds a result has survived. Two guards matter:

        * no store (unit tests inject a fake adapter) -> do not touch a real
          database just to keep a ledger;
        * nothing was actually mined -> do not log a round, or a failed study
          would inflate everyone else's threshold.

        A ledger write failure only warns: the ledger is audit information and
        must never be the reason a research run fails.
        """

        if self._store is None or tested_conditions <= 0:
            return _trial_context(tested_conditions=tested_conditions, trials=1, studies=0)
        try:
            self._store.save_research_trial(
                uuid.uuid4().hex,
                data_fingerprint=_combined_fingerprint(fingerprints) or "",
                start_date=config.start_date.isoformat(),
                end_date=config.end_date.isoformat(),
                tested_conditions=tested_conditions,
            )
            records = [
                TrialRecord(
                    history_id=str(row["history_id"]),
                    created_at=cast(datetime, row["created_at"]),
                    data_fingerprint=str(row["data_fingerprint"]),
                    start_date=date.fromisoformat(str(row["start_date"])),
                    end_date=date.fromisoformat(str(row["end_date"])),
                    tested_conditions=int(str(row["tested_conditions"])),
                )
                for row in self._store.list_research_trials()
            ]
        except Exception:
            logger.warning("试验台账读写失败，本次研究不计入累计试验次数", exc_info=True)
            return _trial_context(tested_conditions=tested_conditions, trials=1, studies=0)
        return _trial_context(
            tested_conditions=tested_conditions,
            trials=effective_trial_count(records),
            studies=len(records),
            cumulative=cumulative_tested_conditions(records),
        )

    def _rolling_windows(
        self,
        config: ConditionResearchConfig,
        windows: Sequence[DateWindow],
        *,
        refs: Sequence[StockRef],
        feature_ids: tuple[str, ...],
        fingerprints: Mapping[str, str],
        holdout_ids: Collection[str],
        holdout_specs: tuple[tuple[str, str, ScreenCondition], ...],
        results: Sequence[dict[str, object]],
        failures: Counter[str],
        progress_callback: ProgressCallback | None,
        cancel_check: CancelCheck | None,
        plan: ResearchResourceEstimate,
        work_total: int,
    ) -> list[dict[str, object]]:
        """Replay the same candidates on each slice of the holdout window.

        挖掘只在训练段做一次，所以各窗用的是同一批条件——这才谈得上"同一个条件
        在几个窗口里的表现"。真 walk-forward（每窗重挖）要等能判定跨窗条件是否
        同义之后再说，见 docs/rolling-validation-plan.md。
        """

        per_window_candidates: list[dict[str, list[Mapping[str, object]]]] = [
            {candidate_id: [] for candidate_id in holdout_ids} for _ in windows
        ]
        per_window_baseline: list[list[Mapping[str, object]]] = [[] for _ in windows]
        for index, window in enumerate(windows):
            for outcome in self._run_phase(
                CandidateWindowPayload(
                    config=config,
                    feature_ids=feature_ids,
                    start_date=window.start_date,
                    end_date=window.end_date,
                    fingerprints=dict(fingerprints),
                    candidates=holdout_specs,
                ),
                refs,
                _candidate_window_task,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
                progress_offset=len(refs) * (3 + index),
                work_total=work_total,
                phase_label=f"复验第 {index + 1}/{len(windows)} 个窗口",
                plan=plan,
            ):
                if outcome.failure is not None:
                    failures[outcome.failure] += 1
                    continue
                per_window_baseline[index].append(cast(Mapping[str, object], outcome.baseline))
                for candidate_id, report in outcome.candidates:
                    per_window_candidates[index][candidate_id].append(report)

        baseline_summaries = [summarize_backtests(reports) for reports in per_window_baseline]
        baseline_payloads = [
            build_baseline_window_payload(
                index=index + 1,
                window=window,
                baseline_summary=baseline_summaries[index],
            )
            for index, window in enumerate(windows)
        ]
        for result in results:
            candidate_id = cast(str, result["candidate_id"])
            if candidate_id not in holdout_ids:
                continue
            payloads = [
                build_window_payload(
                    index=index + 1,
                    window=window,
                    summary=summarize_backtests(
                        per_window_candidates[index][candidate_id],
                        baseline_reports=per_window_baseline[index],
                    ),
                    baseline_summary=baseline_summaries[index],
                    min_trades=config.min_trades,
                )
                for index, window in enumerate(windows)
            ]
            result["windows"] = payloads
            result["consistency"] = build_consistency(
                payloads,
                requested=config.walk_forward_windows,
                used=len(windows),
                min_trades=config.min_trades,
            )
        return baseline_payloads

    def _ranking_assessment(
        self,
        config: ConditionResearchConfig,
        buy_samples: Sequence[MiningSamples],
        feature_ids: Sequence[str],
    ) -> dict[str, object]:
        """Advise on a ranking value, reusing the signal days mining built.

        Purely additive: it reads the mined entry samples and never changes how
        candidates are evaluated, so a wrong ranking suggestion cannot corrupt
        the study itself.
        """

        display_names = {
            item.id: item.display_name for item in default_indicator_catalog(self._registry).values
        }
        if config.formula_text:
            parsed = parse_formula(config.formula_text)
            display_names.update({custom_output_id(name): name for name in parsed.value_names})
        payload = assess_ranking_values(
            buy_samples,
            feature_ids,
            top_n=config.ranking_top_n,
            display_names=display_names,
        ).to_dict()
        payload["selection"] = {
            "value": config.ranking_value,
            "order": config.rank_order,
        }
        return payload

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
                    pending = set(futures)
                    completed = 0
                    while pending:
                        if cancel_check is not None and cancel_check():
                            for future in pending:
                                future.cancel()
                            self._raise_if_cancelled(cancel_check)
                        done, pending = wait(
                            pending,
                            timeout=0.25,
                            return_when=FIRST_COMPLETED,
                        )
                        if not done:
                            continue
                        for future in done:
                            outcomes.append(future.result())
                            completed += 1
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
        *,
        ranking_value: str | None,
        rank_order: str,
        stats: MutableMapping[str, int] | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> list[tuple[str, str, MinedCondition]]:
        # Slots are competed for on entry only, so the top-N diagnostic is
        # meaningful for buy candidates and meaningless for sell triggers.
        buy_context = (
            _top_n_context(buy_samples, ranking_value, rank_order, config.ranking_top_n)
            if config.search_sides in {"buy", "both"}
            else None
        )
        results: list[tuple[str, str, MinedCondition]] = []
        side_stats: dict[str, int] = {}
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
                        top_n_context=buy_context,
                        permutations=config.significance_permutations,
                        significance_quantile=config.significance_quantile,
                        duplicate_correlation=config.duplicate_correlation,
                        stats=side_stats,
                        abort_check=cancel_check,
                    )
                )
            )
        if config.search_sides in {"sell", "both"}:
            sell_stats: dict[str, int] = {}
            results.extend(
                (f"sell-{index + 1}", "sell", candidate)
                for index, candidate in enumerate(
                    _mine_sample_sets(
                        sell_samples,
                        min_support=config.min_trades,
                        min_instruments=config.min_instruments,
                        limit=config.candidate_limit,
                        excluded=_rule_condition_signatures(config.sell_rule),
                        permutations=config.significance_permutations,
                        significance_quantile=config.significance_quantile,
                        duplicate_correlation=config.duplicate_correlation,
                        stats=sell_stats,
                        abort_check=cancel_check,
                    )
                )
            )
            side_stats["tested_conditions"] = side_stats.get(
                "tested_conditions", 0
            ) + sell_stats.get("tested_conditions", 0)
        if stats is not None:
            # 两侧分别挖，各自枚举一遍候选池，所以累计试验数要把两侧加起来。
            stats["tested_conditions"] = side_stats.get("tested_conditions", 0)
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
            "duplicate_correlation": config.duplicate_correlation,
            "merged_duplicate_count": 0,
            "sample_totals": dict(sample_totals),
            "disclosure": _disclosure(config.universe, total, len(processed)),
            "total_candidates": total,
            "processed": len(processed),
            "skipped": max(0, total - len(processed)),
            "errors": sum(failures.values()),
            "failure_reasons": dict(failures),
            "splits": _splits_dict(splits),
            "baseline": {"train": _without_stock_rows(baseline), "windows": []},
            # 没有候选就没有滚动复验的对象，但字段要有，前端才能统一读。
            "rolling": {
                "enabled": False,
                "requested": config.walk_forward_windows,
                "used": 1,
                "note": (
                    "没有候选通过验证进入留出期，未做滚动复验"
                    if config.walk_forward_windows > 1
                    else None
                ),
            },
            "candidates": [],
            "diagnostic": _empty_diagnostic(config, sample_totals),
            "algorithm": {
                "mining": "contrastive_threshold_and_pairwise_rules",
                "search": "single_condition_then_full_stock_replay",
                "holdout_candidates_per_side": config.holdout_candidates,
            },
        }


def _holdout_evaluation(
    candidate_summary: Mapping[str, object],
    baseline_summary: Mapping[str, object],
    *,
    trading_day_count: int | None,
    min_trades: int,
    min_instruments: int,
) -> dict[str, object]:
    failures = candidate_constraint_failures(
        candidate_summary,
        baseline_summary,
        min_trades=min_trades,
        min_instruments=min_instruments,
    )
    insufficient = [
        reason
        for reason in failures
        if reason in {
            "交易样本不足",
            "覆盖股票不足",
            "基础规则样本不足，无法比较胜率",
        }
    ]
    base: dict[str, object] = {
        "trading_days": trading_day_count,
        "failures": list(failures),
    }
    if trading_day_count is None:
        return {
            **base,
            "status": "unevaluable",
            "reason": "实际交易日数据不可用，无法确认留出段覆盖情况",
        }
    if trading_day_count < 1:
        return {**base, "status": "insufficient_samples", "reason": "留出段没有实际交易日"}
    if insufficient:
        return {
            **base,
            "status": "insufficient_samples",
            "reason": "；".join(insufficient),
        }
    return {
        **base,
        "status": "evaluated",
        "passed_constraints": not failures,
    }


_COMPACT_TRADE_KEYS = ("direction", "pnl", "cost_basis", "rejected")


__all__ = ["ConditionResearchService", "candidate_constraint_failures"]
