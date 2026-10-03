"""Single-security swing labels, condition mining, and staged exit optimization."""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from contextlib import closing
from datetime import date
from itertools import combinations
from typing import cast

import numpy as np
import pandas as pd

from selector_app.backtest.models import BacktestConfig
from selector_app.backtest.service import BacktestService, PreparedBacktest
from selector_app.condition_research.mining import MinedCondition, mine_binary_conditions
from selector_app.condition_research.models import rule_to_dict
from selector_app.market_data.adapter import MarketDataAdapter
from selector_app.market_data.models import MarketCode, StockRef
from selector_app.portfolio_backtest.models import (
    SINGLE_SECURITY_RANKING_VALUE,
    PortfolioBacktestConfig,
    PortfolioBacktestReport,
)
from selector_app.portfolio_backtest.service import PortfolioBacktestService
from selector_app.screening.jobs import TaskCancelledError
from selector_app.screening.models import ScreenCondition
from selector_app.screening.rules import RuleGroup
from selector_app.strategy_discovery.evaluation_budget import EvaluationWorkerBudget
from selector_app.strategy_discovery.models import DiscoveryRequest
from selector_app.strategy_discovery.service import StrategyDiscoveryService
from selector_app.strategy_pool.selection import slices_for_common_windows
from selector_app.swing_research.combinations import split_trading_days
from selector_app.swing_research.labels import bar_labels
from selector_app.swing_research.segments import SwingSegment, split_swings
from selector_app.web.schemas import PortfolioBacktestRequest

from .metrics import candidate_quality
from .models import IndividualResearchConfig
from .search_policy import SEARCH_POLICY_ID, search_policy_settings

ProgressCallback = Callable[[int, int], None]
AbortCheck = Callable[[], bool]
RULE_SHORTLIST_PER_BOARD = 5
MAX_COMBINATION_COMPONENTS = 24
MAX_CANDIDATES_PER_COMPONENT = 3


class InsufficientResearchHistoryError(ValueError):
    """A deterministic split failure tied to the exact prepared market data."""

    def __init__(
        self,
        data_fingerprint: str,
        split_metadata: Mapping[str, object],
    ) -> None:
        super().__init__("当前历史区间无法切出训练、验证、留出三段；请扩大历史区间")
        self.data_fingerprint = data_fingerprint
        self.split_metadata = dict(split_metadata)


class IndividualStockResearchService:
    """Run all stages for one symbol, keeping the final holdout untouched."""

    def __init__(
        self,
        *,
        adapter: MarketDataAdapter,
        backtest_service: BacktestService,
        portfolio_service: PortfolioBacktestService,
        strategy_service: StrategyDiscoveryService,
        validation_database_path: str | None = None,
        validation_workers: int = 1,
        evaluation_worker_budget: EvaluationWorkerBudget | None = None,
    ) -> None:
        self._adapter = adapter
        self._backtest_service = backtest_service
        self._portfolio_service = portfolio_service
        self._strategy_service = strategy_service
        self._validation_database_path = validation_database_path
        self._validation_workers = validation_workers
        self._evaluation_worker_budget = evaluation_worker_budget

    def run(
        self,
        config: IndividualResearchConfig,
        progress_callback: ProgressCallback | None = None,
        abort_check: AbortCheck | None = None,
    ) -> dict[str, object]:
        run_started = time.perf_counter()
        stage_started = run_started
        stage_durations: dict[str, float] = {}
        ref = self._adapter.stock_ref("", config.market, config.code)
        if getattr(ref, "instrument_type", "stock") != "stock":
            raise ValueError("个股研究只支持股票行情")
        self._check_cancelled(abort_check)
        self._report(progress_callback, 2, "准备行情", "读取单股日线和指标")

        prepared = self._prepare(config, ref)
        dates, features, labels, signal_values, segments = self._research_window(prepared, config)
        windows: dict[str, dict[str, date] | None]
        if config.split_windows:
            split_slices = slices_for_common_windows(dates, config.split_windows)
            split_metadata = dict(config.common_split_metadata or {})
            split_metadata.setdefault(
                "method", "按共同市场日历固定切分 60/20/20，并在预测窗口边界清除前视样本"
            )
            split_metadata["holdout_policy"] = "留出段只复核最终风险候选，不参与条件或风险参数排序"
            windows = _split_windows(dates, split_slices)
            split_metadata["observed_windows"] = {
                phase: {
                    "start_date": bounds["start"].isoformat(),
                    "end_date": bounds["end"].isoformat(),
                }
                for phase, bounds in windows.items()
                if bounds is not None
            }
        else:
            split_slices, split_metadata = split_trading_days(dates, config.horizon)
            split_metadata["method"] = "按交易日顺序固定切分 60/20/20，并在预测窗口边界清除前视样本"
            split_metadata["holdout_policy"] = "留出段只复核最终风险候选，不参与条件或风险参数排序"
            windows = _split_windows(dates, split_slices)
        if any(window is None for window in windows.values()):
            raise InsufficientResearchHistoryError(prepared.data_fingerprint, split_metadata)

        segmentation = _segmentation_summary(segments, config)
        self._report(
            progress_callback,
            10,
            "识别波段",
            f"分析 {len(dates)} 个交易日：上涨波段 {segmentation.get('up_segments', 0)} 个，"
            f"下跌波段 {segmentation.get('down_segments', 0)} 个；生成买卖启动标签",
        )
        self._check_cancelled(abort_check)

        stage_durations["prepare_and_segment"] = time.perf_counter() - stage_started
        stage_started = time.perf_counter()
        candidates_by_side = self._mine_conditions(
            config,
            features,
            labels,
            signal_values,
            split_slices["train"],
        )
        stage_durations["mine_conditions"] = time.perf_counter() - stage_started
        stage_started = time.perf_counter()
        buy_candidates = candidates_by_side["buy"]
        sell_candidates = candidates_by_side["sell"]
        self._report(
            progress_callback,
            20,
            "发现买卖条件",
            f"买入候选 {len(buy_candidates)} 个，卖出候选 {len(sell_candidates)} 个",
        )
        if not buy_candidates or not sell_candidates:
            return self._empty_result(
                config,
                ref,
                prepared.data_fingerprint,
                segmentation,
                split_metadata,
                _public_conditions(candidates_by_side),
                "训练段没有足够的上涨/下跌样本，无法组成完整买卖规则；扩大日期区间或更换搜索指标后重试。",
                trading_days=len(dates),
            )

        pairs = _rule_pairs(buy_candidates, sell_candidates)
        train_window = cast(dict[str, date], windows["train"])
        validation_window = cast(dict[str, date], windows["validation"])
        rule_candidates: list[dict[str, object]] = []
        total_pairs = len(pairs)
        if self._evaluation_worker_budget is not None:
            self._report(progress_callback, 20, "验证买卖条件", "候选已生成，等待可用计算资源")
        pair_metrics = self._rule_pair_metrics(
            config, ref, pairs, train_window, validation_window, abort_check
        )
        with closing(pair_metrics):
            for index, ((pair_id, buy, sell), (train_metrics, validation_metrics)) in enumerate(
                zip(pairs, pair_metrics, strict=True), start=1
            ):
                self._check_cancelled(abort_check)
                buy_rule = buy["rule"]
                sell_rule = sell["rule"]
                assert isinstance(buy_rule, RuleGroup)
                assert isinstance(sell_rule, RuleGroup)
                rule_candidates.append(
                    {
                        "candidate_id": pair_id,
                        "buy": _public_rule_candidate(buy),
                        "sell": _public_rule_candidate(sell),
                        "buy_rule": rule_to_dict(buy_rule),
                        "sell_rule": rule_to_dict(sell_rule),
                        "train": train_metrics,
                        "validation": validation_metrics,
                    }
                )
                self._report(
                    progress_callback,
                    20 + int(25 * index / max(1, total_pairs)),
                    "验证买卖条件",
                    f"训练与验证回测：已验证 {index} / {total_pairs} 组买卖组合",
                )

        rule_by_sharpe = _rank_candidates(rule_candidates, "sharpe")
        rule_by_return = _rank_candidates(rule_candidates, "annual_return")
        selected_rule_pairs = _select_rule_pairs(rule_candidates)
        if not selected_rule_pairs:
            return self._empty_result(
                config,
                ref,
                prepared.data_fingerprint,
                segmentation,
                split_metadata,
                _public_conditions(candidates_by_side),
                "买卖规则候选没有可计算的验证绩效。",
                trading_days=len(dates),
                rule_candidates=rule_candidates,
            )

        stage_durations["rule_pair_validation"] = time.perf_counter() - stage_started
        stage_started = time.perf_counter()
        risk_candidates: list[dict[str, object]] = []
        risk_search_runs: list[dict[str, object]] = []
        risk_data_fingerprints: set[str] = set()
        for pair_index, pair in enumerate(selected_rule_pairs, start=1):
            self._check_cancelled(abort_check)
            pair_id = str(pair["candidate_id"])
            buy_rule = _parse_rule(pair.get("buy_rule"))
            sell_rule = _parse_rule(pair.get("sell_rule"))
            if buy_rule is None or sell_rule is None:
                continue
            request_payload = self._risk_base_payload(config, buy_rule, sell_rule)
            discovery_request = self._risk_discovery_request(
                request_payload,
                config,
                windows,
                seed=_stable_seed(config, pair_id),
            )
            try:
                result = self._strategy_service.run(
                    discovery_request,
                    progress_callback=self._risk_progress(
                        progress_callback,
                        pair_index,
                        len(selected_rule_pairs),
                        pair_id,
                        pair,
                    ),
                    abort_check=abort_check,
                )
            except (RuntimeError, ValueError) as exc:
                if abort_check is not None and abort_check():
                    raise TaskCancelledError("个股研究已取消") from exc
                risk_search_runs.append(
                    {
                        "rule_id": pair_id,
                        "search_metadata": {
                            "max_trials": config.risk_trials,
                            "stop_reason": "failed",
                        },
                        "error": str(exc),
                    }
                )
                risk_candidates.append(
                    {
                        "candidate_id": pair_id,
                        "condition_candidate_id": pair_id,
                        "error": str(exc),
                        "config": request_payload,
                        "metrics": {},
                    }
                )
                continue
            risk_search_runs.append(
                {
                    "rule_id": pair_id,
                    "search_metadata": result.get("search_metadata", {}),
                    "stage_durations_seconds": result.get("stage_durations_seconds", {}),
                }
            )
            risk_fingerprint = result.get("data_fingerprint")
            if isinstance(risk_fingerprint, str) and risk_fingerprint:
                risk_data_fingerprints.add(risk_fingerprint)
            for board in ("results_by_sharpe", "results_by_return"):
                for raw_candidate in _mapping_sequence(result.get(board)):
                    enriched = _individual_risk_candidate(raw_candidate, pair)
                    enriched["research_search"] = risk_search_runs[-1]
                    risk_candidates.append(enriched)

        stage_durations["risk_search"] = time.perf_counter() - stage_started
        stage_started = time.perf_counter()
        if len(risk_data_fingerprints) > 1:
            raise ValueError("本地行情在不同买卖规则寻优之间发生变化，请重新运行个股研究")

        unique_risk_candidates = _unique_candidates(risk_candidates, key="candidate_hash")
        ranked_sharpe = _rank_candidates(unique_risk_candidates, "sharpe")[:10]
        ranked_return = _rank_candidates(unique_risk_candidates, "annual_return")[:10]
        final_holdout = _unique_candidates([*ranked_sharpe, *ranked_return], key="candidate_hash")
        holdout_window = cast(dict[str, date], windows["holdout"])
        final_by_hash: dict[str, dict[str, object]] = {}
        total_holdout = len(final_holdout)
        for index, candidate in enumerate(final_holdout, start=1):
            self._check_cancelled(abort_check)
            candidate_config = candidate.get("config")
            if not isinstance(candidate_config, Mapping):
                continue
            try:
                holdout_metrics = self._backtest_candidate_metrics(
                    candidate_config,
                    holdout_window["start"],
                    holdout_window["end"],
                )
            except (TypeError, ValueError) as exc:
                holdout_metrics = {"error": str(exc), "closed_trades": 0}
            raw_metrics = candidate.get("metrics")
            metrics = dict(raw_metrics) if isinstance(raw_metrics, Mapping) else {}
            metrics["holdout"] = holdout_metrics
            candidate["metrics"] = metrics
            validation = metrics.get("validation")
            final_validation_metrics = validation if isinstance(validation, Mapping) else {}
            candidate["quality"] = dict(
                candidate_quality(
                    _trade_count(final_validation_metrics),
                    _trade_count(holdout_metrics),
                    _number(final_validation_metrics.get("max_drawdown")),
                )
            )
            final_by_hash[str(candidate.get("candidate_hash") or candidate.get("candidate_id"))] = (
                candidate
            )
            self._report(
                progress_callback,
                78 + int(10 * index / max(1, total_holdout)),
                "留出段复核",
                f"已复核 {index} / {total_holdout} 组最终候选",
            )

        full_backtest_fingerprints: set[str] = set()
        full_backtests: dict[str, dict[str, object]] = {}
        total_full_backtests = len(final_holdout)
        for index, candidate in enumerate(final_holdout, start=1):
            self._check_cancelled(abort_check)
            candidate_config = candidate.get("config")
            if not isinstance(candidate_config, Mapping):
                continue
            try:
                full_backtest = self._backtest_candidate_detail(
                    candidate_config,
                    config.start_date,
                    config.end_date,
                )
            except (TypeError, ValueError, RuntimeError) as exc:
                full_backtest = {
                    "start_date": config.start_date.isoformat(),
                    "end_date": config.end_date.isoformat(),
                    "bars": 0,
                    "performance": {},
                    "equity_curve": [],
                    "trades": [],
                    "error": str(exc),
                }
            fingerprint = full_backtest.get("data_fingerprint")
            if isinstance(fingerprint, str) and fingerprint:
                full_backtest_fingerprints.add(fingerprint)
            candidate_key = str(candidate.get("candidate_hash") or candidate.get("candidate_id"))
            candidate["full_backtest_id"] = candidate_key
            full_backtests[candidate_key] = full_backtest
            self._report(
                progress_callback,
                88 + int(11 * index / max(1, total_full_backtests)),
                "完整区间回测",
                f"已回测 {index} / {total_full_backtests} 组最终候选",
            )

        if len(full_backtest_fingerprints) > 1:
            raise ValueError("最终候选完整区间回测期间本地行情发生变化，请重新运行个股研究")

        def final_board(entries: Sequence[dict[str, object]]) -> list[dict[str, object]]:
            return [
                final_by_hash.get(
                    str(item.get("candidate_hash") or item.get("candidate_id")),
                    item,
                )
                for item in entries
            ]

        data_fingerprint = _fingerprint_many(
            prepared.data_fingerprint,
            sorted(risk_data_fingerprints | full_backtest_fingerprints),
        )
        final_result: dict[str, object] = {
            "market": config.market,
            "code": config.code,
            "name": ref.name or config.name,
            "start_date": config.start_date.isoformat(),
            "end_date": config.end_date.isoformat(),
            "date_range": {
                "start": config.start_date.isoformat(),
                "end": config.end_date.isoformat(),
                "days": len(dates),
            },
            "single_security_mode": True,
            "segmentation": segmentation,
            "split_metadata": split_metadata,
            "condition_candidates": _public_conditions(candidates_by_side),
            "rule_candidates": {
                "results_by_sharpe": rule_by_sharpe[:10],
                "results_by_return": rule_by_return[:10],
                "evaluated_pairs": len(rule_candidates),
                "shortlist_per_board": RULE_SHORTLIST_PER_BOARD,
                "selected_rule_count": len(selected_rule_pairs),
            },
            "results_by_sharpe": final_board(ranked_sharpe),
            "results_by_return": final_board(ranked_return),
            "full_backtests": full_backtests,
            "candidate_count": len(unique_risk_candidates),
            "risk_errors": [
                str(item["error"]) for item in risk_candidates if isinstance(item.get("error"), str)
            ],
            "stage_durations_seconds": {
                **stage_durations,
                "final_replays": time.perf_counter() - stage_started,
                "total": time.perf_counter() - run_started,
            },
            "risk_search_runs": risk_search_runs,
            "search_policy": search_policy_settings()
            if config.search_policy == SEARCH_POLICY_ID
            else {"id": "legacy"},
            "condition_counts": {side: len(items) for side, items in candidates_by_side.items()},
            "risk_rule_sets": [str(item["candidate_id"]) for item in selected_rule_pairs],
            "risk_trials_per_rule_set": config.risk_trials,
            "searched_value_ids": list(config.value_ids),
            "searched_signal_ids": list(config.signal_ids),
            "data_fingerprint": data_fingerprint,
            "market_fingerprint": prepared.data_fingerprint,
            "configuration_fingerprint": _configuration_fingerprint(config),
            "limitations": [
                "个股条件依据单只股票的历史时间序列挖掘，不代表可泛化到其他股票。",
                "留出表现只复核最终候选，仍是历史模拟结果。",
            ],
        }
        if not unique_risk_candidates:
            failures = final_result["risk_errors"]
            detail = str(failures[0]) if isinstance(failures, list) and failures else ""
            final_result["reason"] = (
                f"没有可计算的退出风控候选：{detail}"
                if detail
                else "没有可计算的退出风控候选；可扩大历史区间或放宽指标范围。"
            )
        self._report(progress_callback, 100, "完成", "个股研究完成")
        return final_result

    def _prepare(self, config: IndividualResearchConfig, ref: StockRef) -> PreparedBacktest:
        signals_rule = (
            RuleGroup(signals=config.signal_ids, combine_mode="any") if config.signal_ids else None
        )
        backtest_config = BacktestConfig(
            market=config.market,
            code=config.code,
            vipdoc_path="",
            buy_rule=signals_rule,
            always_in_market=True,
            close_at_end=True,
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
            instrument_type="stock",
            board=ref.board,
        )
        return self._backtest_service.prepare_for_ref(
            backtest_config,
            ref,
            feature_ids=config.value_ids,
        )

    def _research_window(
        self,
        prepared: PreparedBacktest,
        config: IndividualResearchConfig,
    ) -> tuple[
        np.ndarray,
        pd.DataFrame,
        dict[str, np.ndarray],
        dict[str, np.ndarray],
        list[SwingSegment],
    ]:
        frame = prepared.frame
        frame_dates = pd.to_datetime(frame["date"], errors="coerce").dt.date
        start_rows = np.flatnonzero((frame_dates >= config.start_date).to_numpy())
        end_rows = np.flatnonzero((frame_dates <= config.end_date).to_numpy())
        if not start_rows.size or not end_rows.size:
            raise ValueError("所选股票在研究区间内没有日线数据")
        window_start = int(start_rows[0])
        window_end = int(end_rows[-1]) + 1
        if window_start >= window_end:
            raise ValueError("所选股票在研究区间内没有连续可用日线")
        warm_start = max(0, window_start - config.atr_window * 3 - 10)
        calculation_frame = frame.iloc[warm_start:window_end].reset_index(drop=True)
        segments = split_swings(
            calculation_frame,
            reversal_multiple=config.reversal_multiple,
            atr_window=config.atr_window,
            include_pending=False,
        )
        labels_all = bar_labels(
            calculation_frame,
            segments,
            min_bars=config.min_bars,
            horizon=config.horizon,
        )
        offset = window_start - warm_start
        length = window_end - window_start
        labels = {
            label: np.asarray(values[offset : offset + length], dtype=float)
            for label, values in labels_all.items()
        }
        features = prepared.feature_values.iloc[window_start:window_end].reset_index(drop=True)
        features = features[
            ["date", *[value for value in config.value_ids if value in features.columns]]
        ]
        if len(features) != length:
            raise ValueError("指标数据与行情日期无法对齐，请重试")
        signal_values: dict[str, np.ndarray] = {}
        for signal_id in config.signal_ids:
            formula_id, _, signal_name = signal_id.partition(".")
            formula_result = prepared.formula_results.get(formula_id)
            values = formula_result.signals.get(signal_name) if formula_result is not None else None
            if values is None:
                continue
            signal_values[signal_id] = np.asarray(values, dtype=bool)[window_start:window_end]
        dates = pd.to_datetime(features["date"], errors="coerce").to_numpy()
        return dates, features.drop(columns=["date"]), labels, signal_values, segments

    def _mine_conditions(
        self,
        config: IndividualResearchConfig,
        features: pd.DataFrame,
        labels: Mapping[str, np.ndarray],
        signals: Mapping[str, np.ndarray],
        train_slice: slice,
    ) -> dict[str, list[dict[str, object]]]:
        result: dict[str, list[dict[str, object]]] = {"buy": [], "sell": []}
        for side, label_id in (("buy", "up_onset_ahead"), ("sell", "down_onset_ahead")):
            outcomes = labels[label_id][train_slice]
            side_features = features.iloc[train_slice].reset_index(drop=True)
            valid = np.isfinite(outcomes)
            if int(valid.sum()) < 5:
                continue
            x = side_features.loc[valid].reset_index(drop=True)
            y = outcomes[valid] > 0.5
            row_indices = np.flatnonzero(valid)
            groups = (row_indices // max(1, config.horizon)).astype(str)
            previous = side_features.shift(1).iloc[valid].reset_index(drop=True)
            mined = mine_binary_conditions(
                x,
                y,
                [config.symbol] * len(y),
                sample_groups=groups,
                min_support=5,
                min_instruments=1,
                limit=config.candidate_limit * 4,
                permutations=0,
                duplicate_correlation=0.9,
                previous_features=previous,
                allow_price_scale_features=True,
            )
            candidates = [
                _condition_candidate(side, index, item) for index, item in enumerate(mined, start=1)
            ]
            candidate_masks = [_condition_mask(item.condition, x, previous) for item in mined]
            baseline_rate = float(np.mean(y)) if len(y) else 0.0
            for signal_id, signal_values in signals.items():
                signal_train = np.asarray(signal_values[train_slice], dtype=bool)[valid]
                support = int(signal_train.sum())
                if support < 5 or support == len(y) or baseline_rate <= 0 or baseline_rate >= 1:
                    continue
                outcome_rate = float(np.mean(y[signal_train]))
                if outcome_rate <= baseline_rate:
                    continue
                rule = RuleGroup(signals=(signal_id,), combine_mode="all")
                candidates.append(
                    {
                        "candidate_id": f"{side}-signal-{signal_id}",
                        "side": side,
                        "kind": "signal",
                        "display": signal_id,
                        "support_count": support,
                        "outcome_rate": outcome_rate,
                        "baseline_rate": baseline_rate,
                        "lift": outcome_rate / baseline_rate,
                        "rule": rule,
                    }
                )
                candidate_masks.append(signal_train)
            candidates.extend(
                _combine_condition_candidates(
                    side,
                    candidates,
                    candidate_masks,
                    y,
                    groups,
                    candidate_limit=config.candidate_limit,
                )
            )
            trigger_masks: dict[str, np.ndarray] = {}
            for candidate in candidates:
                rule = cast(RuleGroup, candidate["rule"])
                parts = [
                    np.asarray(signals[signal][train_slice], dtype=bool)[valid]
                    for signal in rule.signals
                ]
                parts.extend(
                    _condition_mask(condition, x, previous) for condition in rule.conditions
                )
                if parts:
                    trigger_masks[_rule_signature(rule)] = (
                        np.logical_and.reduce(parts)
                        if rule.combine_mode == "all"
                        else np.logical_or.reduce(parts)
                    )
            result[side] = _select_condition_candidates(
                candidates,
                config.candidate_limit,
                trigger_masks=trigger_masks if config.search_policy == SEARCH_POLICY_ID else None,
            )
        return result

    def _rule_pair_metrics(
        self,
        config: IndividualResearchConfig,
        ref: StockRef,
        pairs: Sequence[tuple[str, dict[str, object], dict[str, object]]],
        train: Mapping[str, date],
        validation: Mapping[str, date],
        abort_check: AbortCheck | None,
    ) -> Generator[tuple[dict[str, object], dict[str, object]], None, None]:
        if (
            self._validation_database_path is not None
            and self._evaluation_worker_budget is not None
        ):
            from .parallel_validation import RulePairJob, config_payload, evaluate_rule_pairs

            def jobs() -> Iterator[RulePairJob]:
                for _pair_id, buy, sell in pairs:
                    buy_rule = cast(RuleGroup, buy["rule"])
                    sell_rule = cast(RuleGroup, sell["rule"])
                    yield (
                        ref,
                        config_payload(
                            _portfolio_config(
                                config, buy_rule, sell_rule, train["start"], train["end"]
                            )
                        ),
                        config_payload(
                            _portfolio_config(
                                config, buy_rule, sell_rule, validation["start"], validation["end"]
                            )
                        ),
                    )

            yield from evaluate_rule_pairs(
                self._validation_database_path,
                jobs(),
                budget=self._evaluation_worker_budget,
                requested_workers=self._validation_workers,
                abort_check=abort_check,
            )
            return
        for _pair_id, buy, sell in pairs:
            self._check_cancelled(abort_check)
            buy_rule = cast(RuleGroup, buy["rule"])
            sell_rule = cast(RuleGroup, sell["rule"])
            yield (
                self._backtest_metrics(
                    config, ref, buy_rule, sell_rule, train["start"], train["end"]
                ),
                self._backtest_metrics(
                    config, ref, buy_rule, sell_rule, validation["start"], validation["end"]
                ),
            )

    def _backtest_metrics(
        self,
        config: IndividualResearchConfig,
        ref: StockRef,
        buy_rule: RuleGroup,
        sell_rule: RuleGroup,
        start_date: date,
        end_date: date,
    ) -> dict[str, object]:
        report = self._portfolio_service.run_stock(
            ref,
            _portfolio_config(config, buy_rule, sell_rule, start_date, end_date),
        )
        return _report_metrics(report)

    def _risk_base_payload(
        self,
        config: IndividualResearchConfig,
        buy_rule: RuleGroup,
        sell_rule: RuleGroup,
    ) -> dict[str, object]:
        payload = {
            "vipdoc_path": "",
            "universe": "codes",
            "codes": [f"{config.market}:{config.code}"],
            "instrument_types": ["stock"],
            "boards": [],
            "selected_signals": [],
            "combine_mode": "all",
            "minimum_matches": None,
            "buy_rule": rule_to_dict(buy_rule),
            "ranking_value": None,
            "single_security_mode": True,
            "rank_order": "desc",
            "max_positions": 1,
            "rebalance_frequency": "daily",
            "formula_text": None,
            "formula_parameters": {},
            "sell_signal": None,
            "stop_loss_pct": None,
            "take_profit_pct": None,
            "take_profit_mode": "pct",
            "take_profit_atr_multiplier": None,
            "take_profit_cooldown_days": 0,
            "stop_loss_mode": "pct",
            "stop_loss_atr_multiplier": None,
            "stop_loss_cooldown_days": 0,
            "trailing_stop_pct": None,
            "trailing_stop_mode": "pct",
            "trailing_stop_atr_multiplier": None,
            "trailing_stop_cooldown_days": 0,
            "atr_period": 14,
            "sell_value": None,
            "sell_value_operator": None,
            "sell_value_threshold": None,
            "compare_left_value": None,
            "compare_operator": None,
            "compare_right_value": None,
            "sell_rule": rule_to_dict(sell_rule),
            "date_mode": "custom",
            "start_date": config.start_date.isoformat(),
            "end_date": config.end_date.isoformat(),
            "initial_cash": config.initial_cash,
            "commission": config.commission,
            "min_commission": config.min_commission,
            "stamp_tax": config.stamp_tax,
            "slippage": config.slippage,
            "execution": config.execution,
            "fitness_rank_weight": 0.0,
            "fitness_min_trades": 5,
            "fitness_max_drawdown": 0.3,
            "position_mode": config.position_mode,
            "fixed_size": config.fixed_size,
            "exclude_st": False,
        }
        return cast(
            dict[str, object],
            PortfolioBacktestRequest.model_validate(payload).model_dump(mode="json"),
        )

    def _risk_discovery_request(
        self,
        base_config: Mapping[str, object],
        config: IndividualResearchConfig,
        windows: Mapping[str, Mapping[str, date] | None],
        *,
        seed: int,
    ) -> DiscoveryRequest:
        split_payload: dict[str, dict[str, str]] = {}
        for phase in ("search", "validation", "holdout"):
            window = windows.get("train" if phase == "search" else phase)
            if window is None:
                raise ValueError(f"{phase} 区间没有足够的交易日")
            split_payload[phase] = {
                "start_date": window["start"].isoformat(),
                "end_date": window["end"].isoformat(),
            }
        return DiscoveryRequest(
            base_config=dict(base_config),
            splits=split_payload,
            max_trials=config.risk_trials,
            timeout_seconds=7_200,
            validation_candidates=50,
            holdout_candidates=0,
            min_trades=0,
            max_drawdown=None,
            max_complexity=3,
            multifidelity_enabled=True,
            convergence_enabled=config.search_policy == SEARCH_POLICY_ID,
            research_search_policy=config.search_policy,
            seed=seed,
            candidate_mode="risk_only",
        )

    def _backtest_candidate_metrics(
        self,
        raw_config: Mapping[str, object],
        start_date: date,
        end_date: date,
    ) -> dict[str, object]:
        return _report_metrics(self._backtest_candidate_report(raw_config, start_date, end_date))

    def full_period_metrics(
        self,
        candidate: Mapping[str, object],
        start_date: date,
        end_date: date,
    ) -> dict[str, object]:
        """Replay a selected candidate over the complete research interval."""

        config = candidate.get("config")
        if not isinstance(config, Mapping):
            raise ValueError("策略候选缺少完整回测配置")
        return self._backtest_candidate_metrics(config, start_date, end_date)

    def _backtest_candidate_detail(
        self,
        raw_config: Mapping[str, object],
        start_date: date,
        end_date: date,
    ) -> dict[str, object]:
        report = self._backtest_candidate_report(raw_config, start_date, end_date)
        benchmark_source = report.benchmark
        benchmark_metrics_source = (
            benchmark_source.get("performance") if benchmark_source is not None else None
        )
        benchmark_metrics = (
            dict(benchmark_metrics_source) if isinstance(benchmark_metrics_source, Mapping) else {}
        )
        benchmark_return = _number(benchmark_metrics.get("total_return"))
        strategy_return = _number(report.performance.get("total_return"))
        excess_return = (
            strategy_return - benchmark_return
            if strategy_return is not None and benchmark_return is not None
            else None
        )
        benchmark = (
            {
                "label": str(benchmark_source.get("label") or "个股买入持有"),
                "instrument_count": _integer(benchmark_source.get("instrument_count")),
                "total_return": benchmark_return,
                "annual_return": _number(benchmark_metrics.get("annual_return")),
                "max_drawdown": _number(benchmark_metrics.get("max_drawdown")),
            }
            if benchmark_source is not None
            else None
        )
        return {
            "start_date": report.start_date,
            "end_date": report.end_date,
            "bars": report.bars,
            "closed_trades": sum(
                1
                for trade in report.trades
                if trade.get("direction") == "SELL" and not bool(trade.get("rejected", False))
            ),
            "performance": dict(report.performance),
            "benchmark": benchmark,
            "benchmark_return": benchmark_return,
            "excess_return": excess_return,
            "equity_curve": [dict(point) for point in report.equity_curve],
            "trades": [dict(trade) for trade in report.trades],
            "data_fingerprint": report.data_fingerprint,
        }

    def _backtest_candidate_report(
        self,
        raw_config: Mapping[str, object],
        start_date: date,
        end_date: date,
    ) -> PortfolioBacktestReport:
        payload = dict(raw_config)
        payload["date_mode"] = "custom"
        payload["start_date"] = start_date.isoformat()
        payload["end_date"] = end_date.isoformat()
        request = PortfolioBacktestRequest.model_validate(payload)
        config = request.to_config()
        if not config.codes or len(config.codes) != 1:
            raise ValueError("候选策略没有绑定到唯一股票")
        market_code = config.codes[0]
        market, code = market_code.split(":", 1) if ":" in market_code else ("SH", market_code)
        ref = self._adapter.stock_ref(config.vipdoc_path, cast(MarketCode, market), code)
        return self._portfolio_service.run_stock(ref, config)

    @staticmethod
    def _risk_progress(
        progress_callback: ProgressCallback | None,
        pair_index: int,
        pair_count: int,
        pair_id: str,
        pair: Mapping[str, object] | None = None,
    ) -> Callable[..., None] | None:
        if progress_callback is None:
            return None

        def report(
            current: int, total: int, stage: str, _details: Mapping[str, object] | None = None
        ) -> None:
            overall = _details.get("overall_progress") if _details is not None else None
            fraction = (
                min(1.0, max(0.0, float(overall)))
                if isinstance(overall, (int, float))
                else (min(1.0, max(0.0, current / total if total else 1.0)))
            )
            stage_label = {
                "preparing_snapshot": "准备行情与指标快照",
                "searching": "搜索止损、止盈和冷却参数",
                "converged": "提前收敛，继续精筛",
                "refining_medium": "中等精度复核候选",
                "refining_full": "完整精度复核候选",
                "validation": "验证区间回测",
                "holdout": "留出区间复核",
                "completed": "本组寻优完成",
            }.get(stage, stage)
            count_label = "已尝试" if stage in {"searching", "converged"} else "阶段进度"
            percent = 45 + int(33 * ((pair_index - 1 + fraction) / max(pair_count, 1)))
            IndividualStockResearchService._report(
                progress_callback,
                percent,
                "止盈止损寻优",
                f"规则 {pair_index}/{pair_count} · {pair_id[:8]} · {stage_label}"
                f" · {count_label} {current}/{total}",
                work_details={
                    "stage": stage_label,
                    "current": current,
                    "total": total,
                    "rule_index": pair_index,
                    "rule_count": pair_count,
                    "buy_rule": pair.get("buy_rule") if pair else None,
                    "sell_rule": pair.get("sell_rule") if pair else None,
                    "elapsed_seconds": _details.get("elapsed_seconds") if _details else None,
                    "best_sharpe": _details.get("best_sharpe") if _details else None,
                    "best_return": _details.get("best_return") if _details else None,
                },
            )

        return report

    @staticmethod
    def _report(
        progress_callback: ProgressCallback | None,
        percent: int,
        phase: str,
        message: str,
        *,
        work_details: Mapping[str, object] | None = None,
    ) -> None:
        if progress_callback is None:
            return
        from selector_app.screening.jobs import report_progress_detail

        bounded = min(100, max(0, percent))
        report_progress_detail(
            progress_callback,
            {
                "phase": phase,
                "phase_label": message,
                "overall_progress": bounded / 100.0,
                "work_details": dict(work_details) if work_details else {},
            },
        )
        progress_callback(bounded, 100)

    @staticmethod
    def _check_cancelled(abort_check: AbortCheck | None) -> None:
        if abort_check is not None and abort_check():
            raise TaskCancelledError("个股研究已取消")

    @staticmethod
    def _empty_result(
        config: IndividualResearchConfig,
        ref: StockRef,
        data_fingerprint: str,
        segmentation: Mapping[str, object],
        split_metadata: Mapping[str, object],
        condition_candidates: Mapping[str, object],
        reason: str,
        *,
        trading_days: int,
        rule_candidates: Sequence[Mapping[str, object]] = (),
    ) -> dict[str, object]:
        return {
            "market": config.market,
            "code": config.code,
            "name": ref.name or config.name,
            "start_date": config.start_date.isoformat(),
            "end_date": config.end_date.isoformat(),
            "date_range": {
                "start": config.start_date.isoformat(),
                "end": config.end_date.isoformat(),
                "days": trading_days,
            },
            "single_security_mode": True,
            "segmentation": dict(segmentation),
            "split_metadata": dict(split_metadata),
            "condition_candidates": dict(condition_candidates),
            "rule_candidates": {
                "results_by_sharpe": [],
                "results_by_return": list(rule_candidates),
            },
            "results_by_sharpe": [],
            "results_by_return": [],
            "candidate_count": 0,
            "risk_trials_per_rule_set": config.risk_trials,
            "data_fingerprint": data_fingerprint,
            "market_fingerprint": data_fingerprint,
            "configuration_fingerprint": _configuration_fingerprint(config),
            "reason": reason,
        }


def _condition_candidate(side: str, index: int, candidate: MinedCondition) -> dict[str, object]:
    condition = candidate.condition
    rule = RuleGroup(conditions=(condition,), combine_mode="all")
    display = _condition_text(condition)
    return {
        "candidate_id": f"{side}-condition-{index}-{_short_hash(display)}",
        "side": side,
        "kind": "condition",
        "display": display,
        "support_count": candidate.support_count,
        "outcome_rate": candidate.outcome_rate,
        "baseline_rate": candidate.baseline_rate,
        "lift": candidate.lift,
        "rule": rule,
    }


def _condition_mask(
    condition: ScreenCondition,
    features: pd.DataFrame,
    previous_features: pd.DataFrame,
) -> np.ndarray:
    """Evaluate one mined condition on the same training rows it was scored on."""

    size = len(features)
    if condition.left_value not in features.columns:
        return np.zeros(size, dtype=bool)
    left = pd.to_numeric(features[condition.left_value], errors="coerce").to_numpy(dtype=float)
    valid = np.isfinite(left)
    if condition.right_value is not None:
        if condition.right_value not in features.columns:
            return np.zeros(size, dtype=bool)
        right = pd.to_numeric(features[condition.right_value], errors="coerce").to_numpy(
            dtype=float
        )
        valid &= np.isfinite(right)
    elif condition.right_constant is not None:
        right = np.full(size, condition.right_constant, dtype=float)
    else:
        return np.zeros(size, dtype=bool)

    if condition.operator in {"cross_up", "cross_down"}:
        if condition.left_value not in previous_features.columns:
            return np.zeros(size, dtype=bool)
        previous_left = pd.to_numeric(
            previous_features[condition.left_value], errors="coerce"
        ).to_numpy(dtype=float)
        if condition.right_value is not None:
            if condition.right_value not in previous_features.columns:
                return np.zeros(size, dtype=bool)
            previous_right = pd.to_numeric(
                previous_features[condition.right_value], errors="coerce"
            ).to_numpy(dtype=float)
        else:
            previous_right = right
        valid &= np.isfinite(previous_left) & np.isfinite(previous_right)
        crossed = (
            (previous_left <= previous_right) & (left > right)
            if condition.operator == "cross_up"
            else (previous_left >= previous_right) & (left < right)
        )
        return np.asarray(crossed & valid, dtype=bool)

    operators = {
        "gt": np.greater,
        "gte": np.greater_equal,
        "lt": np.less,
        "lte": np.less_equal,
        "eq": np.equal,
        "neq": np.not_equal,
    }
    comparison = operators.get(str(condition.operator))
    if comparison is None:
        return np.zeros(size, dtype=bool)
    return np.asarray(comparison(left, right) & valid, dtype=bool)


def _single_component_id(candidate: Mapping[str, object]) -> str | None:
    rule = candidate.get("rule")
    if not isinstance(rule, RuleGroup) or rule.required_conditions:
        return None
    if len(rule.signals) + len(rule.conditions) != 1:
        return None
    if rule.signals:
        return f"signal:{rule.signals[0]}"
    condition = rule.conditions[0]
    if condition.right_value is not None or condition.right_constant is None:
        return None
    return f"value:{condition.left_value}"


def _combine_condition_candidates(
    side: str,
    candidates: Sequence[dict[str, object]],
    masks: Sequence[np.ndarray],
    outcomes: np.ndarray,
    groups: np.ndarray,
    *,
    candidate_limit: int,
) -> list[dict[str, object]]:
    """Mine bounded two-component AND/OR rules from training-only candidates."""

    if len(candidates) != len(masks) or not candidates:
        return []
    group_codes = pd.factorize(groups, sort=False)[0]
    group_sizes = np.bincount(group_codes)
    nonempty_groups = group_sizes > 0
    if not nonempty_groups.any():
        return []
    group_outcomes = np.bincount(group_codes, weights=outcomes.astype(float))
    baseline_rate = float(np.mean(group_outcomes[nonempty_groups] / group_sizes[nonempty_groups]))
    if not 0.0 < baseline_rate < 1.0:
        return []

    component_limit = min(MAX_COMBINATION_COMPONENTS, max(2, candidate_limit * 2))
    ranked_components = sorted(
        zip(candidates, masks, strict=True),
        key=lambda pair: (
            -(_number(pair[0].get("lift")) or 0.0),
            -_integer(pair[0].get("support_count")),
            str(pair[0].get("display") or ""),
        ),
    )
    components: list[tuple[dict[str, object], np.ndarray, str]] = []
    per_component_count: dict[str, int] = {}
    for candidate, mask in ranked_components:
        component_id = _single_component_id(candidate)
        if component_id is None:
            continue
        if per_component_count.get(component_id, 0) >= MAX_CANDIDATES_PER_COMPONENT:
            continue
        components.append((candidate, np.asarray(mask, dtype=bool), component_id))
        per_component_count[component_id] = per_component_count.get(component_id, 0) + 1
        if len(components) >= component_limit:
            break

    combined: list[dict[str, object]] = []
    seen_signatures: set[str] = set()
    for (left, left_mask, left_component), (right, right_mask, right_component) in combinations(
        components, 2
    ):
        if left_component == right_component:
            continue
        left_rule = left.get("rule")
        right_rule = right.get("rule")
        if not isinstance(left_rule, RuleGroup) or not isinstance(right_rule, RuleGroup):
            continue
        for combine_mode in ("all", "any"):
            matched_mask = (
                left_mask & right_mask if combine_mode == "all" else left_mask | right_mask
            )
            if not matched_mask.any():
                continue
            matching_rows = np.flatnonzero(matched_mask)
            _, first_positions = np.unique(group_codes[matching_rows], return_index=True)
            matched_rows = np.sort(matching_rows[first_positions])
            support_count = int(matched_rows.size)
            if support_count < 5 or support_count >= int(nonempty_groups.sum()):
                continue
            outcome_rate = float(np.mean(outcomes[matched_rows]))
            if outcome_rate <= baseline_rate:
                continue
            rule = RuleGroup(
                signals=tuple(dict.fromkeys((*left_rule.signals, *right_rule.signals))),
                conditions=(*left_rule.conditions, *right_rule.conditions),
                combine_mode=combine_mode,
            )
            signature = _rule_signature(rule)
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            separator = " 且 " if combine_mode == "all" else " 或 "
            display = f"{left.get('display', '')}{separator}{right.get('display', '')}"
            combined.append(
                {
                    "candidate_id": f"{side}-combo-{_short_hash(signature)}",
                    "side": side,
                    "kind": "condition_combo",
                    "display": display,
                    "support_count": support_count,
                    "outcome_rate": outcome_rate,
                    "baseline_rate": baseline_rate,
                    "lift": outcome_rate / baseline_rate,
                    "rule": rule,
                }
            )
    return combined


def _select_condition_candidates(
    candidates: Sequence[dict[str, object]],
    limit: int,
    *,
    trigger_masks: Mapping[str, np.ndarray] | None = None,
) -> list[dict[str, object]]:
    """Keep both atomic and two-component rules in the train-ranked pool."""

    ranked = sorted(
        candidates,
        key=lambda item: (
            -(_number(item.get("lift")) or 0.0),
            -_integer(item.get("support_count")),
            str(item.get("display") or ""),
        ),
    )
    unique: list[dict[str, object]] = []
    seen: set[str] = set()
    for candidate in ranked:
        rule = candidate.get("rule")
        if not isinstance(rule, RuleGroup):
            continue
        signature = _rule_signature(rule)
        if signature in seen:
            continue
        mask = trigger_masks.get(signature) if trigger_masks is not None else None
        if mask is not None and any(
            _similar_triggers(mask, trigger_masks[_rule_signature(cast(RuleGroup, prior["rule"]))])
            for prior in unique
            if trigger_masks is not None
            and _rule_signature(cast(RuleGroup, prior["rule"])) in trigger_masks
        ):
            continue
        seen.add(signature)
        unique.append(candidate)

    atomic_slots = (limit + 1) // 2
    combo_slots = limit // 2
    selected = [
        *[item for item in unique if item.get("kind") != "condition_combo"][:atomic_slots],
        *[item for item in unique if item.get("kind") == "condition_combo"][:combo_slots],
    ]
    selected_signatures = {_rule_signature(cast(RuleGroup, item["rule"])) for item in selected}
    for item in unique:
        if len(selected) >= limit:
            break
        signature = _rule_signature(cast(RuleGroup, item["rule"]))
        if signature in selected_signatures:
            continue
        selected.append(item)
        selected_signatures.add(signature)
        if len(selected) >= limit:
            break
    return sorted(
        selected,
        key=lambda item: (
            -(_number(item.get("lift")) or 0.0),
            -_integer(item.get("support_count")),
            str(item.get("display") or ""),
        ),
    )


def _public_conditions(
    candidates: Mapping[str, Sequence[dict[str, object]]],
) -> dict[str, list[dict[str, object]]]:
    return {
        side: [_public_rule_candidate(candidate) for candidate in items]
        for side, items in candidates.items()
    }


def _condition_text(condition: ScreenCondition) -> str:
    symbols = {
        "gt": ">",
        "gte": "≥",
        "lt": "<",
        "lte": "≤",
        "cross_up": "上穿",
        "cross_down": "下穿",
        "eq": "=",
        "neq": "≠",
    }
    right = condition.right_value if condition.right_value is not None else condition.right_constant
    return f"{condition.left_value} {symbols.get(condition.operator, condition.operator)} {right}"


def _public_rule_candidate(candidate: Mapping[str, object]) -> dict[str, object]:
    rule = candidate.get("rule")
    return {key: value for key, value in candidate.items() if key != "rule"} | {
        "rule": rule_to_dict(rule) if isinstance(rule, RuleGroup) else None
    }


def _individual_risk_candidate(
    candidate: Mapping[str, object],
    rule_pair: Mapping[str, object],
) -> dict[str, object]:
    raw_metrics = candidate.get("metrics")
    metrics = dict(raw_metrics) if isinstance(raw_metrics, Mapping) else {}
    metrics["holdout"] = None
    config = candidate.get("config")
    candidate_hash = str(candidate.get("candidate_hash") or "")
    if not candidate_hash and isinstance(config, Mapping):
        candidate_hash = _short_hash(
            json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
    validation = metrics.get("validation")
    training = metrics.get("train")
    data_fingerprint = candidate.get("data_fingerprint")
    if not isinstance(data_fingerprint, str):
        data_fingerprint = (
            validation.get("data_fingerprint") if isinstance(validation, Mapping) else None
        )
    if not isinstance(data_fingerprint, str):
        data_fingerprint = (
            training.get("data_fingerprint") if isinstance(training, Mapping) else None
        )
    return {
        "candidate_hash": candidate_hash,
        "candidate_id": f"{rule_pair.get('candidate_id')}:{candidate_hash[:12]}",
        "condition_candidate_id": rule_pair.get("candidate_id"),
        "human_readable_rules": candidate.get("human_readable_rules"),
        "condition_rules": {
            "buy": rule_pair.get("buy_rule"),
            "sell": rule_pair.get("sell_rule"),
        },
        "config": dict(config) if isinstance(config, Mapping) else {},
        "train": metrics.get("train"),
        "validation": metrics.get("validation"),
        "metrics": metrics,
        "data_fingerprint": data_fingerprint,
    }


def _rule_signature(rule: RuleGroup) -> str:
    return json.dumps(rule_to_dict(rule), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse_rule(value: object) -> RuleGroup | None:
    if not isinstance(value, Mapping):
        return None
    signals = tuple(str(item) for item in value.get("signals", ()) if isinstance(item, str))
    conditions: list[ScreenCondition] = []
    for item in (
        value.get("conditions", ()) if isinstance(value.get("conditions"), (list, tuple)) else ()
    ):
        if not isinstance(item, Mapping):
            continue
        operator = str(item.get("operator") or "")
        left_value = str(item.get("left_value") or "")
        if not left_value or operator not in {
            "gt",
            "gte",
            "lt",
            "lte",
            "eq",
            "neq",
            "cross_up",
            "cross_down",
        }:
            continue
        right_value = item.get("right_value")
        right_constant = item.get("right_constant")
        conditions.append(
            ScreenCondition(
                left_value=left_value,
                operator=operator,
                right_value=str(right_value) if right_value is not None else None,
                right_constant=float(right_constant)
                if isinstance(right_constant, (int, float))
                else None,
            )
        )
    if not signals and not conditions:
        return None
    return RuleGroup(
        signals=signals,
        conditions=tuple(conditions),
        combine_mode="any" if value.get("combine_mode") == "any" else "all",
    )


def _rule_pairs(
    buy_candidates: Sequence[dict[str, object]],
    sell_candidates: Sequence[dict[str, object]],
) -> list[tuple[str, dict[str, object], dict[str, object]]]:
    pairs: list[tuple[str, dict[str, object], dict[str, object]]] = []
    seen: set[str] = set()
    for buy in buy_candidates:
        buy_rule = buy.get("rule")
        if not isinstance(buy_rule, RuleGroup):
            continue
        for sell in sell_candidates:
            sell_rule = sell.get("rule")
            if not isinstance(sell_rule, RuleGroup):
                continue
            signature = f"{_rule_signature(buy_rule)}|{_rule_signature(sell_rule)}"
            if signature in seen:
                continue
            seen.add(signature)
            pairs.append((f"rules-{_short_hash(signature)}", buy, sell))
    return pairs


def _portfolio_config(
    config: IndividualResearchConfig,
    buy_rule: RuleGroup,
    sell_rule: RuleGroup,
    start_date: date,
    end_date: date,
) -> PortfolioBacktestConfig:
    return PortfolioBacktestConfig(
        vipdoc_path="",
        universe="codes",
        codes=(f"{config.market}:{config.code}",),
        selected_signals=(),
        combine_mode="all",
        minimum_matches=None,
        ranking_value=SINGLE_SECURITY_RANKING_VALUE,
        max_positions=1,
        instrument_types=("stock",),
        boards=(),
        single_security_mode=True,
        buy_rule=buy_rule,
        sell_rule=sell_rule,
        start_date=start_date,
        end_date=end_date,
        initial_cash=config.initial_cash,
        commission=config.commission,
        min_commission=config.min_commission,
        stamp_tax=config.stamp_tax,
        slippage=config.slippage,
        execution=config.execution,
        position_mode=config.position_mode,
        fixed_size=config.fixed_size,
        atr_period=14,
    )


def _report_metrics(report: PortfolioBacktestReport) -> dict[str, object]:
    performance = dict(report.performance)
    closed_trades = sum(
        1
        for trade in report.trades
        if trade.get("direction") == "SELL" and not bool(trade.get("rejected", False))
    )
    return {
        "total_return": _number(performance.get("total_return")),
        "annual_return": _number(performance.get("annual_return")),
        "sharpe": _number(performance.get("sharpe")),
        "max_drawdown": _number(performance.get("max_drawdown")),
        "win_rate": _number(performance.get("win_rate")),
        "payoff_ratio": _number(performance.get("payoff_ratio")),
        "profit_factor": _number(performance.get("profit_factor")),
        "total_trades": _integer(performance.get("total_trades"), closed_trades),
        "closed_trades": closed_trades,
        "data_fingerprint": report.data_fingerprint,
    }


def _rank_candidates(
    candidates: Sequence[dict[str, object]],
    primary: str,
) -> list[dict[str, object]]:
    secondary = "annual_return" if primary == "sharpe" else "sharpe"

    def validation_metrics(item: Mapping[str, object]) -> Mapping[str, object]:
        direct = _mapping(item.get("validation"))
        if direct:
            return direct
        return _mapping(_mapping(item.get("metrics")).get("validation"))

    def sort_key(item: dict[str, object]) -> tuple[bool, float, bool, float, str]:
        metrics = validation_metrics(item)
        primary_value = _number(metrics.get(primary))
        secondary_value = _number(metrics.get(secondary))
        return (
            primary_value is None,
            -(primary_value if primary_value is not None else 0.0),
            secondary_value is None,
            -(secondary_value if secondary_value is not None else 0.0),
            str(item.get("candidate_id") or item.get("candidate_hash") or ""),
        )

    return sorted(candidates, key=sort_key)


def _select_rule_pairs(
    candidates: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    sharpe_board = _rank_candidates(candidates, "sharpe")
    return_board = _rank_candidates(candidates, "annual_return")
    return _unique_candidates(
        [
            *sharpe_board[:RULE_SHORTLIST_PER_BOARD],
            *return_board[:RULE_SHORTLIST_PER_BOARD],
        ],
        key="candidate_id",
    )


def _unique_candidates(
    candidates: Sequence[dict[str, object]],
    *,
    key: str,
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in candidates:
        value = str(item.get(key) or "")
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(item)
    return result


def _split_windows(
    dates: np.ndarray,
    slices: Mapping[str, slice],
) -> dict[str, dict[str, date] | None]:
    result: dict[str, dict[str, date] | None] = {}
    for phase in ("train", "validation", "holdout"):
        current = slices[phase]
        start = current.start or 0
        stop = current.stop if current.stop is not None else len(dates)
        if stop <= start:
            result[phase] = None
            continue
        result[phase] = {
            "start": pd.Timestamp(dates[start]).date(),
            "end": pd.Timestamp(dates[stop - 1]).date(),
        }
    return result


def _segmentation_summary(
    segments: Sequence[object], config: IndividualResearchConfig
) -> dict[str, object]:
    completed = [
        segment
        for segment in segments
        if not bool(getattr(segment, "pending", False))
        and int(getattr(segment, "duration", 0)) >= config.min_bars
    ]
    ups = [segment for segment in completed if getattr(segment, "direction", None) == "up"]
    downs = [segment for segment in completed if getattr(segment, "direction", None) == "down"]
    return {
        "reversal_multiple": config.reversal_multiple,
        "atr_window": config.atr_window,
        "min_bars": config.min_bars,
        "up_segments": len(ups),
        "down_segments": len(downs),
        "completed_segments": len(completed),
        "median_up_amplitude": _median([float(getattr(item, "amplitude", 0.0)) for item in ups]),
        "median_down_amplitude": _median(
            [abs(float(getattr(item, "amplitude", 0.0))) for item in downs]
        ),
        "median_duration": _median([float(getattr(item, "duration", 0)) for item in completed]),
    }


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _integer(value: object, fallback: int = 0) -> int:
    number = _number(value)
    return int(number) if number is not None else fallback


def _trade_count(metrics: Mapping[str, object]) -> int:
    closed = metrics.get("closed_trades")
    if isinstance(closed, (int, float)) and not isinstance(closed, bool):
        return max(0, int(closed))
    return max(0, _integer(metrics.get("total_trades")))


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _mapping_sequence(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _median(values: Sequence[float]) -> float | None:
    return float(np.median(values)) if values else None


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _stable_seed(config: IndividualResearchConfig, pair_id: str) -> int:
    text = f"{config.market}:{config.code}:{config.start_date}:{config.end_date}:{pair_id}"
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)


def _fingerprint_many(base: str, others: Sequence[str]) -> str:
    return hashlib.sha256("|".join([base, *sorted(others)]).encode("utf-8")).hexdigest()


def _configuration_fingerprint(config: IndividualResearchConfig) -> str:
    payload = {
        "market": config.market,
        "code": config.code,
        "start_date": config.start_date.isoformat(),
        "end_date": config.end_date.isoformat(),
        "value_ids": list(config.value_ids),
        "signal_ids": list(config.signal_ids),
        "reversal_multiple": config.reversal_multiple,
        "atr_window": config.atr_window,
        "min_bars": config.min_bars,
        "horizon": config.horizon,
        "candidate_limit": config.candidate_limit,
        "search_policy": search_policy_settings()
        if config.search_policy == SEARCH_POLICY_ID
        else {"id": "legacy"},
        "risk_trials": config.risk_trials,
        "split_windows": {
            phase: [start.isoformat(), end.isoformat()]
            for phase, (start, end) in (config.split_windows or {}).items()
        },
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


__all__ = ["IndividualStockResearchService"]


def _similar_triggers(left: np.ndarray, right: np.ndarray) -> bool:
    if np.array_equal(left, right):
        return True
    if left.size < 2 or np.all(left == left[0]) or np.all(right == right[0]):
        return False
    return bool(abs(float(np.corrcoef(left.astype(float), right.astype(float))[0, 1])) >= 0.9)
