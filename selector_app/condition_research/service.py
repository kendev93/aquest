"""Mine and replay buy/sell condition candidates across independent stocks."""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol, cast

import pandas as pd

from selector_app.backtest.models import BacktestConfig, BacktestReport
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
from selector_app.strategy_discovery.catalog import default_indicator_catalog

from .metrics import summarize_backtests
from .mining import MinedCondition, mine_binary_conditions
from .models import ConditionResearchConfig
from .samples import MiningSamples, build_buy_trade_samples, build_early_exit_samples
from .time_split import DateWindow, ResearchSplits, split_calendar_range

ProgressCallback = Callable[[int, int], None]
CancelCheck = Callable[[], bool]


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
        backtest_service: BacktestService | None = None,
        registry: FormulaRegistry | None = None,
    ) -> None:
        selected_store = DuckDbMarketDataStore()
        self._adapter = adapter or DuckDbMarketDataAdapter(selected_store)
        self._registry = registry or FORMULA_REGISTRY
        self._backtest_service = backtest_service or BacktestService(
            adapter=cast(BacktestMarketDataAdapter, self._adapter),
            registry=self._registry,
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
        work_total = max(1, len(refs) * 3)
        self._progress(progress_callback, 0, work_total, "准备训练样本", 0, len(refs))

        train_reports: list[dict[str, object]] = []
        full_period_reports: list[dict[str, object]] = []
        buy_samples: list[MiningSamples] = []
        sell_samples: list[MiningSamples] = []
        failures: Counter[str] = Counter()
        processed: set[tuple[str, str]] = set()
        fingerprints: dict[str, str] = {}

        for index, ref in enumerate(refs):
            self._raise_if_cancelled(cancel_check)
            try:
                prepared = self._prepare(config, ref, feature_ids)
                fingerprints[_symbol_key(ref)] = prepared.data_fingerprint
                baseline = self._run_window(prepared, splits.train)
                train_reports.append(_compact_report(baseline))
                full_period_reports.append(_compact_report(self._run_full_period(prepared)))
                processed.add((ref.market, ref.code))
                if config.search_sides in {"buy", "both"}:
                    buy_samples.append(
                        build_buy_trade_samples(
                            baseline.trades,
                            prepared.feature_values,
                            market=ref.market,
                            code=ref.code,
                        )
                    )
                if config.search_sides in {"sell", "both"}:
                    sell_samples.append(
                        build_early_exit_samples(
                            baseline.trades,
                            prepared.feature_values,
                            prepared.frame,
                            prepared.config,
                            market=ref.market,
                            code=ref.code,
                        )
                    )
            except (ValueError, OSError) as exc:
                failures[_reason(exc)] += 1
            self._progress(
                progress_callback,
                index + 1,
                work_total,
                "挖掘盈利/亏损样本特征",
                index + 1,
                len(refs),
            )

        mined = self._mine_candidates(config, buy_samples, sell_samples)
        baseline_train = summarize_backtests(train_reports)
        if not mined:
            validation_reports: list[dict[str, object]] = []
            holdout_reports: list[dict[str, object]] = []
            for index, ref in enumerate(refs):
                self._raise_if_cancelled(cancel_check)
                try:
                    prepared = self._prepare(config, ref, feature_ids)
                    self._verify_data_fingerprint(prepared, ref, fingerprints)
                    validation_reports.append(
                        _compact_report(self._run_window(prepared, splits.validation))
                    )
                except (ValueError, OSError) as exc:
                    failures[_reason(exc)] += 1
                self._progress(
                    progress_callback,
                    len(refs) + index + 1,
                    work_total,
                    "计算基础规则验证结果",
                    index + 1,
                    len(refs),
                )
            for index, ref in enumerate(refs):
                self._raise_if_cancelled(cancel_check)
                try:
                    prepared = self._prepare(config, ref, feature_ids)
                    self._verify_data_fingerprint(prepared, ref, fingerprints)
                    holdout_reports.append(
                        _compact_report(self._run_window(prepared, splits.holdout))
                    )
                except (ValueError, OSError) as exc:
                    failures[_reason(exc)] += 1
                self._progress(
                    progress_callback,
                    len(refs) * 2 + index + 1,
                    work_total,
                    "计算基础规则留出结果",
                    index + 1,
                    len(refs),
                )
            empty_result = self._empty_result(
                config,
                splits,
                len(refs),
                processed,
                feature_ids,
                baseline_train,
                failures,
                fingerprints,
            )
            baseline_payload = cast(dict[str, object], empty_result["baseline"])
            baseline_payload["validation"] = _without_stock_rows(
                summarize_backtests(validation_reports)
            )
            baseline_payload["full_period"] = summarize_backtests(full_period_reports)
            baseline_payload["holdout"] = summarize_backtests(holdout_reports)
            return empty_result

        validation_candidates: dict[str, list[dict[str, object]]] = {
            candidate_id: [] for candidate_id, _, _ in mined
        }
        validation_baseline: list[dict[str, object]] = []
        for index, ref in enumerate(refs):
            self._raise_if_cancelled(cancel_check)
            try:
                prepared = self._prepare(config, ref, feature_ids)
                self._verify_data_fingerprint(prepared, ref, fingerprints)
                validation_baseline.append(
                    _compact_report(self._run_window(prepared, splits.validation))
                )
                for candidate_id, side, candidate in mined:
                    self._raise_if_cancelled(cancel_check)
                    report = self._run_candidate(
                        prepared, candidate.condition, side, splits.validation
                    )
                    validation_candidates[candidate_id].append(_compact_report(report))
            except (ValueError, OSError) as exc:
                failures[_reason(exc)] += 1
            self._progress(
                progress_callback,
                len(refs) + index + 1,
                work_total,
                "验证候选条件",
                index + 1,
                len(refs),
            )

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

        holdout_candidates: dict[str, list[dict[str, object]]] = {
            candidate_id: [] for candidate_id in holdout_ids
        }
        full_period_candidates: dict[str, list[dict[str, object]]] = {
            candidate_id: [] for candidate_id in holdout_ids
        }
        holdout_baseline: list[dict[str, object]] = []
        for index, ref in enumerate(refs):
            self._raise_if_cancelled(cancel_check)
            try:
                prepared = self._prepare(config, ref, feature_ids)
                self._verify_data_fingerprint(prepared, ref, fingerprints)
                holdout_baseline.append(_compact_report(self._run_window(prepared, splits.holdout)))
                for candidate_id, side, candidate in mined:
                    if candidate_id not in holdout_ids:
                        continue
                    self._raise_if_cancelled(cancel_check)
                    report = self._run_candidate(
                        prepared, candidate.condition, side, splits.holdout
                    )
                    holdout_candidates[candidate_id].append(_compact_report(report))
                    full_period_candidates[candidate_id].append(
                        _compact_report(
                            self._run_full_candidate(prepared, candidate.condition, side)
                        )
                    )
            except (ValueError, OSError) as exc:
                failures[_reason(exc)] += 1
            self._progress(
                progress_callback,
                len(refs) * 2 + index + 1,
                work_total,
                "复验最终留出期",
                index + 1,
                len(refs),
            )

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

    def _list_stock_refs(self, config: ConditionResearchConfig) -> list[StockRef]:
        try:
            refs = self._adapter.list_stock_refs(
                config.vipdoc_path,
                config.universe,
                instrument_types=("stock",),
                boards=config.boards or None,
            )
        except TypeError as exc:
            if "unexpected keyword" not in str(exc):
                raise
            refs = self._adapter.list_stock_refs(config.vipdoc_path, config.universe)
        return [
            ref
            for ref in refs
            if getattr(ref, "instrument_type", "stock") == "stock"
            and (not config.boards or getattr(ref, "board", None) in config.boards)
        ]

    def _feature_ids(self, config: ConditionResearchConfig) -> tuple[str, ...]:
        if config.formula_text:
            parsed = parse_formula(config.formula_text)
            value_ids = tuple(custom_output_id(name) for name in parsed.value_names)
            if not value_ids:
                raise ValueError("自定义公式至少需要包含一个数值输出，才能自动挖掘条件")
            return value_ids
        return tuple(
            item.id for item in default_indicator_catalog(self._registry).values if item.rankable
        )

    def _prepare(
        self,
        config: ConditionResearchConfig,
        ref: StockRef,
        feature_ids: tuple[str, ...],
    ) -> PreparedBacktest:
        backtest_config = BacktestConfig(
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
            commission=config.commission,
            min_commission=config.min_commission,
            stamp_tax=config.stamp_tax,
            slippage=config.slippage,
            execution=config.execution,
            position_mode=config.position_mode,
            fixed_size=config.fixed_size,
            instrument_type=ref.instrument_type,
            board=ref.board,
        )
        return self._backtest_service.prepare_for_ref(backtest_config, ref, feature_ids=feature_ids)

    def _run_window(self, prepared: PreparedBacktest, window: DateWindow) -> BacktestReport:
        return self._backtest_service.run_prepared(
            prepared,
            start_date=window.start_date,
            end_date=window.end_date,
            summary_only=True,
        )

    def _run_full_period(self, prepared: PreparedBacktest) -> BacktestReport:
        return self._backtest_service.run_prepared(prepared, summary_only=True)

    def _run_candidate(
        self,
        prepared: PreparedBacktest,
        condition: ScreenCondition,
        side: str,
        window: DateWindow,
    ) -> BacktestReport:
        return self._backtest_service.run_prepared(
            prepared,
            buy_filter=condition if side == "buy" else None,
            sell_trigger=condition if side == "sell" else None,
            start_date=window.start_date,
            end_date=window.end_date,
            summary_only=True,
        )

    def _run_full_candidate(
        self,
        prepared: PreparedBacktest,
        condition: ScreenCondition,
        side: str,
    ) -> BacktestReport:
        return self._backtest_service.run_prepared(
            prepared,
            buy_filter=condition if side == "buy" else None,
            sell_trigger=condition if side == "sell" else None,
            summary_only=True,
        )

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
    def _verify_data_fingerprint(
        prepared: PreparedBacktest,
        ref: StockRef,
        fingerprints: Mapping[str, str],
    ) -> None:
        previous = fingerprints.get(_symbol_key(ref))
        if previous is None:
            raise ValueError("该股票在寻优期没有有效行情，跳过后续复验")
        if previous != prepared.data_fingerprint:
            raise TaskUserError("行情数据在研究期间发生变化，请重新运行以保证各阶段一致")

    @staticmethod
    def _empty_result(
        config: ConditionResearchConfig,
        splits: ResearchSplits,
        total: int,
        processed: set[tuple[str, str]],
        feature_ids: tuple[str, ...],
        baseline: Mapping[str, object],
        failures: Counter[str],
        fingerprints: Mapping[str, str],
    ) -> dict[str, object]:
        return {
            "universe": config.universe,
            "start_date": config.start_date.isoformat(),
            "end_date": config.end_date.isoformat(),
            "search_sides": config.search_sides,
            "data_fingerprint": _combined_fingerprint(fingerprints),
            "feature_count": len(feature_ids),
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
    )


def _compact_report(report: BacktestReport) -> dict[str, object]:
    result = report.to_dict()
    trades = result.get("trades")
    result["trades"] = [
        {key: trade.get(key) for key in ("direction", "pnl", "cost_basis", "rejected")}
        for trade in cast(Sequence[Mapping[str, object]], trades or ())
    ]
    result["equity_curve"] = []
    result["positions"] = []
    return result


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
