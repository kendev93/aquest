"""Shared contracts for the online market-data synchronization providers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal
from zoneinfo import ZoneInfo

from selector_app.concurrency import MAX_WORKERS
from selector_app.market_data.models import (
    InstrumentBoard,
    InstrumentType,
    MarketDataProvider,
    ResolvedMarketDataProvider,
)
from selector_app.market_data.scope import InstrumentScope

SyncUniverse = Literal["all", "sh", "sz"]
_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
AbortCheck = Callable[[], bool]
ProgressCallback = Callable[[int, int], None]
DetailCallback = Callable[[Mapping[str, object]], None]


class MarketDataCancelledError(RuntimeError):
    """Raised when an operator requests cancellation of a sync job."""


@dataclass(frozen=True)
class MarketSyncConfig:
    """Configuration for one online-to-DuckDB synchronization run."""

    universe: SyncUniverse = "all"
    bars: int = 800
    timeout: float = 30.0
    instrument_types: tuple[InstrumentType, ...] | None = None
    boards: tuple[InstrumentBoard, ...] | None = None
    prefer_store_targets: bool = False
    refresh_names: bool = False
    sync_workers: int = 8
    provider: MarketDataProvider = "eastmoney"
    full_history: bool = False

    def __post_init__(self) -> None:
        if self.universe not in {"all", "sh", "sz"}:
            raise ValueError(f"不支持的同步范围: {self.universe}")
        if self.provider != "eastmoney":
            raise ValueError(f"行情数据源无效: {self.provider}")
        if not isinstance(self.full_history, bool):
            raise ValueError("是否全量同步历史行情必须是布尔值")
        if not 1 <= self.bars <= 800:
            raise ValueError("每只股票同步的 K 线数量必须在 1 到 800 之间")
        if self.timeout <= 0:
            raise ValueError("行情服务器超时时间必须大于 0")
        if not isinstance(self.prefer_store_targets, bool):
            raise ValueError("行情同步目标来源开关必须是布尔值")
        if not isinstance(self.refresh_names, bool):
            raise ValueError("行情名称刷新开关必须是布尔值")
        if isinstance(self.sync_workers, bool) or not 1 <= self.sync_workers <= MAX_WORKERS:
            raise ValueError(f"行情同步并发数必须在 1 到 {MAX_WORKERS} 之间")
        scope = InstrumentScope.from_values(
            universe=self.universe,
            instrument_types=self.instrument_types,
            boards=self.boards,
        )
        object.__setattr__(self, "instrument_types", scope.instrument_types)
        object.__setattr__(self, "boards", scope.boards)


@dataclass(frozen=True)
class MarketSyncReport:
    total_candidates: int
    processed: int
    updated_files: int
    unchanged_files: int
    written_bars: int
    errors: int
    failure_reasons: Mapping[str, int]
    listed_instruments: int = 0
    filtered_instruments: int = 0
    new_instruments: int = 0
    existing_instruments: int = 0
    target_source: str = "eastmoney_stock_list"
    # ``ResolvedMarketDataProvider`` still carries "tdx" so historical rows whose
    # metadata was written by the removed legacy provider keep deserializing.
    provider: ResolvedMarketDataProvider = "eastmoney"
    provider_version: str | None = None
    instrument_list_fingerprint: str | None = None
    history_sources: Mapping[str, int] = field(default_factory=dict)
    execution_mode: str = "direct"
    worker_count: int = 1
    staged_rows: int = 0
    merged_rows: int = 0
    stage_files: tuple[str, ...] = ()
    download_seconds: float = 0.0
    merge_seconds: float = 0.0
    cleanup_seconds: float = 0.0
    rate_limit_events: int = 0
    rate_limit_backoffs: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "source": "online",
            "total_candidates": self.total_candidates,
            "processed": self.processed,
            "updated_files": self.updated_files,
            "unchanged_files": self.unchanged_files,
            "written_bars": self.written_bars,
            "errors": self.errors,
            "failure_reasons": dict(self.failure_reasons),
            "listed_instruments": self.listed_instruments,
            "filtered_instruments": self.filtered_instruments,
            "new_instruments": self.new_instruments,
            "existing_instruments": self.existing_instruments,
            "target_source": self.target_source,
            "provider": self.provider,
            "provider_version": self.provider_version,
            "instrument_list_fingerprint": self.instrument_list_fingerprint,
            "history_sources": dict(self.history_sources),
            "execution_mode": self.execution_mode,
            "worker_count": self.worker_count,
            "staged_rows": self.staged_rows,
            "merged_rows": self.merged_rows,
            "stage_files": list(self.stage_files),
            "download_seconds": self.download_seconds,
            "merge_seconds": self.merge_seconds,
            "cleanup_seconds": self.cleanup_seconds,
            "rate_limit_events": self.rate_limit_events,
            "rate_limit_backoffs": self.rate_limit_backoffs,
        }


@dataclass(frozen=True)
class InstrumentDiscoveryReport:
    """Result of refreshing the online instrument universe without bars."""

    total_candidates: int
    listed_instruments: int
    filtered_instruments: int
    new_instruments: int
    existing_instruments: int
    target_source: str = "eastmoney_stock_list"
    provider: ResolvedMarketDataProvider = "eastmoney"
    provider_reports: tuple[Mapping[str, object], ...] = ()
    provider_version: str | None = None
    instrument_list_fingerprint: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "source": "online_discovery",
            "total_candidates": self.total_candidates,
            "listed_instruments": self.listed_instruments,
            "filtered_instruments": self.filtered_instruments,
            "new_instruments": self.new_instruments,
            "existing_instruments": self.existing_instruments,
            "target_source": self.target_source,
            "provider": self.provider,
            "provider_reports": [dict(item) for item in self.provider_reports],
            "provider_version": self.provider_version,
            "instrument_list_fingerprint": self.instrument_list_fingerprint,
        }


def _report_detail(
    callback: DetailCallback | None,
    detail: Mapping[str, object],
) -> None:
    if callback is not None:
        callback(detail)


def _instrument_detail(
    market: str,
    code: str,
    name: str | None,
    *,
    status: str,
    range_label: str | None = None,
    range_start: str | None = None,
    range_end: str | None = None,
    bar_count: int | None = None,
    error: str | None = None,
    retry: int | None = None,
) -> dict[str, object]:
    detail: dict[str, object] = {
        "phase": "online_bars",
        "current_status": status,
        "current_instrument": {
            "market": market,
            "code": code,
            "name": name,
        },
    }
    if range_label is not None:
        detail["range_label"] = range_label
    if range_start is not None:
        detail["range_start"] = range_start
    if range_end is not None:
        detail["range_end"] = range_end
    if bar_count is not None:
        detail["bar_count"] = bar_count
    if error is not None:
        detail["error"] = error
    if retry is not None:
        detail["retry"] = retry
    return detail


__all__ = [
    "InstrumentDiscoveryReport",
    "MarketSyncConfig",
    "MarketSyncReport",
    "MarketDataCancelledError",
]
