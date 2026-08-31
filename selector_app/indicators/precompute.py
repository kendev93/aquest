"""Batch materialization of built-in indicator results for the local store."""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pandas as pd

from selector_app.formulas.common import validate_market_data
from selector_app.formulas.registry import FORMULA_REGISTRY, FormulaRegistry
from selector_app.indicators.cache import IndicatorCache
from selector_app.market_data.models import InstrumentBoard, InstrumentType, StockRef

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int], None]


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

        for current, ref in enumerate(refs, start=1):
            try:
                source = (
                    frames.get((ref.market, ref.code))
                    if frames is not None
                    else self._adapter.read_stock(ref)
                )
                frame = _prepare_frame(source if source is not None else pd.DataFrame())
                eligible = tuple(
                    formula_id
                    for formula_id in formula_ids
                    if len(frame) >= minimum_bars[formula_id]
                )
                if not eligible:
                    skipped += 1
                else:
                    self._cache.get_or_compute(
                        frame,
                        eligible,
                        market=ref.market,
                        code=ref.code,
                    )
                    processed += 1
            except Exception as exc:  # noqa: BLE001 - one bad instrument must not abort warming
                reason = str(exc).strip()[:200] or type(exc).__name__
                failures[reason] += 1
                logger.warning("指标预计算 %s 失败，已跳过", ref.code, exc_info=True)
            if progress_callback is not None:
                progress_callback(current, len(refs))

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
