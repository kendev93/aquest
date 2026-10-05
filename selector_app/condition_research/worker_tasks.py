"""Per-stock condition-research worker tasks."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import cast

from selector_app.backtest.models import BacktestConfig, BacktestReport
from selector_app.backtest.profiles import profile_from_overrides
from selector_app.backtest.service import (
    BacktestService,
    PreparedBacktest,
)
from selector_app.market_data.models import InstrumentType, StockRef
from selector_app.screening.jobs import TaskUserError
from selector_app.screening.models import ScreenCondition

from . import parallel
from .models import ConditionResearchConfig
from .parallel import (
    CandidateWindowOutcome,
    CandidateWindowPayload,
    HoldoutOutcome,
    HoldoutPayload,
    TrainOutcome,
    TrainPayload,
)
from .samples import (
    build_buy_trade_samples,
    build_early_exit_samples,
    feature_rows_by_date,
)

_COMPACT_TRADE_KEYS = ("direction", "pnl", "cost_basis", "rejected")


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
        # 没有基准卖出规则时让回测在期末按收盘价平仓，否则持仓永远不产生卖出交易，
        # 「提前离场」样本就无从构造。
        close_at_end=config.sell_rule is None,
        # 对偶：没有基准买入规则时让回测持续满仓，否则永远不入场，
        # 「盈利/亏损入场」样本同样无从构造。
        always_in_market=config.buy_rule is None,
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
    # TaskUserError 故意不在这里兜：行情在研究期间发生变化时，继续跑下去会让各阶段
    # 的数据口径不一致，设计上就是要整轮中止（test_service_aborts_if_market_data_
    # changes_between_time_split_passes 固定的行为）。
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
    # TaskUserError 故意不在这里兜：行情在研究期间发生变化时，继续跑下去会让各阶段
    # 的数据口径不一致，设计上就是要整轮中止（test_service_aborts_if_market_data_
    # changes_between_time_split_passes 固定的行为）。
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
    # TaskUserError 故意不在这里兜：行情在研究期间发生变化时，继续跑下去会让各阶段
    # 的数据口径不一致，设计上就是要整轮中止（test_service_aborts_if_market_data_
    # changes_between_time_split_passes 固定的行为）。
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


def _symbol_key(ref: StockRef) -> str:
    return f"{ref.market}{ref.code}"


def _reason(exc: Exception) -> str:
    text = str(exc).strip()
    return text[:200] if text else type(exc).__name__
