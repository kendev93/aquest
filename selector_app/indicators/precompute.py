"""Batch materialization of built-in indicator results for the local store."""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pandas as pd

from selector_app.concurrency import MAX_WORKERS
from selector_app.formulas.common import validate_market_data
from selector_app.formulas.registry import FORMULA_REGISTRY, FormulaRegistry
from selector_app.formulas.types import FormulaResult
from selector_app.indicators.cache import IndicatorCache
from selector_app.market_data.models import InstrumentBoard, InstrumentType, StockRef

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int], None]
_PRECOMPUTE_CHUNK_SIZE = 32
# Loading every bar for every instrument is an optional cache-warming task.  A
# large store should remain available for online sync and normal queries even
# when warming it would temporarily require several copies of the data in
# memory.
PRECOMPUTE_MAX_BARS = 2_000_000


def should_defer_precompute(
    bar_count: int,
    *,
    max_bars: int = PRECOMPUTE_MAX_BARS,
) -> bool:
    """Return whether cache warming should be deferred for a large store."""

    if isinstance(bar_count, bool) or not isinstance(bar_count, int) or bar_count < 0:
        raise ValueError("行情根数必须是非负整数")
    if isinstance(max_bars, bool) or not isinstance(max_bars, int) or max_bars < 0:
        raise ValueError("指标预计算上限必须是非负整数")
    return bar_count > max_bars


@dataclass(frozen=True)
class _PrecomputeOutcome:
    processed: int = 0
    skipped: int = 0
    failure_reason: str | None = None
    results: Mapping[str, FormulaResult] | None = None


class PrecomputeMarketDataAdapter(Protocol):
    def list_stock_refs(
        self,
        vipdoc_path: str | Path,
        universe: str,
        universe_file: str | Path | None = None,
        instrument_types: tuple[InstrumentType, ...] | None = None,
        boards: tuple[InstrumentBoard, ...] | None = None,
    ) -> list[StockRef]: ...

    def read_stock(self, ref: StockRef) -> pd.DataFrame: ...


@dataclass(frozen=True)
class IndicatorPrecomputeReport:
    total_candidates: int
    processed: int
    skipped: int
    errors: int
    computed: int
    cache_hits: int
    failure_reasons: Mapping[str, int]

    def to_dict(self) -> dict[str, object]:
        return {
            "total_candidates": self.total_candidates,
            "processed": self.processed,
            "skipped": self.skipped,
            "errors": self.errors,
            "computed": self.computed,
            "cache_hits": self.cache_hits,
            "failure_reasons": dict(self.failure_reasons),
        }


class IndicatorPrecomputeService:
    """Warm every built-in formula cache entry for a selected instrument set."""

    def __init__(
        self,
        adapter: PrecomputeMarketDataAdapter,
        cache: IndicatorCache,
        registry: FormulaRegistry | None = None,
    ) -> None:
        self._adapter = adapter
        self._cache = cache
        self._registry = registry or FORMULA_REGISTRY

    def run(
        self,
        *,
        vipdoc_path: str | Path,
        universe: str = "all",
        universe_file: str | Path | None = None,
        instrument_types: tuple[InstrumentType, ...] | None = None,
        boards: tuple[InstrumentBoard, ...] | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> IndicatorPrecomputeReport:
        refs = self._adapter.list_stock_refs(
            vipdoc_path,
            universe,
            universe_file,
            instrument_types=instrument_types,
            boards=boards,
        )
        frames = self._batch_frames(refs)
        formula_ids = self._registry.all_formula_ids()
        minimum_bars = {
            formula_id: self._registry.formula(formula_id).minimum_bars
            for formula_id in formula_ids
        }
        before = self._cache.metrics()
        failures: Counter[str] = Counter()
        processed = 0
        skipped = 0
        progress_count = 0

        def report_progress() -> None:
            nonlocal progress_count
            progress_count += 1
            if progress_callback is not None:
                progress_callback(progress_count, len(refs))

        for offset in range(0, len(refs), _PRECOMPUTE_CHUNK_SIZE):
            batch = refs[offset : offset + _PRECOMPUTE_CHUNK_SIZE]
            prepared: dict[tuple[str, str], pd.DataFrame] = {}
            empty: set[tuple[str, str]] = set()
            failed: set[tuple[str, str]] = set()
            with ThreadPoolExecutor(
                max_workers=min(MAX_WORKERS, len(batch)),
                thread_name_prefix="indicator-precompute",
            ) as executor:
                preparation_futures = {
                    executor.submit(self._prepare_ref, ref, frames): ref for ref in batch
                }
                for future in as_completed(preparation_futures):
                    ref = preparation_futures[future]
                    key = (ref.market, ref.code)
                    try:
                        frame = future.result()
                    except Exception as exc:  # noqa: BLE001 - isolate one instrument
                        failed.add(key)
                        reason = str(exc).strip()[:200] or type(exc).__name__
                        failures[reason] += 1
                        logger.warning("指标预计算 %s 失败，已跳过", ref.code, exc_info=True)
                    else:
                        if frame.empty:
                            empty.add(key)
                        else:
                            prepared[key] = frame

            preloaded = 0
            if prepared:
                try:
                    preloaded = self._cache.preload(prepared, formula_ids)
                except (OSError, RuntimeError, ValueError):
                    logger.warning("指标缓存批量预加载失败，改用逐标的按需读取", exc_info=True)

            for ref in batch:
                key = (ref.market, ref.code)
                if key in failed or key in empty:
                    if key in empty:
                        skipped += 1
                    report_progress()

            compute_targets = [
                (ref, prepared[(ref.market, ref.code)])
                for ref in batch
                if (ref.market, ref.code) in prepared
            ]
            if compute_targets:
                computed_results: dict[tuple[str, str], Mapping[str, FormulaResult]] = {}
                eligible_total = sum(
                    sum(len(frame) >= minimum_bars[formula_id] for formula_id in formula_ids)
                    for _, frame in compute_targets
                )
                with ThreadPoolExecutor(
                    max_workers=min(MAX_WORKERS, len(compute_targets)),
                    thread_name_prefix="indicator-precompute",
                ) as executor:
                    compute_futures = {
                        executor.submit(
                            self._compute_ref,
                            ref,
                            frame,
                            formula_ids,
                            minimum_bars,
                        ): ref
                        for ref, frame in compute_targets
                    }
                    for future in as_completed(compute_futures):
                        ref = compute_futures[future]
                        try:
                            outcome = future.result()
                        except Exception as exc:  # noqa: BLE001 - isolate one instrument
                            reason = str(exc).strip()[:200] or type(exc).__name__
                            failures[reason] += 1
                            logger.warning("指标预计算 %s 失败，已跳过", ref.code, exc_info=True)
                        else:
                            processed += outcome.processed
                            skipped += outcome.skipped
                            if outcome.failure_reason is not None:
                                failures[outcome.failure_reason] += 1
                            if outcome.results is not None:
                                computed_results[(ref.market, ref.code)] = outcome.results
                        report_progress()
                if computed_results and preloaded < eligible_total:
                    self._cache.persist_results(prepared, computed_results)

        after = self._cache.metrics()
        return IndicatorPrecomputeReport(
            total_candidates=len(refs),
            processed=processed,
            skipped=skipped,
            errors=sum(failures.values()),
            computed=int(after["computed"]) - int(before["computed"]),
            cache_hits=int(after["hits"]) - int(before["hits"]),
            failure_reasons=failures,
        )

    def _prepare_ref(
        self,
        ref: StockRef,
        frames: Mapping[tuple[str, str], pd.DataFrame] | None,
    ) -> pd.DataFrame:
        key = (ref.market, ref.code)
        source = frames.get(key) if frames is not None else self._adapter.read_stock(ref)
        return _prepare_frame(source if source is not None else pd.DataFrame())

    def _compute_ref(
        self,
        ref: StockRef,
        frame: pd.DataFrame,
        formula_ids: tuple[str, ...],
        minimum_bars: Mapping[str, int],
    ) -> _PrecomputeOutcome:
        eligible = tuple(
            formula_id for formula_id in formula_ids if len(frame) >= minimum_bars[formula_id]
        )
        if not eligible:
            return _PrecomputeOutcome(skipped=1)
        results = self._cache.get_or_compute(
            frame,
            eligible,
            market=ref.market,
            code=ref.code,
            persistent_read=False,
            persist=False,
        )
        return _PrecomputeOutcome(processed=1, results=results)

    def _batch_frames(
        self,
        refs: list[StockRef],
    ) -> Mapping[tuple[str, str], pd.DataFrame] | None:
        reader = getattr(self._adapter, "read_many_stocks", None)
        if reader is None:
            return None
        combined = reader(refs)
        frames: dict[tuple[str, str], pd.DataFrame] = {}
        if combined.empty:
            return frames
        for key, group in combined.groupby(["market", "code"], sort=False):
            frames[(str(key[0]), str(key[1]))] = group.reset_index(drop=True)
        return frames


def _prepare_frame(frame: pd.DataFrame) -> pd.DataFrame:
    prepared = frame.copy(deep=True)
    if prepared.empty:
        return prepared
    validate_market_data(prepared)
    prepared["date"] = pd.to_datetime(prepared["date"], errors="coerce")
    if prepared["date"].isna().any():
        raise ValueError("行情数据包含无效日期")
    return prepared.sort_values("date", kind="stable").reset_index(drop=True)
