"""Spawn-process execution for per-stock condition-research phases.

Each worker owns one stock at a time: it loads bars, builds indicators, runs the
requested rule variants, and returns only the compact reports and mining samples
that the parent needs. Nothing large crosses the process boundary, which is why
a process pool is affordable here (measured: ~235 MB peak RSS per worker).

The parent keeps the mining, constraint filtering, and ranking work, so a worker
never needs the pooled sample set.
"""

from __future__ import annotations

import multiprocessing
from collections.abc import Iterator, Mapping
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import Literal, Protocol, cast

from aquest.backtest.service import BacktestMarketDataAdapter, BacktestService
from aquest.condition_research.models import ConditionResearchConfig
from aquest.condition_research.samples import MiningSamples
from aquest.market_data.adapter import DuckDbMarketDataAdapter
from aquest.market_data.store import DuckDbMarketDataStore
from aquest.screening.models import ScreenCondition

AdapterKind = Literal["duckdb"]


class WorkerPlanLike(Protocol):
    """Either the data-free or the data-aware plan; both expose a worker count."""

    @property
    def selected_workers(self) -> int: ...


# (candidate_id, side, condition)
CandidateSpec = tuple[str, str, ScreenCondition]
CandidateReports = tuple[tuple[str, Mapping[str, object]], ...]


@dataclass(frozen=True)
class TrainPayload:
    """Everything one training-window worker needs."""

    config: ConditionResearchConfig
    feature_ids: tuple[str, ...]
    train_start: date
    train_end: date


@dataclass(frozen=True)
class CandidateWindowPayload:
    """Everything one candidate-replay worker needs."""

    config: ConditionResearchConfig
    feature_ids: tuple[str, ...]
    start_date: date
    end_date: date
    fingerprints: Mapping[str, str]
    candidates: tuple[CandidateSpec, ...]


@dataclass(frozen=True)
class HoldoutPayload:
    """Holdout replay plus the full-period replay of the same candidates."""

    config: ConditionResearchConfig
    feature_ids: tuple[str, ...]
    start_date: date
    end_date: date
    fingerprints: Mapping[str, str]
    candidates: tuple[CandidateSpec, ...]


@dataclass(frozen=True)
class TrainOutcome:
    key: str
    failure: str | None = None
    fingerprint: str | None = None
    train_report: Mapping[str, object] | None = None
    full_report: Mapping[str, object] | None = None
    buy_samples: MiningSamples | None = None
    sell_samples: MiningSamples | None = None


@dataclass(frozen=True)
class CandidateWindowOutcome:
    key: str
    failure: str | None = None
    baseline: Mapping[str, object] | None = None
    candidates: CandidateReports = ()


@dataclass(frozen=True)
class HoldoutOutcome:
    key: str
    failure: str | None = None
    baseline: Mapping[str, object] | None = None
    holdout: CandidateReports = ()
    full_period: CandidateReports = ()


_STATE: dict[str, object] = {}


def init_local_worker(service: BacktestService, payload: object) -> None:
    """Run the same task functions in-process against the caller's service."""

    _STATE["service"] = service
    _STATE["payload"] = payload


def init_research_worker(adapter_kind: str, database_path: str, payload: object) -> None:
    """Process-pool initializer: build one read-only pipeline per worker.

    刻意不接持久化指标缓存。两次实测（4 worker、真实全市场库）都更慢：连接复用前
    慢 27%，把缓存读取也接到可复用连接之后仍然慢 10%。原因是缓存命中要查共享库，
    而公式重算本身在多进程下几乎线性扩展——用可并行的 CPU 换共享库 I/O 不划算。
    """

    _STATE["service"] = BacktestService(
        adapter=cast(BacktestMarketDataAdapter, _build_adapter(adapter_kind, database_path))
    )
    _STATE["payload"] = payload


def worker_service() -> BacktestService:
    """The pipeline stored by the active initializer.

    Injected services may be test doubles, so this only guards against an
    uninitialized worker instead of re-checking the concrete type.
    """

    service = _STATE.get("service")
    if service is None:
        raise RuntimeError("买卖条件研究 worker 尚未初始化")
    return cast(BacktestService, service)


def worker_payload() -> object:
    return _STATE["payload"]


def adapter_worker_target(adapter: object) -> tuple[AdapterKind, str] | None:
    """Describe how to rebuild ``adapter`` inside a worker, if possible."""

    if isinstance(adapter, DuckDbMarketDataAdapter):
        return "duckdb", str(adapter.database_path)
    return None


@contextmanager
def research_executor(
    plan: WorkerPlanLike,
    adapter_kind: str,
    database_path: str,
    payload: object,
) -> Iterator[ProcessPoolExecutor]:
    """Open a spawn pool whose workers share one phase payload."""

    executor = ProcessPoolExecutor(
        max_workers=plan.selected_workers,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=init_research_worker,
        initargs=(adapter_kind, database_path, payload),
    )
    try:
        yield executor
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


def _build_adapter(adapter_kind: str, database_path: str) -> DuckDbMarketDataAdapter:
    if adapter_kind == "duckdb":
        return DuckDbMarketDataAdapter(
            DuckDbMarketDataStore(database_path, read_only=True, pool_read_connections=True)
        )
    raise ValueError(f"不支持的买卖条件研究行情适配器: {adapter_kind}")


def phase_candidates(
    candidates: tuple[CandidateSpec, ...],
    report_for: Mapping[str, Mapping[str, object]],
) -> CandidateReports:
    """Order candidate reports by the candidate list for reproducible summaries."""

    return tuple(
        (candidate_id, report_for[candidate_id])
        for candidate_id, _, _ in candidates
        if candidate_id in report_for
    )


__all__ = [
    "AdapterKind",
    "CandidateSpec",
    "CandidateWindowOutcome",
    "CandidateWindowPayload",
    "HoldoutOutcome",
    "HoldoutPayload",
    "TrainOutcome",
    "WorkerPlanLike",
    "TrainPayload",
    "adapter_worker_target",
    "init_local_worker",
    "init_research_worker",
    "research_executor",
    "worker_payload",
    "worker_service",
]
