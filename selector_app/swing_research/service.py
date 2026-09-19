"""Cut a whole market into swings and describe what those swings look like.

The service exists so the question "what counts as a big move?" can be
answered with data instead of taste. It reads the universe in bounded batches,
cuts every stock with each requested threshold, and reduces the result to
duration and amplitude distributions - never materialising a full panel of
segments, which is what would otherwise make a 5,000-stock scan fall over.
"""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory, gettempdir

import numpy as np
import pandas as pd

from selector_app.formulas.registry import FORMULA_REGISTRY
from selector_app.market_data.adapter import BatchMarketDataAdapter
from selector_app.market_data.models import StockRef
from selector_app.strategy_discovery.catalog import IndicatorCatalog, default_indicator_catalog

from .conditions import build_condition_plan
from .labels import LABEL_DISPLAY_NAMES, bar_labels
from .models import MAX_PANEL_BYTES, SwingIndicatorConfig, SwingOverviewConfig
from .overview import SwingAccumulator
from .scoring import DEFAULT_SEED, IndicatorScore, annotate_significance, score_indicator
from .segments import atr_percent, split_swings

ProgressCallback = Callable[[int, int], None] | None
PANEL_DTYPE = np.float32
PANEL_DIR_PREFIX = "swing-panel-"
#: A panel directory older than this can only be debris from a killed run.
STALE_PANEL_SECONDS = 3600.0


def _clear_stale_panels() -> None:
    """Delete panel scratch directories that a crashed run left behind.

    A healthy run removes its own directory, but an OOM kill arrives as
    SIGKILL and skips that cleanup, potentially leaving gigabytes in the temp
    root. Anything older than an hour is debris; a run still in progress is
    skipped because its directory is recent. On POSIX, unlinking a file that a
    live process has mmap'd is harmless - the mapper keeps its descriptor.
    """

    cutoff = time.time() - STALE_PANEL_SECONDS
    for entry in Path(gettempdir()).glob(f"{PANEL_DIR_PREFIX}*"):
        try:
            if entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            continue


class SwingResearchService:
    """Segment a universe and summarise the legs it produced."""

    def __init__(self, adapter: BatchMarketDataAdapter) -> None:
        self._adapter = adapter

    def overview(
        self,
        config: SwingOverviewConfig,
        progress_callback: ProgressCallback = None,
    ) -> dict[str, object]:
        """Run one overview; returns a JSON-ready payload."""

        resolved = config.normalized()
        refs = self._list_refs(resolved)
        total = len(refs)
        accumulators = {
            multiple: SwingAccumulator(multiple, min_bars=resolved.min_bars)
            for multiple in resolved.reversal_multiples
        }
        # Enough history for the ATR smoothing to settle before the first leg
        # is allowed to start; three times the window is plenty for Wilder.
        # Only meaningful when a window was requested - without a start date
        # the read already begins at the first bar on record, and the store
        # rejects a warm-up it cannot anchor.
        warmup = resolved.atr_window * 3 + 10 if resolved.start_date else 0
        start_timestamp = pd.Timestamp(resolved.start_date) if resolved.start_date else None
        end_timestamp = pd.Timestamp(resolved.end_date) if resolved.end_date else None

        scanned = 0
        first_seen: pd.Timestamp | None = None
        last_seen: pd.Timestamp | None = None
        for offset in range(0, total, resolved.batch_size):
            batch = refs[offset : offset + resolved.batch_size]
            frame = self._adapter.read_many_stocks(
                batch,
                start_date=resolved.start_date,
                end_date=resolved.end_date,
                warmup_bars=warmup,
            )
            if not frame.empty:
                timestamps = pd.to_datetime(frame["date"])
                batch_first = timestamps.min()
                batch_last = timestamps.max()
                first_seen = batch_first if first_seen is None else min(first_seen, batch_first)
                last_seen = batch_last if last_seen is None else max(last_seen, batch_last)
                for _, group in frame.groupby(["market", "code"], sort=True):
                    scanned += 1
                    self._accumulate(
                        group,
                        accumulators,
                        resolved,
                        start_timestamp,
                        end_timestamp,
                    )
            if progress_callback is not None:
                progress_callback(min(offset + len(batch), total), total)

        years = _years_between(first_seen, last_seen)
        return {
            "stock_count": total,
            "scanned_stocks": scanned,
            "min_bars": resolved.min_bars,
            "atr_window": resolved.atr_window,
            "date_range": {
                "start": first_seen.strftime("%Y-%m-%d") if first_seen is not None else None,
                "end": last_seen.strftime("%Y-%m-%d") if last_seen is not None else None,
                "years": years,
            },
            "scenarios": [
                accumulator.summary(years=years).to_dict() for accumulator in accumulators.values()
            ],
        }

    def indicator_study(
        self,
        config: SwingIndicatorConfig,
        progress_callback: ProgressCallback = None,
    ) -> dict[str, object]:
        """Score every indicator against every requested swing label."""

        resolved = config.normalized()
        value_ids = self._resolve_value_ids(resolved)
        refs = self._list_refs(resolved)
        empty = _empty_study_payload(resolved, value_ids, stock_count=len(refs))
        if not refs or not value_ids:
            empty["reason"] = "没有可研究的股票或指标"
            return empty

        calendar, rows = self._scan_calendar(refs, resolved, progress_callback)
        if calendar.size == 0 or not rows:
            empty["reason"] = "选定区间内没有任何日线数据"
            return empty

        _clear_stale_panels()
        with TemporaryDirectory(prefix=PANEL_DIR_PREFIX) as directory:
            path = os.path.join(directory, "panel.dat")
            shape = (len(value_ids), len(rows), calendar.size)
            required = int(np.prod(shape)) * np.dtype(PANEL_DTYPE).itemsize
            if required > MAX_PANEL_BYTES:
                raise ValueError(
                    f"面板需要 {required / 1024**3:.1f} GB 临时空间，超出上限 "
                    f"{MAX_PANEL_BYTES / 1024**3:.0f} GB；请缩短区间、减少股票数或"
                    "减少指标数量后重试"
                )
            panel = np.memmap(path, dtype=PANEL_DTYPE, mode="w+", shape=shape)
            panel[:] = np.nan
            labels = np.full(
                (len(resolved.labels), len(rows), calendar.size),
                np.nan,
                dtype=PANEL_DTYPE,
            )
            try:
                filled = self._fill_panel(
                    refs,
                    resolved,
                    value_ids,
                    calendar,
                    rows,
                    panel,
                    labels,
                    progress_callback,
                )
                scores = self._score_panel(panel, labels, resolved, value_ids)
            finally:
                del panel

        if filled == 0:
            empty["reason"] = "没有股票能同时在区间内提供指标与波段标签"
            return empty

        scores = annotate_significance(scores, alpha=resolved.alpha)
        ordered = sorted(
            scores,
            key=lambda item: (not item.significant, -item.ic_mean, -(item.icir or 0.0)),
        )
        significant_count = sum(1 for item in ordered if item.significant)
        payload = _empty_study_payload(resolved, value_ids, stock_count=len(refs))
        payload.update(
            {
                "scanned_stocks": filled,
                "date_range": {
                    "start": str(calendar[0].astype("datetime64[D]")),
                    "end": str(calendar[-1].astype("datetime64[D]")),
                    "days": int(calendar.size),
                },
                "scores": [item.to_dict() for item in ordered],
                "significant_count": significant_count,
                "reason": _study_reason(ordered, significant_count),
                # Ready-to-paste rules: the study's findings expressed in the
                # shape the condition research consumes as its baseline.
                "condition_plan": build_condition_plan(ordered),
            }
        )
        return payload

    def _resolve_value_ids(self, config: SwingIndicatorConfig) -> tuple[str, ...]:
        """Rankable technical outputs, or the caller's subset of them.

        Only ``technical`` is supported: the other formulas expose intermediate
        variables of one specific trading formula rather than general-purpose
        measurements, so treating them as candidate market factors would be a
        category error.
        """

        catalog: IndicatorCatalog = default_indicator_catalog()
        technical = FORMULA_REGISTRY.formula("technical")
        available = {
            value_id for value_id in catalog.rankable_ids if value_id.startswith("technical.")
        }
        if config.value_ids:
            requested = tuple(catalog.canonical_id(item) for item in config.value_ids)
            unknown = tuple(item for item in requested if item not in available)
            if unknown:
                raise ValueError(
                    f"以下指标不能用于波段研究: {', '.join(unknown)}；"
                    "只支持 technical 公式中可排序的无量纲指标"
                )
            return tuple(item for item in requested if item in available)
        ordered = tuple(
            f"technical.{name}"
            for name in technical.value_names
            if f"technical.{name}" in available
        )
        return ordered

    def _scan_calendar(
        self,
        refs: list[StockRef],
        config: SwingIndicatorConfig,
        progress_callback: ProgressCallback,
    ) -> tuple[np.ndarray, dict[str, int]]:
        """Collect the trading days and the stocks that actually have bars.

        The panel needs one shared date axis and one row per stock, and the
        only honest way to get either is to look first. The read deliberately
        skips warm-up so the axis holds exactly the study window and the row
        map only names stocks that will contribute at least one cell.
        """

        seen_days: set[pd.Timestamp] = set()
        rows: dict[str, int] = {}
        total = len(refs)
        for offset in range(0, total, config.batch_size):
            batch = refs[offset : offset + config.batch_size]
            frame = self._adapter.read_many_stocks(
                batch,
                start_date=config.start_date,
                end_date=config.end_date,
            )
            if not frame.empty:
                seen_days.update(pd.to_datetime(frame["date"]).unique())
                for key in pd.unique(frame["market"].astype(str) + frame["code"].astype(str)):
                    if key not in rows:
                        rows[key] = len(rows)
            if progress_callback is not None:
                progress_callback(min(offset + len(batch), total), total * 2)
        if not seen_days:
            return np.array([], dtype="datetime64[ns]"), {}
        return np.array(sorted(seen_days), dtype="datetime64[ns]"), rows

    def _fill_panel(
        self,
        refs: list[StockRef],
        config: SwingIndicatorConfig,
        value_ids: tuple[str, ...],
        dates: np.ndarray,
        rows: dict[str, int],
        panel: np.memmap,
        labels: np.ndarray,
        progress_callback: ProgressCallback,
    ) -> int:
        """Second pass: compute indicators, cut swings, write both to the panel."""

        technical = FORMULA_REGISTRY.formula("technical")
        names = tuple(value_id.split(".", 1)[1] for value_id in value_ids)
        # Warm-up only makes sense against a window: without a start date the
        # read already begins at the first bar on record, and the store rejects
        # a warm-up it cannot anchor.
        warmup = technical.recommended_bars if config.start_date else 0
        start_timestamp = pd.Timestamp(config.start_date) if config.start_date else None
        end_timestamp = pd.Timestamp(config.end_date) if config.end_date else None
        calendar_index = pd.DatetimeIndex(dates)

        filled = 0
        total = len(refs)
        for offset in range(0, total, config.batch_size):
            batch = refs[offset : offset + config.batch_size]
            frame = self._adapter.read_many_stocks(
                batch,
                start_date=config.start_date,
                end_date=config.end_date,
                warmup_bars=warmup,
            )
            if not frame.empty:
                keys = frame["market"].astype(str) + frame["code"].astype(str)
                for key, group in frame.groupby(keys, sort=True):
                    row = rows.get(str(key))
                    if row is None:
                        continue
                    if _write_stock(
                        group,
                        config,
                        names,
                        calendar_index,
                        start_timestamp,
                        end_timestamp,
                        panel,
                        labels,
                        row,
                    ):
                        filled += 1
            if progress_callback is not None:
                progress_callback(total + min(offset + len(batch), total), total * 2)
        return filled

    def _score_panel(
        self,
        panel: np.memmap,
        labels: np.ndarray,
        config: SwingIndicatorConfig,
        value_ids: tuple[str, ...],
    ) -> list[IndicatorScore]:
        catalog = default_indicator_catalog()
        scores: list[IndicatorScore] = []
        for value_index, value_id in enumerate(value_ids):
            matrix = np.asarray(panel[value_index], dtype=float)
            for label_index, label in enumerate(config.labels):
                scores.append(
                    score_indicator(
                        matrix,
                        np.asarray(labels[label_index], dtype=float),
                        value_id=value_id,
                        display_name=catalog.display_name(value_id),
                        label=label,
                        min_cross_section=config.min_cross_section,
                        block_size=config.block_size,
                        permutations=config.permutations,
                        seed=DEFAULT_SEED,
                        alpha=config.alpha,
                    )
                )
        return scores

    def _list_refs(self, config: SwingOverviewConfig | SwingIndicatorConfig) -> list[StockRef]:
        refs = self._adapter.list_stock_refs(
            config.vipdoc_path,
            config.universe,
            instrument_types=config.instrument_types or None,
            boards=config.boards or None,
        )
        if config.limit_stocks is not None:
            refs = refs[: config.limit_stocks]
        return refs

    @staticmethod
    def _accumulate(
        group: pd.DataFrame,
        accumulators: dict[float, SwingAccumulator],
        config: SwingOverviewConfig,
        start_timestamp: pd.Timestamp | None,
        end_timestamp: pd.Timestamp | None,
    ) -> None:
        """Cut one stock once per threshold, keeping only in-window starts.

        A leg is in the study when it *started* inside the window, even if it
        ran past the end date: the start is what the study will later try to
        predict. Warm-up bars therefore only feed the ATR, never the counts.
        """

        if len(group) < 2:
            return
        timestamps = pd.to_datetime(group["date"])
        keep = pd.Series(True, index=group.index)
        if start_timestamp is not None:
            keep &= timestamps >= start_timestamp
        if end_timestamp is not None:
            keep &= timestamps <= end_timestamp
        keep_values = keep.to_numpy(dtype=bool)
        atr = atr_percent(group, window=config.atr_window)
        for multiple, accumulator in accumulators.items():
            segments = split_swings(
                group,
                reversal_multiple=multiple,
                atr_window=config.atr_window,
                include_pending=config.include_pending,
                atr=atr,
            )
            if keep_values.all():
                accumulator.add(segments)
            else:
                accumulator.add([item for item in segments if keep_values[item.start_index]])


def _study_reason(scores: list[IndicatorScore], significant: int) -> str | None:
    """Say what the board means, including when it means less than it looks."""

    if not significant:
        if not scores:
            return "没有可用于评估的指标样本"
        return "没有任何指标在多重检验校正后仍然显著，建议放宽区间或改用更大的波段阈值再试"
    if significant * 2 <= len(scores):
        return None
    # Twenty technical outputs are not twenty independent hypotheses: they are
    # variations on momentum, mean reversion and volatility, so a board full of
    # hits usually means one signal wearing twenty hats. Saying so is cheaper
    # than letting the user find out later, in a backtest that looks great.
    return (
        f"{len(scores)} 项检验里有 {significant} 项显著，但这些指标彼此高度相关，"
        "很可能只是同一类信号（超跌反弹或动量延续）的不同写法；"
        "请以信息系数最高、信息比率最稳的一两个为准，不要叠加使用"
    )


def _write_stock(
    group: pd.DataFrame,
    config: SwingIndicatorConfig,
    names: tuple[str, ...],
    calendar_index: pd.DatetimeIndex,
    start_timestamp: pd.Timestamp | None,
    end_timestamp: pd.Timestamp | None,
    panel: np.memmap,
    labels: np.ndarray,
    row: int,
) -> bool:
    """Write one stock's indicator rows and swing labels into the panel."""

    if len(group) < 2:
        return False
    timestamps = pd.DatetimeIndex(pd.to_datetime(group["date"]))
    keep = np.ones(len(group), dtype=bool)
    if start_timestamp is not None:
        keep &= timestamps >= start_timestamp
    if end_timestamp is not None:
        keep &= timestamps <= end_timestamp
    kept = np.nonzero(keep)[0]
    if kept.size == 0:
        return False

    result = FORMULA_REGISTRY.formula("technical").calculate(group)
    values = np.column_stack([np.asarray(result.values[name], dtype=float) for name in names])
    segments = split_swings(
        group,
        reversal_multiple=config.reversal_multiple,
        atr_window=config.atr_window,
        include_pending=False,
        atr=atr_percent(group, window=config.atr_window),
    )
    targets = bar_labels(group, segments, min_bars=config.min_bars, horizon=config.horizon)

    positions = calendar_index.get_indexer(timestamps[keep])
    valid = positions >= 0
    if not valid.any():
        return False
    columns = positions[valid]
    source = kept[valid]
    panel[:, row, columns] = values[source, :].T
    for index, label in enumerate(config.labels):
        labels[index, row, columns] = targets[label][source]
    return bool(np.isfinite(panel[:, row, columns]).any())


def _empty_study_payload(
    config: SwingIndicatorConfig,
    value_ids: tuple[str, ...],
    *,
    stock_count: int,
) -> dict[str, object]:
    return {
        "stock_count": stock_count,
        "scanned_stocks": 0,
        "reversal_multiple": config.reversal_multiple,
        "min_bars": config.min_bars,
        "atr_window": config.atr_window,
        "horizon": config.horizon,
        "permutations": config.permutations,
        "block_size": config.block_size,
        "alpha": config.alpha,
        "value_count": len(value_ids),
        "labels": [
            {"id": label, "display_name": LABEL_DISPLAY_NAMES[label]} for label in config.labels
        ],
        "date_range": {"start": None, "end": None, "days": 0},
        "scores": [],
        "significant_count": 0,
        "reason": None,
    }


def _years_between(first: pd.Timestamp | None, last: pd.Timestamp | None) -> float | None:
    if first is None or last is None or pd.isna(first) or pd.isna(last):
        return None
    days = (last - first).days
    return days / 365.25 if days > 0 else None
