"""Full-range replay orchestration for the strategy laboratory."""

from __future__ import annotations

import multiprocessing
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import date
from typing import Protocol, cast

from selector_app.indicators.cache import IndicatorCache
from selector_app.market_data.adapter import DuckDbMarketDataAdapter, MarketDataAdapter
from selector_app.market_data.etfquest_adapter import ETFQuestMarketDataAdapter
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.portfolio_backtest.models import PortfolioBacktestConfig, PortfolioBacktestReport
from selector_app.portfolio_backtest.service import PortfolioBacktestService
from selector_app.screening.models import ScreenCondition
from selector_app.strategy_discovery.catalog import default_indicator_catalog
from selector_app.strategy_discovery.models import DiscoveryRequest
from selector_app.strategy_discovery.replay import (
    ReplayLeaderboard,
    StrategyReplayCandidate,
    StrategyReplayResult,
    build_replay_candidate,
)
from selector_app.strategy_discovery.resources import (
    resolve_discovery_workers,
    validate_discovery_workers,
)
from selector_app.strategy_discovery.snapshot import DiscoverySnapshot, build_discovery_snapshot
from selector_app.web.schemas import PortfolioBacktestRequest

ReplayProgressCallback = Callable[[int, int, str, Mapping[str, object]], None]
REPLAY_EVALUATION_WORKERS = resolve_discovery_workers()


class PortfolioServiceLike(Protocol):
    def run(
        self, config: PortfolioBacktestConfig
    ) -> PortfolioBacktestReport | dict[str, object]: ...


_WORKER_REPLAY_SERVICE: PortfolioBacktestService | None = None
_WORKER_REPLAY_SNAPSHOT: DiscoverySnapshot | None = None


def _init_replay_worker(
    database_path: str,
    snapshot_path: str,
    market_data_path: str | None = None,
) -> None:
    global _WORKER_REPLAY_SERVICE, _WORKER_REPLAY_SNAPSHOT
    worker_store = DuckDbMarketDataStore(database_path, read_only=True)
    market_adapter = (
        ETFQuestMarketDataAdapter(market_data_path)
        if market_data_path
        else DuckDbMarketDataAdapter(worker_store)
    )
    _WORKER_REPLAY_SERVICE = PortfolioBacktestService(
        adapter=market_adapter,
        indicator_cache=IndicatorCache(),
    )
    _WORKER_REPLAY_SNAPSHOT = DiscoverySnapshot(snapshot_path)


def _evaluate_replay_candidate_process(
    job: tuple[object, ...],
) -> Mapping[str, object]:
    if len(job) == 4:
        legacy_job = cast(tuple[dict[str, object], str, str, int], job)
        candidate_config, database_path, snapshot_path, total_candidates = legacy_job
        market_data_path = None
    else:
        extended_job = cast(tuple[dict[str, object], str, str, str | None, int], job)
        (
            candidate_config,
            database_path,
            snapshot_path,
            market_data_path,
            total_candidates,
        ) = extended_job
    if _WORKER_REPLAY_SERVICE is None or _WORKER_REPLAY_SNAPSHOT is None:
        _init_replay_worker(database_path, snapshot_path, market_data_path)
    assert _WORKER_REPLAY_SERVICE is not None
    assert _WORKER_REPLAY_SNAPSHOT is not None
    config = PortfolioBacktestRequest.model_validate(candidate_config).to_config()
    report = _WORKER_REPLAY_SERVICE.run_prepared_contexts(
        config,
        _WORKER_REPLAY_SNAPSHOT.contexts_for_config(config),
        total_candidates=total_candidates,
        include_details=True,
    )
    return report.to_dict()


class StrategyReplayService:
    """Replay the selected leaderboard candidates on the latest local history."""

    def __init__(
        self,
        *,
        portfolio_service: PortfolioServiceLike,
        store: DuckDbMarketDataStore,
        market_data_adapter: MarketDataAdapter | None = None,
        evaluation_workers: int | None = None,
        persist_indicator_cache: bool = True,
    ) -> None:
        self._portfolio_service = portfolio_service
        self._store = store
        self._market_data_adapter = cast(
            MarketDataAdapter,
            market_data_adapter
            or getattr(portfolio_service, "_adapter", None)
            or DuckDbMarketDataAdapter(store),
        )
        self._evaluation_workers = validate_discovery_workers(
            resolve_discovery_workers() if evaluation_workers is None else evaluation_workers
        )
        self._persist_indicator_cache = persist_indicator_cache
        self._parallel_enabled = isinstance(portfolio_service, PortfolioBacktestService) and all(
            callable(getattr(self._market_data_adapter, name, None))
            for name in ("read_many_stocks", "count_many_stocks")
        )

    def run(
        self,
        experiment: Mapping[str, object],
        leaderboard: ReplayLeaderboard,
        *,
        progress_callback: ReplayProgressCallback | None = None,
        abort_check: Callable[[], bool] | None = None,
    ) -> dict[str, object]:
        if leaderboard not in {"sharpe", "return"}:
            raise ValueError("策略复核榜单类型无效")
        experiment_id = str(experiment.get("history_id") or experiment.get("experiment_id") or "")
        if not experiment_id:
            raise ValueError("策略复核缺少实验 ID")
        source_candidates = _leaderboard_candidates(experiment, leaderboard)
        if not source_candidates:
            raise ValueError("该榜单没有可用于全量复核的策略候选")

        status_reader = getattr(self._market_data_adapter, "status", None)
        store_status = status_reader() if callable(status_reader) else self._store.status()
        data_start = _date_from_text(_base_config(experiment).get("start_date"))
        data_end = _date_from_text(store_status.data_end)
        if data_start is None:
            data_start = _date_from_text(store_status.data_start)
        if data_start is None or data_end is None:
            raise ValueError("本地 DuckDB 没有可用于全量复核的已完成日线")
        if data_start > data_end:
            raise ValueError("全量复核开始日期晚于本地最新行情日")

        full_configs = tuple(
            _full_candidate_config(candidate, data_start, data_end)
            for candidate in source_candidates
        )
        total_work = len(full_configs) + 1
        replay_request = _replay_discovery_request(experiment, data_start, data_end)
        raw_results: list[Mapping[str, object]]
        if self._parallel_enabled:
            raw_results = self._run_parallel(
                full_configs,
                replay_request,
                total_work,
                progress_callback=progress_callback,
                abort_check=abort_check,
            )
        else:
            if progress_callback is not None:
                progress_callback(
                    1,
                    total_work,
                    "snapshot_ready",
                    {
                        "overall_progress": 1 / total_work,
                        "phase_current": 0,
                        "phase_total": len(full_configs),
                        "phase_label": "开始全量复核",
                    },
                )
            raw_results = []
            for index, config_payload in enumerate(full_configs, start=1):
                if abort_check is not None and abort_check():
                    raise RuntimeError("策略复核已取消")
                try:
                    config = PortfolioBacktestRequest.model_validate(config_payload).to_config()
                    raw_report = self._portfolio_service.run(config)
                    report = (
                        raw_report.to_dict()
                        if isinstance(raw_report, PortfolioBacktestReport)
                        else dict(raw_report)
                    )
                except Exception as exc:  # noqa: BLE001 - isolate one candidate
                    report = _error_report(str(exc))
                raw_results.append(report)
                if progress_callback is not None:
                    progress_callback(
                        index + 1,
                        total_work,
                        "replaying",
                        {
                            "overall_progress": (index + 1) / total_work,
                            "phase_current": index,
                            "phase_total": len(full_configs),
                            "phase_label": f"全量复核 {index}/{len(full_configs)}",
                        },
                    )

        candidates: list[StrategyReplayCandidate] = []
        diagnostics: dict[str, str] = {}
        for rank, (source, raw_result) in enumerate(
            zip(source_candidates, raw_results, strict=True), start=1
        ):
            candidate = build_replay_candidate(
                source,
                raw_result,
                rank=rank,
                start_date=data_start,
                end_date=data_end,
            )
            candidates.append(candidate)
            if candidate.diagnostic:
                diagnostics[candidate.candidate_hash] = candidate.diagnostic
        successful = [candidate for candidate in candidates if candidate.data_fingerprint]
        if not successful:
            raise ValueError("前十策略全量复核均失败，请检查本地行情和候选配置")
        replay_fingerprint = successful[0].data_fingerprint
        source_fingerprint = str(experiment.get("data_fingerprint") or "")
        result = StrategyReplayResult(
            experiment_id=experiment_id,
            leaderboard=leaderboard,
            data_start=data_start.isoformat(),
            data_end=data_end.isoformat(),
            data_fingerprint=replay_fingerprint,
            source_data_fingerprint=source_fingerprint,
            data_changed=bool(source_fingerprint and replay_fingerprint != source_fingerprint),
            candidate_count=len(candidates),
            candidates=tuple(candidates),
            diagnostics=diagnostics,
        )
        return result.to_dict()

    def _run_parallel(
        self,
        configs: Sequence[dict[str, object]],
        replay_request: DiscoveryRequest,
        total_work: int,
        *,
        progress_callback: ReplayProgressCallback | None,
        abort_check: Callable[[], bool] | None,
    ) -> list[Mapping[str, object]]:
        snapshot_progress: Callable[[int, int], None] | None = None
        if progress_callback is not None:

            def report_snapshot_progress(current: int, total: int) -> None:
                snapshot_ratio = current / total if total > 0 else 0.0
                progress_callback(
                    0,
                    total_work,
                    "preparing_snapshot",
                    {
                        "overall_progress": min(1.0, max(0.0, snapshot_ratio / total_work)),
                        "phase_current": current,
                        "phase_total": total,
                        "phase_label": "准备全量行情和指标快照",
                    },
                )

            snapshot_progress = report_snapshot_progress
        if progress_callback is not None:
            progress_callback(
                0,
                total_work,
                "preparing_snapshot",
                {
                    "overall_progress": 0.0,
                    "phase_current": 0,
                    "phase_total": 1,
                    "phase_label": "准备全量行情和指标快照",
                },
            )

        with build_discovery_snapshot(
            self._store,
            replay_request,
            adapter=self._market_data_adapter,
            progress_callback=snapshot_progress,
            abort_check=abort_check,
            persist_indicator_cache=self._persist_indicator_cache,
        ) as snapshot:
            if progress_callback is not None:
                progress_callback(
                    1,
                    total_work,
                    "snapshot_ready",
                    {
                        "overall_progress": 1 / total_work,
                        "phase_current": 0,
                        "phase_total": len(configs),
                        "phase_label": "快照已准备，开始全量复核",
                    },
                )
            with self._evaluation_executor(str(snapshot.root)) as executor:
                market_data_path = (
                    str(getattr(self._market_data_adapter, "database_path"))
                    if isinstance(self._market_data_adapter, ETFQuestMarketDataAdapter)
                    else None
                )
                jobs = [
                    (
                        config,
                        str(self._store.database_path),
                        str(snapshot.root),
                        market_data_path,
                        snapshot.total_candidates,
                    )
                    for config in configs
                ]
                results: list[Mapping[str, object] | None] = [None] * len(jobs)
                futures = {
                    executor.submit(_evaluate_replay_candidate_process, job): index
                    for index, job in enumerate(jobs)
                }
                completed = 0
                for future in as_completed(futures):
                    if abort_check is not None and abort_check():
                        for pending in futures:
                            pending.cancel()
                        raise RuntimeError("策略复核已取消")
                    index = futures[future]
                    try:
                        results[index] = dict(future.result())
                    except Exception as exc:  # noqa: BLE001 - isolate one candidate
                        results[index] = _error_report(str(exc))
                    completed += 1
                    if progress_callback is not None:
                        progress_callback(
                            completed + 1,
                            total_work,
                            "replaying",
                            {
                                "overall_progress": (completed + 1) / total_work,
                                "phase_current": completed,
                                "phase_total": len(jobs),
                                "phase_label": f"全量复核 {completed}/{len(jobs)}",
                            },
                        )
                return [
                    item if item is not None else _error_report("复核结果为空") for item in results
                ]

    @contextmanager
    def _evaluation_executor(self, snapshot_path: str) -> Iterator[ProcessPoolExecutor]:
        market_data_path = (
            str(getattr(self._market_data_adapter, "database_path"))
            if isinstance(self._market_data_adapter, ETFQuestMarketDataAdapter)
            else None
        )
        executor = ProcessPoolExecutor(
            max_workers=self._evaluation_workers,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=_init_replay_worker,
            initargs=(str(self._store.database_path), snapshot_path, market_data_path),
        )
        try:
            yield executor
        finally:
            executor.shutdown(wait=True, cancel_futures=True)


def _leaderboard_candidates(
    experiment: Mapping[str, object], leaderboard: ReplayLeaderboard
) -> tuple[Mapping[str, object], ...]:
    raw = experiment.get("results_by_sharpe" if leaderboard == "sharpe" else "results_by_return")
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(item for item in raw[:10] if isinstance(item, Mapping))


def _base_config(experiment: Mapping[str, object]) -> Mapping[str, object]:
    raw_request = experiment.get("request")
    if not isinstance(raw_request, Mapping):
        return {}
    base = raw_request.get("base_config")
    return base if isinstance(base, Mapping) else {}


def _full_candidate_config(
    candidate: Mapping[str, object], start_date: date, end_date: date
) -> dict[str, object]:
    raw_config = candidate.get("config")
    config = dict(raw_config) if isinstance(raw_config, Mapping) else {}
    config["start_date"] = start_date.isoformat()
    config["end_date"] = end_date.isoformat()
    return config


def _replay_discovery_request(
    experiment: Mapping[str, object], start_date: date, end_date: date
) -> DiscoveryRequest:
    base = dict(_base_config(experiment))
    base["start_date"] = start_date.isoformat()
    base["end_date"] = end_date.isoformat()
    raw_request = experiment.get("request")
    request_mapping = raw_request if isinstance(raw_request, Mapping) else {}
    catalog = default_indicator_catalog()
    return DiscoveryRequest(
        base_config=base,
        splits={"search": {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()}},
        indicator_ids=tuple(catalog.rankable_ids),
        signal_ids=tuple(catalog.signal_ids),
        required_buy_conditions=_conditions(request_mapping.get("required_buy_conditions")),
        required_sell_conditions=_conditions(request_mapping.get("required_sell_conditions")),
        max_trials=1,
        timeout_seconds=86_400,
        validation_candidates=1,
        holdout_candidates=1,
        min_trades=0,
        max_drawdown=None,
        max_turnover=None,
        max_complexity=50,
        multifidelity_enabled=False,
        seed=0,
    )


def _conditions(raw: object) -> tuple[ScreenCondition, ...]:
    if not isinstance(raw, (list, tuple)):
        return ()
    conditions: list[ScreenCondition] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        left = item.get("left_value")
        operator = item.get("operator")
        right_value = item.get("right_value")
        right_constant = item.get("right_constant")
        if not isinstance(left, str) or not isinstance(operator, str):
            continue
        if right_value is not None and not isinstance(right_value, str):
            continue
        if right_constant is not None and not isinstance(right_constant, (int, float)):
            continue
        if (right_value is None) == (right_constant is None):
            continue
        conditions.append(
            ScreenCondition(
                left_value=left,
                operator=operator,
                right_value=right_value,
                right_constant=float(right_constant) if right_constant is not None else None,
            )
        )
    return tuple(conditions)


def _date_from_text(value: object) -> date | None:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _error_report(message: str) -> dict[str, object]:
    return {
        "performance": {},
        "equity_curve": [],
        "trades": [],
        "states": [],
        "ranking_events": [],
        "data_fingerprint": "",
        "diagnostic": message.strip()[:200] or "候选复核失败",
    }
