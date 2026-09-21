"""Provider router for online market-data synchronization."""

from __future__ import annotations

import multiprocessing
import queue
import time
import uuid
from collections import Counter
from collections.abc import Mapping
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from dataclasses import replace
from datetime import datetime
from inspect import Parameter, signature
from pathlib import Path
from typing import Any, Protocol, cast

from selector_app.market_data.models import (
    InstrumentBoard,
    InstrumentType,
    ResolvedMarketDataProvider,
)
from selector_app.market_data.staging import (
    STAGE_DIRECTORY_NAME,
    STAGE_WORKERS,
    ProviderStagePlan,
    StageTarget,
    StageWorkerRequest,
    StageWorkerResult,
    cleanup_staging_root,
    cleanup_stale_staging,
    create_rate_state,
    ensure_staging_capacity,
    new_staging_root,
    rate_state_snapshot,
    run_stage_worker,
    staging_directory,
)
from selector_app.market_data.store import DuckDbMarketDataStore

from .market_sync import (
    AbortCheck,
    DetailCallback,
    InstrumentDiscoveryReport,
    MarketDataCancelledError,
    MarketSyncConfig,
    MarketSyncReport,
    ProgressCallback,
)
from .online_market_sync import OnlineMarketSync, OnlineModuleFactory, OnlineProviderError

_EASTMONEY_STOCK_BOARDS = frozenset({"main", "star", "chinext"})
# 本系统只研究股票。基金（含 ETF）与指数既不在同步范围内也不进候选池：ETF 跟踪
# 同一指数会产生大量共线标的，指数本身不可交易。
_EASTMONEY_SUPPORTED_BOARDS = _EASTMONEY_STOCK_BOARDS
_EASTMONEY_SUPPORTED_TYPES = frozenset({"stock"})


class ProviderSyncService(Protocol):
    def discover_instruments(
        self,
        config: MarketSyncConfig,
        *,
        detail_callback: DetailCallback | None = None,
    ) -> InstrumentDiscoveryReport: ...

    def sync(
        self,
        config: MarketSyncConfig,
        progress_callback: ProgressCallback | None = None,
        *,
        detail_callback: DetailCallback | None = None,
        abort_check: AbortCheck | None = None,
    ) -> MarketSyncReport: ...


class MarketDataSync:
    """Route the default Online flow (East Money is the only provider)."""

    def __init__(
        self,
        store: DuckDbMarketDataStore | None = None,
        *,
        eastmoney_sync: ProviderSyncService | None = None,
        eastmoney_module_factory: OnlineModuleFactory | None = None,
    ) -> None:
        selected_store = store or DuckDbMarketDataStore()
        self._eastmoney_sync = eastmoney_sync or OnlineMarketSync(
            store=selected_store,
            module_factory=eastmoney_module_factory,
        )
        self._store = selected_store

    def discover_instruments(
        self,
        config: MarketSyncConfig,
        *,
        detail_callback: DetailCallback | None = None,
    ) -> InstrumentDiscoveryReport:
        reports: list[InstrumentDiscoveryReport] = []
        for provider, provider_config, service in self._plans(config):
            reports.append(
                _coerce_discovery_report(
                    service.discover_instruments(
                        provider_config,
                        detail_callback=_provider_detail_callback(detail_callback, provider),
                    ),
                    provider,
                )
            )
        return _combine_discovery_reports(reports)

    def sync(
        self,
        config: MarketSyncConfig,
        progress_callback: ProgressCallback | None = None,
        *,
        detail_callback: DetailCallback | None = None,
        abort_check: AbortCheck | None = None,
    ) -> MarketSyncReport:
        if abort_check is not None and abort_check():
            raise MarketDataCancelledError("行情同步已取消")
        plans = self._plans(config)
        if _can_process_stage(plans):
            return self._sync_staged(
                plans,
                progress_callback=progress_callback,
                detail_callback=detail_callback,
                abort_check=abort_check,
            )
        return self._sync_direct(
            plans,
            config=config,
            progress_callback=progress_callback,
            detail_callback=detail_callback,
            abort_check=abort_check,
        )

    def _sync_direct(
        self,
        plans: list[tuple[str, MarketSyncConfig, ProviderSyncService]],
        *,
        config: MarketSyncConfig,
        progress_callback: ProgressCallback | None,
        detail_callback: DetailCallback | None,
        abort_check: AbortCheck | None,
    ) -> MarketSyncReport:
        reports: list[MarketSyncReport] = []
        offset = 0
        known_total = _known_store_total(self._store, config, plans)
        for provider, provider_config, service in plans:
            if abort_check is not None and abort_check():
                raise MarketDataCancelledError("行情同步已取消")
            sub_total = _store_target_count(self._store, provider_config)

            def on_progress(current: int, total: int, *, base: int = offset) -> None:
                if progress_callback is None:
                    return
                if known_total is not None:
                    progress_callback(base + current, known_total)
                else:
                    progress_callback(current, total)

            reports.append(
                _coerce_sync_report(
                    _call_provider_sync(
                        service,
                        provider_config,
                        on_progress if progress_callback is not None else None,
                        detail_callback=_provider_detail_callback(detail_callback, provider),
                        abort_check=abort_check,
                    ),
                    provider,
                )
            )
            offset += sub_total if sub_total is not None else reports[-1].total_candidates
        combined = _combine_sync_reports(reports)
        self._store.set_meta("last_online_provider", combined.provider)
        if combined.provider_version is not None:
            self._store.set_meta("last_online_provider_version", combined.provider_version)
        if combined.instrument_list_fingerprint is not None:
            self._store.set_meta(
                "instrument_list_fingerprint",
                combined.instrument_list_fingerprint,
            )
        return combined

    def _sync_staged(
        self,
        plans: list[tuple[str, MarketSyncConfig, ProviderSyncService]],
        *,
        progress_callback: ProgressCallback | None,
        detail_callback: DetailCallback | None,
        abort_check: AbortCheck | None,
    ) -> MarketSyncReport:
        prepared = [
            cast(ProviderStagePlan, getattr(service, "prepare_stage_plan")(provider_config))
            for _provider, provider_config, service in plans
        ]
        total = sum(len(plan.targets) for plan in prepared)
        if progress_callback is not None:
            progress_callback(0, total)
        _report_stage_detail(
            detail_callback,
            {
                "phase": "planning",
                "current_status": "completed",
                "execution_mode": "process_pool_staging",
                "worker_count": STAGE_WORKERS,
                "total_candidates": total,
            },
        )
        if abort_check is not None and abort_check():
            raise MarketDataCancelledError("行情同步已取消")
        if not prepared:
            return MarketSyncReport(
                total_candidates=0,
                processed=0,
                updated_files=0,
                unchanged_files=0,
                written_bars=0,
                errors=0,
                failure_reasons={},
                provider="mixed",
                execution_mode="process_pool_staging",
                worker_count=0,
            )

        provider = prepared[0].provider
        run_id = uuid.uuid4().hex
        cleanup_stale_staging(staging_directory(self._store.database_path))
        ensure_staging_capacity(self._store.database_path)
        stage_root = new_staging_root(self._store.database_path, run_id)
        stage_paths: list[Path] = []
        manager = multiprocessing.Manager()
        rate_state, rate_lock = create_rate_state(manager)
        cancel_event = manager.Event()
        event_queue = manager.Queue()
        worker_results: list[StageWorkerResult] = []
        download_started = time.monotonic()
        download_seconds = 0.0
        downloaded = 0
        try:
            all_targets = tuple(target for plan in prepared for target in plan.targets)
            worker_count = min(STAGE_WORKERS, len(all_targets)) if all_targets else 0
            buckets: list[list[StageTarget]] = [[] for _ in range(worker_count)]
            for index, target in enumerate(all_targets):
                buckets[index % worker_count].append(target)
            requests: list[StageWorkerRequest] = []
            for worker_id, targets in enumerate(buckets):
                path = stage_root / f"worker-{worker_id}.duckdb"
                stage_paths.append(path)
                requests.append(
                    StageWorkerRequest(
                        worker_id=worker_id,
                        stage_path=str(path),
                        provider=provider,
                        targets=tuple(targets),
                        config=plans[0][1],
                        as_of=prepared[0].as_of,
                        cancel_event=cancel_event,
                        rate_state=rate_state,
                        rate_lock=rate_lock,
                        event_queue=event_queue,
                    )
                )
            if worker_count:
                _report_stage_detail(
                    detail_callback,
                    {
                        "phase": "fetching",
                        "current_status": "running",
                        "execution_mode": "process_pool_staging",
                        "worker_count": worker_count,
                        "total_candidates": total,
                        "downloaded_targets": 0,
                    },
                )
                context = multiprocessing.get_context("spawn")
                executor = ProcessPoolExecutor(
                    max_workers=worker_count,
                    mp_context=context,
                )
                futures = {
                    executor.submit(run_stage_worker, request): request.worker_id
                    for request in requests
                }
                pending = set(futures)
                try:
                    while pending:
                        if abort_check is not None and abort_check():
                            cancel_event.set()
                            for future in pending:
                                future.cancel()
                            raise MarketDataCancelledError("行情同步已取消")
                        done, pending = wait(
                            pending,
                            timeout=0.2,
                            return_when=FIRST_COMPLETED,
                        )
                        downloaded = _drain_stage_events(
                            event_queue,
                            downloaded=downloaded,
                            total=total,
                            progress_callback=progress_callback,
                            detail_callback=detail_callback,
                        )
                        for future in done:
                            try:
                                worker_results.append(future.result())
                            except BrokenProcessPool as exc:
                                raise RuntimeError(
                                    "行情同步的 8 个进程异常退出，请检查 Docker 资源和上游连接"
                                ) from exc
                    downloaded = _drain_stage_events(
                        event_queue,
                        downloaded=downloaded,
                        total=total,
                        progress_callback=progress_callback,
                        detail_callback=detail_callback,
                    )
                finally:
                    executor.shutdown(wait=True, cancel_futures=True)
            if abort_check is not None and abort_check():
                raise MarketDataCancelledError("行情同步已取消")
            download_seconds = max(0.0, time.monotonic() - download_started)
            worker_summary = _combine_stage_worker_results(worker_results)
            merged_rows = 0
            merged_workers = 0
            merge_started = time.monotonic()
            _report_stage_detail(
                detail_callback,
                {
                    "phase": "merging",
                    "current_status": "running",
                    "execution_mode": "process_pool_staging",
                    "worker_count": worker_count,
                    "downloaded_targets": downloaded,
                    "staged_rows": worker_summary.staged_rows,
                    "merged_workers": 0,
                    "total_workers": worker_count,
                },
            )
            if progress_callback is not None:
                progress_callback(0, worker_count or 1)
            for path in sorted(stage_paths):
                if abort_check is not None and abort_check():
                    raise MarketDataCancelledError("行情同步已取消")
                merge_report = self._store.merge_online_stage(path)
                merged_rows += merge_report.merged_rows
                merged_workers += 1
                if progress_callback is not None:
                    progress_callback(merged_workers, worker_count or 1)
                _report_stage_detail(
                    detail_callback,
                    {
                        "phase": "merging",
                        "current_status": "running",
                        "execution_mode": "process_pool_staging",
                        "worker_count": worker_count,
                        "downloaded_targets": downloaded,
                        "staged_rows": worker_summary.staged_rows,
                        "merged_workers": merged_workers,
                        "total_workers": worker_count,
                        "merged_rows": merged_rows,
                    },
                )
            merge_seconds = max(0.0, time.monotonic() - merge_started)
            cleanup_started = time.monotonic()
            _report_stage_detail(
                detail_callback,
                {
                    "phase": "cleanup",
                    "current_status": "running",
                    "execution_mode": "process_pool_staging",
                },
            )
            cleanup_staging_root(stage_root)
            cleanup_seconds = max(0.0, time.monotonic() - cleanup_started)
            rate = rate_state_snapshot(rate_state)
            finished_at = datetime.now(tz=prepared[0].as_of.tzinfo)
            self._store.set_meta("last_online_sync_at", finished_at.isoformat())
            self._store.set_meta("last_online_provider", provider)
            if prepared[0].provider_version is not None:
                self._store.set_meta(
                    "last_online_provider_version",
                    prepared[0].provider_version,
                )
            if prepared[0].instrument_list_fingerprint is not None:
                self._store.set_meta(
                    "instrument_list_fingerprint",
                    prepared[0].instrument_list_fingerprint,
                )
            _report_stage_detail(
                detail_callback,
                {
                    "phase": "completed",
                    "current_status": "completed",
                    "execution_mode": "process_pool_staging",
                    "worker_count": worker_count,
                    "downloaded_targets": downloaded,
                    "staged_rows": worker_summary.staged_rows,
                    "merged_workers": merged_workers,
                    "total_workers": worker_count,
                    "merged_rows": merged_rows,
                    **rate,
                },
            )
            return MarketSyncReport(
                total_candidates=total,
                processed=worker_summary.processed,
                updated_files=worker_summary.updated_files,
                unchanged_files=worker_summary.unchanged_files,
                written_bars=merged_rows,
                errors=worker_summary.errors,
                failure_reasons=worker_summary.failure_reasons,
                listed_instruments=sum(plan.listed_instruments for plan in prepared),
                filtered_instruments=sum(plan.filtered_instruments for plan in prepared),
                new_instruments=sum(plan.new_instruments for plan in prepared),
                existing_instruments=sum(plan.existing_instruments for plan in prepared),
                target_source="+".join(plan.target_source for plan in prepared),
                provider=cast_provider(provider),
                provider_version=prepared[0].provider_version,
                instrument_list_fingerprint=prepared[0].instrument_list_fingerprint,
                history_sources=worker_summary.history_sources,
                execution_mode="process_pool_staging",
                worker_count=worker_count,
                staged_rows=worker_summary.staged_rows,
                merged_rows=merged_rows,
                stage_files=tuple(path.name for path in stage_paths),
                download_seconds=download_seconds,
                merge_seconds=merge_seconds,
                cleanup_seconds=cleanup_seconds,
                rate_limit_events=_as_int(rate.get("rate_limit_events")),
                rate_limit_backoffs=_as_int(rate.get("backoffs")),
            )
        except MarketDataCancelledError:
            _report_stage_detail(
                detail_callback,
                {
                    "phase": "cancelled",
                    "current_status": "cancelled",
                    "execution_mode": "process_pool_staging",
                },
            )
            raise
        except Exception as exc:
            _report_stage_detail(
                detail_callback,
                {
                    "phase": "failed",
                    "current_status": "error",
                    "execution_mode": "process_pool_staging",
                    "error": str(exc).strip()[:200] or type(exc).__name__,
                },
            )
            raise
        finally:
            try:
                if stage_root.exists():
                    cleanup_staging_root(stage_root)
            finally:
                manager.shutdown()

    def provider_capabilities(self) -> dict[str, object]:
        eastmoney_capabilities = _provider_capabilities(self._eastmoney_sync)
        try:
            instrument_count = self._store.status().instrument_count
        except Exception:  # noqa: BLE001 - capability reporting must survive an unavailable store
            instrument_count = 0
        try:
            last_online_provider = self._store.get_meta("last_online_provider")
            last_online_provider_version = self._store.get_meta("last_online_provider_version")
            instrument_list_fingerprint = self._store.get_meta("instrument_list_fingerprint")
        except Exception:  # noqa: BLE001 - metadata is informational
            last_online_provider = None
            last_online_provider_version = None
            instrument_list_fingerprint = None
        return {
            "default_provider": "eastmoney",
            "instrument_count": instrument_count,
            "last_online_provider": last_online_provider,
            "last_online_provider_version": last_online_provider_version,
            "instrument_list_fingerprint": instrument_list_fingerprint,
            "sync_execution": {
                "mode": "process_pool_staging",
                "worker_count": STAGE_WORKERS,
                "staging_directory": STAGE_DIRECTORY_NAME,
            },
            "providers": {
                "eastmoney": eastmoney_capabilities,
            },
        }

    def _plans(
        self,
        config: MarketSyncConfig,
    ) -> list[tuple[str, MarketSyncConfig, ProviderSyncService]]:
        _validate_eastmoney_scope(config)
        eastmoney_types = _eastmoney_types(config)
        if not eastmoney_types:
            raise OnlineProviderError(
                "在线同步当前只支持沪市主板、科创板和创业板股票。"
            )
        return [
            (
                "eastmoney",
                replace(
                    config,
                    provider="eastmoney",
                    instrument_types=eastmoney_types,
                    boards=_eastmoney_boards(config),
                ),
                self._eastmoney_sync,
            )
        ]


def _provider_detail_callback(
    callback: DetailCallback | None,
    provider: str,
) -> DetailCallback | None:
    if callback is None:
        return None

    def report(detail: Mapping[str, object]) -> None:
        callback({**detail, "provider": provider})

    return report


def _can_process_stage(
    plans: list[tuple[str, MarketSyncConfig, ProviderSyncService]],
) -> bool:
    if not plans:
        return False
    for _provider, _config, service in plans:
        capability = getattr(service, "supports_process_staging", None)
        if not callable(capability) or not bool(capability()):
            return False
        if not callable(getattr(service, "prepare_stage_plan", None)):
            return False
    return True


def _report_stage_detail(
    callback: DetailCallback | None,
    detail: Mapping[str, object],
) -> None:
    if callback is not None:
        callback(detail)


def _drain_stage_events(
    event_queue: Any,
    *,
    downloaded: int,
    total: int,
    progress_callback: ProgressCallback | None,
    detail_callback: DetailCallback | None,
) -> int:
    while True:
        try:
            event = event_queue.get_nowait()
        except queue.Empty:
            return downloaded
        if not isinstance(event, Mapping):
            continue
        kind = event.get("kind")
        if kind == "started":
            _report_stage_detail(
                detail_callback,
                {
                    "phase": "fetching",
                    "current_status": "fetching",
                    "worker_id": event.get("worker_id"),
                    "current_instrument": {
                        "market": event.get("market"),
                        "code": event.get("code"),
                        "name": event.get("name"),
                    },
                    "provider": event.get("provider"),
                    "range_label": event.get("range_label"),
                },
            )
            continue
        if kind != "finished":
            continue
        downloaded += 1
        detail = event.get("detail")
        if isinstance(detail, Mapping):
            _report_stage_detail(detail_callback, dict(detail))
        if progress_callback is not None:
            progress_callback(downloaded, total)


def _combine_stage_worker_results(results: list[StageWorkerResult]) -> StageWorkerResult:
    failures: Counter[str] = Counter()
    history_sources: Counter[str] = Counter()
    for result in results:
        failures.update(result.failure_reasons)
        history_sources.update(result.history_sources)
    return StageWorkerResult(
        worker_id=-1,
        stage_path="",
        processed=sum(result.processed for result in results),
        updated_files=sum(result.updated_files for result in results),
        unchanged_files=sum(result.unchanged_files for result in results),
        staged_rows=sum(result.staged_rows for result in results),
        errors=sum(result.errors for result in results),
        failure_reasons=dict(failures),
        history_sources=dict(history_sources),
    )


def _provider_capabilities(service: ProviderSyncService) -> dict[str, object]:
    method = getattr(service, "provider_capabilities", None)
    if callable(method):
        result = method()
        if isinstance(result, dict):
            return result
    return {
        "provider": "eastmoney",
        "available": True,
        "version": None,
        "instrument_types": ["stock"],
    }


def _call_provider_sync(
    service: ProviderSyncService,
    config: MarketSyncConfig,
    progress_callback: ProgressCallback | None,
    *,
    detail_callback: DetailCallback | None,
    abort_check: AbortCheck | None,
) -> MarketSyncReport | Mapping[str, object]:
    if abort_check is not None and _supports_abort_check(service.sync):
        return service.sync(
            config,
            progress_callback,
            detail_callback=detail_callback,
            abort_check=abort_check,
        )
    return service.sync(config, progress_callback, detail_callback=detail_callback)


def _supports_abort_check(method: object) -> bool:
    if not callable(method):
        return False
    try:
        parameters = signature(method).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "abort_check"
        and parameter.kind in {Parameter.POSITIONAL_OR_KEYWORD, Parameter.KEYWORD_ONLY}
        for parameter in parameters
    )


def _eastmoney_types(config: MarketSyncConfig) -> tuple[InstrumentType, ...] | None:
    """Instrument kinds the East Money channel can serve for this request.

    An unspecified request keeps resolving to stocks only: silently pulling the
    whole ETF catalog into every "sync everything" run would multiply the
    download volume for users who never asked for funds.
    """

    if config.instrument_types is None:
        return ("stock",)
    supported = tuple(
        value for value in config.instrument_types if value in _EASTMONEY_SUPPORTED_TYPES
    )
    return supported or None


def _validate_eastmoney_scope(config: MarketSyncConfig) -> None:
    unsupported_types = set(config.instrument_types or ()) - _EASTMONEY_SUPPORTED_TYPES
    unsupported_boards = set(config.boards or ()) - _EASTMONEY_SUPPORTED_BOARDS
    if unsupported_types or unsupported_boards:
        unsupported = sorted({*unsupported_types, *unsupported_boards})
        raise OnlineProviderError(
            "在线同步当前仅支持主板、科创板、创业板股票；"
            f"当前范围包含不支持的品种/板块: {', '.join(unsupported)}。"
        )
    # Both sides can be legal on their own and still describe nothing: 基金 +
    # 主板 used to pass both set checks, then resolved to zero targets and made
    # the job report success with no explanation. Reject the empty intersection.
    selected_types = set(config.instrument_types or ())
    selected_boards = set(config.boards or ())
    if selected_types and selected_boards:
        reachable: set[str] = set(_EASTMONEY_STOCK_BOARDS)
        if not reachable & selected_boards:
            raise OnlineProviderError(
                "所选品种与板块没有交集，不会命中任何标的："
                f"品种 {', '.join(sorted(selected_types))} 只能落在 "
                f"{', '.join(sorted(reachable))} 板块，而当前选的是 "
                f"{', '.join(sorted(selected_boards))}。"
            )


def _eastmoney_boards(config: MarketSyncConfig) -> tuple[InstrumentBoard, ...] | None:
    if config.boards is None:
        return None
    selected = tuple(value for value in config.boards if value in _EASTMONEY_SUPPORTED_BOARDS)
    return selected or None


def _store_target_count(
    store: DuckDbMarketDataStore,
    config: MarketSyncConfig,
) -> int | None:
    if not config.prefer_store_targets:
        return None
    return len(
        store.list_instruments(
            market=None if config.universe == "all" else config.universe.upper(),
            instrument_types=config.instrument_types,
            boards=config.boards,
        )
    )


def _known_store_total(
    store: DuckDbMarketDataStore,
    config: MarketSyncConfig,
    plans: list[tuple[str, MarketSyncConfig, ProviderSyncService]],
) -> int | None:
    if not config.prefer_store_targets:
        return None
    return sum(_store_target_count(store, provider_config) or 0 for _, provider_config, _ in plans)


def _combine_discovery_reports(
    reports: list[InstrumentDiscoveryReport],
) -> InstrumentDiscoveryReport:
    if not reports:
        return InstrumentDiscoveryReport(
            total_candidates=0,
            listed_instruments=0,
            filtered_instruments=0,
            new_instruments=0,
            existing_instruments=0,
            target_source="none",
            provider="mixed",
        )
    providers = {report.provider for report in reports}
    provider: str = next(iter(providers)) if len(providers) == 1 else "mixed"
    provider_versions = {report.provider_version for report in reports if report.provider_version}
    fingerprints = {
        report.instrument_list_fingerprint
        for report in reports
        if report.instrument_list_fingerprint
    }
    return InstrumentDiscoveryReport(
        total_candidates=sum(report.total_candidates for report in reports),
        listed_instruments=sum(report.listed_instruments for report in reports),
        filtered_instruments=sum(report.filtered_instruments for report in reports),
        new_instruments=sum(report.new_instruments for report in reports),
        existing_instruments=sum(report.existing_instruments for report in reports),
        target_source="+".join(report.target_source for report in reports),
        provider=cast_provider(provider),
        provider_reports=tuple(report.to_dict() for report in reports),
        provider_version=(next(iter(provider_versions)) if len(provider_versions) == 1 else None),
        instrument_list_fingerprint=(next(iter(fingerprints)) if len(fingerprints) == 1 else None),
    )


def _coerce_discovery_report(
    value: InstrumentDiscoveryReport | Mapping[str, object],
    provider: str,
) -> InstrumentDiscoveryReport:
    if isinstance(value, InstrumentDiscoveryReport):
        return value
    return InstrumentDiscoveryReport(
        total_candidates=_as_int(value.get("total_candidates")),
        listed_instruments=_as_int(value.get("listed_instruments")),
        filtered_instruments=_as_int(value.get("filtered_instruments")),
        new_instruments=_as_int(value.get("new_instruments")),
        existing_instruments=_as_int(value.get("existing_instruments")),
        target_source=str(value.get("target_source") or f"{provider}_unknown"),
        provider=cast_provider(str(value.get("provider") or provider)),
    )


def _combine_sync_reports(reports: list[MarketSyncReport]) -> MarketSyncReport:
    if not reports:
        return MarketSyncReport(
            total_candidates=0,
            processed=0,
            updated_files=0,
            unchanged_files=0,
            written_bars=0,
            errors=0,
            failure_reasons={},
            target_source="none",
            provider="mixed",
        )
    reasons: Counter[str] = Counter()
    history_sources: Counter[str] = Counter()
    for report in reports:
        reasons.update(report.failure_reasons)
        history_sources.update(report.history_sources)
    providers = {report.provider for report in reports}
    provider: str = next(iter(providers)) if len(providers) == 1 else "mixed"
    provider_versions = {report.provider_version for report in reports if report.provider_version}
    fingerprints = {
        report.instrument_list_fingerprint
        for report in reports
        if report.instrument_list_fingerprint
    }
    execution_modes = {report.execution_mode for report in reports}
    return MarketSyncReport(
        total_candidates=sum(report.total_candidates for report in reports),
        processed=sum(report.processed for report in reports),
        updated_files=sum(report.updated_files for report in reports),
        unchanged_files=sum(report.unchanged_files for report in reports),
        written_bars=sum(report.written_bars for report in reports),
        errors=sum(report.errors for report in reports),
        failure_reasons=dict(reasons),
        listed_instruments=sum(report.listed_instruments for report in reports),
        filtered_instruments=sum(report.filtered_instruments for report in reports),
        new_instruments=sum(report.new_instruments for report in reports),
        existing_instruments=sum(report.existing_instruments for report in reports),
        target_source="+".join(report.target_source for report in reports),
        provider=cast_provider(provider),
        provider_version=(next(iter(provider_versions)) if len(provider_versions) == 1 else None),
        instrument_list_fingerprint=(next(iter(fingerprints)) if len(fingerprints) == 1 else None),
        history_sources=dict(history_sources),
        execution_mode=(next(iter(execution_modes)) if len(execution_modes) == 1 else "mixed"),
        worker_count=max((report.worker_count for report in reports), default=1),
        staged_rows=sum(report.staged_rows for report in reports),
        merged_rows=sum(report.merged_rows for report in reports),
        stage_files=tuple(file for report in reports for file in report.stage_files),
        download_seconds=sum(report.download_seconds for report in reports),
        merge_seconds=sum(report.merge_seconds for report in reports),
        cleanup_seconds=sum(report.cleanup_seconds for report in reports),
        rate_limit_events=sum(report.rate_limit_events for report in reports),
        rate_limit_backoffs=sum(report.rate_limit_backoffs for report in reports),
    )


def _coerce_sync_report(
    value: MarketSyncReport | Mapping[str, object],
    provider: str,
) -> MarketSyncReport:
    if isinstance(value, MarketSyncReport):
        return value
    raw_reasons = value.get("failure_reasons")
    reasons = (
        {str(key): _as_int(item) for key, item in raw_reasons.items()}
        if isinstance(raw_reasons, Mapping)
        else {}
    )
    raw_history_sources = value.get("history_sources")
    history_sources = (
        {str(key): _as_int(item) for key, item in raw_history_sources.items()}
        if isinstance(raw_history_sources, Mapping)
        else {}
    )
    return MarketSyncReport(
        total_candidates=_as_int(value.get("total_candidates")),
        processed=_as_int(value.get("processed")),
        updated_files=_as_int(value.get("updated_files")),
        unchanged_files=_as_int(value.get("unchanged_files")),
        written_bars=_as_int(value.get("written_bars")),
        errors=_as_int(value.get("errors")),
        failure_reasons=reasons,
        listed_instruments=_as_int(value.get("listed_instruments")),
        filtered_instruments=_as_int(value.get("filtered_instruments")),
        new_instruments=_as_int(value.get("new_instruments")),
        existing_instruments=_as_int(value.get("existing_instruments")),
        target_source=str(value.get("target_source") or f"{provider}_unknown"),
        provider=cast_provider(str(value.get("provider") or provider)),
        history_sources=history_sources,
        execution_mode=str(value.get("execution_mode") or "direct"),
        worker_count=_as_int(value.get("worker_count")) or 1,
        staged_rows=_as_int(value.get("staged_rows")),
        merged_rows=_as_int(value.get("merged_rows")),
        stage_files=_as_string_tuple(value.get("stage_files")),
        download_seconds=_as_float(value.get("download_seconds")),
        merge_seconds=_as_float(value.get("merge_seconds")),
        cleanup_seconds=_as_float(value.get("cleanup_seconds")),
        rate_limit_events=_as_int(value.get("rate_limit_events")),
        rate_limit_backoffs=_as_int(value.get("rate_limit_backoffs")),
    )


def _as_int(value: object) -> int:
    if value is None or isinstance(value, bool):
        return 0
    if not isinstance(value, (int, float, str)):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _as_float(value: object) -> float:
    if value is None or isinstance(value, bool):
        return 0.0
    if not isinstance(value, (int, float, str)):
        return 0.0
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _as_string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value if isinstance(item, str))


def cast_provider(value: str) -> ResolvedMarketDataProvider:
    # Historical metadata written by the removed legacy TDX provider maps to the
    # "mixed" catch-all so old store rows keep deserializing.
    return "eastmoney" if value == "eastmoney" else "mixed"


__all__ = ["MarketDataSync", "ProviderSyncService"]
