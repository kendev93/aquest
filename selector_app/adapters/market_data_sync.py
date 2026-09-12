"""Provider router for online market-data synchronization."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import replace
from typing import Protocol

from selector_app.market_data.models import InstrumentBoard, ResolvedMarketDataProvider
from selector_app.market_data.store import DuckDbMarketDataStore

from .akshare_market_sync import AkShareMarketSync, AkShareModuleFactory, AkShareProviderError
from .market_sync import (
    DetailCallback,
    InstrumentDiscoveryReport,
    MarketSyncConfig,
    MarketSyncReport,
    ProgressCallback,
    TdxMarketSync,
)

_AKSHARE_STOCK_BOARDS = frozenset({"main", "star", "chinext"})


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
    ) -> MarketSyncReport: ...


class MarketDataSync:
    """Route the default AKShare flow while retaining TDX compatibility."""

    def __init__(
        self,
        store: DuckDbMarketDataStore | None = None,
        *,
        tdx_sync: ProviderSyncService | None = None,
        akshare_sync: ProviderSyncService | None = None,
        akshare_module_factory: AkShareModuleFactory | None = None,
    ) -> None:
        selected_store = store or DuckDbMarketDataStore()
        self._tdx_sync = tdx_sync or TdxMarketSync(store=selected_store)
        self._akshare_sync = akshare_sync or AkShareMarketSync(
            store=selected_store,
            module_factory=akshare_module_factory,
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
    ) -> MarketSyncReport:
        plans = self._plans(config)
        reports: list[MarketSyncReport] = []
        offset = 0
        known_total = _known_store_total(self._store, config, plans)
        for provider, provider_config, service in plans:
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
                    service.sync(
                        provider_config,
                        on_progress if progress_callback is not None else None,
                        detail_callback=_provider_detail_callback(detail_callback, provider),
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

    def provider_capabilities(self) -> dict[str, object]:
        akshare_capabilities = _provider_capabilities(self._akshare_sync)
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
            "default_provider": "akshare",
            "instrument_count": instrument_count,
            "last_online_provider": last_online_provider,
            "last_online_provider_version": last_online_provider_version,
            "instrument_list_fingerprint": instrument_list_fingerprint,
            "providers": {
                "akshare": akshare_capabilities,
                "tdx": {
                    "provider": "tdx",
                    "available": True,
                    "version": None,
                    "instrument_types": ["stock", "fund", "index", "bond"],
                    "boards": ["main", "star", "chinext", "b_share", "fund", "index", "bond"],
                },
            },
        }

    def _plans(
        self,
        config: MarketSyncConfig,
    ) -> list[tuple[str, MarketSyncConfig, ProviderSyncService]]:
        if config.provider == "tdx":
            return [("tdx", config, self._tdx_sync)]
        _validate_akshare_scope(config)
        plans: list[tuple[str, MarketSyncConfig, ProviderSyncService]] = []
        if _scope_can_include_akshare_stock(config):
            plans.append(
                (
                    "akshare",
                    replace(
                        config,
                        provider="akshare",
                        instrument_types=("stock",),
                        boards=_akshare_boards(config),
                    ),
                    self._akshare_sync,
                )
            )
        if not plans:
            raise AkShareProviderError(
                "AKShare 当前只支持主板、科创板和创业板股票；B 股、基金、指数、债券请切换为 TDX。"
            )
        return plans


def _provider_detail_callback(
    callback: DetailCallback | None,
    provider: str,
) -> DetailCallback | None:
    if callback is None:
        return None

    def report(detail: Mapping[str, object]) -> None:
        callback({**detail, "provider": provider})

    return report


def _provider_capabilities(service: ProviderSyncService) -> dict[str, object]:
    method = getattr(service, "provider_capabilities", None)
    if callable(method):
        result = method()
        if isinstance(result, dict):
            return result
    return {
        "provider": "akshare",
        "available": True,
        "version": None,
        "instrument_types": ["stock"],
    }


def _scope_can_include_akshare_stock(config: MarketSyncConfig) -> bool:
    if config.instrument_types is not None and "stock" not in config.instrument_types:
        return False
    return config.boards is None or bool(set(config.boards) & _AKSHARE_STOCK_BOARDS)


def _validate_akshare_scope(config: MarketSyncConfig) -> None:
    unsupported_types = set(config.instrument_types or ()) - {"stock"}
    unsupported_boards = set(config.boards or ()) - _AKSHARE_STOCK_BOARDS
    if unsupported_types or unsupported_boards:
        unsupported = sorted({*unsupported_types, *unsupported_boards})
        raise AkShareProviderError(
            "AKShare 当前仅支持主板、科创板和创业板股票；"
            f"当前范围包含不支持的品种/板块: {', '.join(unsupported)}，请切换为 TDX。"
        )


def _akshare_boards(config: MarketSyncConfig) -> tuple[InstrumentBoard, ...] | None:
    if config.boards is None:
        return None
    selected = tuple(value for value in config.boards if value in _AKSHARE_STOCK_BOARDS)
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


def cast_provider(value: str) -> ResolvedMarketDataProvider:
    if value == "akshare":
        return "akshare"
    if value == "tdx":
        return "tdx"
    return "mixed"


__all__ = ["MarketDataSync", "ProviderSyncService"]
