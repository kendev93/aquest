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
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from tempfile import TemporaryDirectory, gettempdir

import numpy as np
import pandas as pd

from selector_app.formulas.registry import FORMULA_REGISTRY
from selector_app.market_data.adapter import BatchMarketDataAdapter
from selector_app.market_data.models import StockRef
from selector_app.strategy_discovery.catalog import IndicatorCatalog, default_indicator_catalog

from .combinations import (
    HOLDOUT_FINALIST_LIMIT_PER_LABEL,
    PAIR_COMPONENT_LIMIT,
    PAIR_OPERATION_NAMES,
    combine_rank_panels,
    fast_rank_ic,
    percentile_rank_panel,
    split_trading_days,
)
from .conditions import TRADEABLE_LABELS, build_condition_plan
from .labels import LABEL_DISPLAY_NAMES, bar_labels
from .models import MAX_PANEL_BYTES, SwingIndicatorConfig, SwingOverviewConfig
from .overview import SwingAccumulator
from .scoring import (
    DEFAULT_SEED,
    MIN_IC_DAYS,
    IndicatorScore,
    annotate_significance,
    score_indicator,
)
from .segments import atr_percent, split_swings
from .values import custom_value_ids, display_name_for, formula_groups

ProgressCallback = Callable[[int, int], None] | None
#: ``indicator_study`` reports ``(current, total, stage)``. The stage is what
#: the UI prints; the numbers span calendar read, panel fill, split scoring,
#: pair evaluation and the final untouched holdout pass.
StudyProgressCallback = Callable[[int, int, str], None] | None
#: Weighted work units reported while scoring.
ScoreProgressCallback = Callable[[float, str], None] | None
#: A calendar-scan step reads one stock's bars; a panel-fill step re-reads them
#: with warm-up, computes every indicator and cuts the swings. Measured on a
#: 1,000-stock / five-year run: 12.7s against 2.1s for the same number of steps.
_PANEL_FILL_WEIGHT = 6.0
#: Approximate cost of one permutation-tested indicator/label score, in scan
#: steps per stock. Fast rank-only training and pair ranking receive a smaller
#: weight because they do not build the date-by-date permutation matrix.
_SCORE_PAIR_WEIGHT = 0.00125
_FAST_SCORE_WEIGHT = 0.00015
_STABLE_DIRECTION_MIN_IC = 0.01
PANEL_DTYPE = np.float32
PANEL_DIR_PREFIX = "swing-panel-"
#: How much free RAM a panel must leave for itself. The memmap's page cache, the
#: resident label array and the float64 slice the scoring pass copies all sit on
#: top of the file size, so peak memory is roughly twice ``required``. Measured
#: the hard way: a 6 GB panel on an 8 GB container killed the backend.
PANEL_MEMORY_SAFETY = 2
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


def _available_memory_bytes() -> int | None:
    """Free RAM as the kernel sees it, or None where that cannot be read."""

    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        return None
    return None


def _check_panel_budget(required: int, *, memory_extra_bytes: int = 0) -> None:
    """Refuse a run that cannot fit, naming the wall it hit.

    Three different walls, and they need different advice.

    ``MAX_PANEL_BYTES`` is our own ceiling: a request past it is not dangerous,
    just slow enough that finishing was never the likely outcome. The free space
    in the temp root is next - the panel is a real file, and discovering that
    halfway through, as the run dies on ENOSPC, is much worse than hearing it up
    front.

    RAM is the one that bites. The panel is a memmap, but the kernel caches it,
    the labels are resident, and scoring copies slices and a bounded set of
    percentile-rank panels into memory. Refusing before the run beats taking the
    whole process down with it.
    """

    gib = 1024**3
    if required > MAX_PANEL_BYTES:
        raise ValueError(
            f"面板需要 {required / gib:.1f} GB 临时空间，超出上限 "
            f"{MAX_PANEL_BYTES / gib:.0f} GB；请缩短区间、减少股票数或"
            "减少指标数量后重试"
        )

    raw = os.environ.get("SWING_PANEL_MAX_MEMORY_BYTES", "").strip()
    memory_ceiling = int(raw) if raw.isdigit() else 0
    if memory_ceiling == 0:
        available = _available_memory_bytes()
        memory_ceiling = available // PANEL_MEMORY_SAFETY if available is not None else 0
    projected_panel_equivalent = required + memory_extra_bytes // PANEL_MEMORY_SAFETY
    if memory_ceiling > 0 and projected_panel_equivalent > memory_ceiling:
        raise ValueError(
            f"面板和双指标打分暂存预计需要 "
            f"{(required * PANEL_MEMORY_SAFETY + memory_extra_bytes) / gib:.1f} GB，"
            "但当前可用内存 "
            f"{memory_ceiling * PANEL_MEMORY_SAFETY / gib:.1f} GB 不足以安全容纳，"
            "运行中很可能把后端拖垮；请减少股票数或缩短区间"
            "（确要强行运行可设置 SWING_PANEL_MAX_MEMORY_BYTES）"
        )

    try:
        free = shutil.disk_usage(gettempdir()).free
    except OSError:  # pragma: no cover - a temp root we cannot stat is exotic
        return
    if required > free:
        raise ValueError(
            f"面板需要 {required / gib:.1f} GB 临时空间，但临时目录只剩 "
            f"{free / gib:.1f} GB；请清理磁盘后重试"
        )


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
        progress_callback: StudyProgressCallback = None,
    ) -> dict[str, object]:
        """Score indicators and bounded pair features on chronological splits."""

        def report(current: int, total: int, stage: str) -> None:
            if progress_callback is not None:
                progress_callback(min(current, total), total, stage)

        resolved = config.normalized()
        value_ids = self._resolve_value_ids(resolved)
        refs = self._list_refs(resolved)
        ref_total = len(refs)
        scan_units = float(ref_total)
        fill_units = scan_units * _PANEL_FILL_WEIGHT
        tradeable_labels = tuple(label for label in resolved.labels if label in TRADEABLE_LABELS)
        validation_score_count = len(value_ids) * len(resolved.labels)
        training_scores = len(value_ids) * len(resolved.labels)
        unordered_component_pairs = PAIR_COMPONENT_LIMIT * (PAIR_COMPONENT_LIMIT - 1) // 2
        max_pairs_per_label = unordered_component_pairs * len(PAIR_OPERATION_NAMES)
        max_pair_scores = len(tradeable_labels) * max_pairs_per_label
        max_holdout_scores = (
            len(tradeable_labels) * HOLDOUT_FINALIST_LIMIT_PER_LABEL * 2
        )
        permutation_score_weight = max(
            1.0, _SCORE_PAIR_WEIGHT * (1 + resolved.permutations) * ref_total
        )
        fast_score_weight = max(0.1, _FAST_SCORE_WEIGHT * ref_total)
        pair_score_weight = fast_score_weight * 2
        score_units = max(
            validation_score_count * permutation_score_weight
            + max_holdout_scores * permutation_score_weight
            + training_scores * fast_score_weight
            + len(tradeable_labels) * PAIR_COMPONENT_LIMIT * fast_score_weight
            + max_pair_scores * pair_score_weight,
            1.0,
        )
        total_steps = max(int(round(scan_units + fill_units + score_units)), 1)
        score_done = 0.0

        def panel_progress(current: int, _total: int) -> None:
            if current <= ref_total:
                done = float(current)
                stage = "正在扫描交易日"
            else:
                done = scan_units + (current - ref_total) * _PANEL_FILL_WEIGHT
                stage = "正在计算指标与切分波段"
            report(int(round(done)), total_steps, stage)

        def score_progress(units: float, stage: str) -> None:
            nonlocal score_done
            score_done = min(score_done + units, score_units)
            earned = scan_units + fill_units + score_done
            report(int(round(earned)), total_steps, stage)

        empty = _empty_study_payload(resolved, value_ids, stock_count=len(refs))
        if not refs or not value_ids:
            empty["reason"] = "没有可研究的股票或指标"
            report(total_steps, total_steps, "没有可研究的对象")
            return empty

        calendar, rows = self._scan_calendar(refs, resolved, panel_progress)
        if calendar.size == 0 or not rows:
            empty["reason"] = "选定区间内没有任何日线数据"
            report(total_steps, total_steps, "区间内没有数据")
            return empty

        _clear_stale_panels()
        single_finalists_by_label: dict[str, list[IndicatorScore]] = {}
        with TemporaryDirectory(prefix=PANEL_DIR_PREFIX) as directory:
            path = os.path.join(directory, "panel.dat")
            shape = (len(value_ids), len(rows), calendar.size)
            cell = np.dtype(PANEL_DTYPE).itemsize
            # The panel is a memmap, but the labels sit in RAM beside it, so a
            # budget that only counted the file would understate what the run
            # actually asks the machine for.
            panel_cells = int(np.prod(shape))
            label_cells = len(resolved.labels) * len(rows) * calendar.size
            required = (panel_cells + label_cells) * cell
            pair_scratch_cells = (
                (PAIR_COMPONENT_LIMIT + 24) * len(rows) * calendar.size
                if tradeable_labels
                else 0
            )
            _check_panel_budget(required, memory_extra_bytes=pair_scratch_cells * cell)
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
                    panel_progress,
                )
                if filled == 0:
                    empty["reason"] = "没有股票能同时在区间内提供指标与波段标签"
                    report(total_steps, total_steps, "没有可用的样本")
                    return empty

                split_slices, split_metadata = split_trading_days(calendar, resolved.horizon)
                split_metadata["method"] = "按交易日顺序固定切分 60/20/20，并在边界清除前视窗口"
                split_metadata["holdout_policy"] = "只复核验证段选出的候选，不参与筛选或排序"
                split_metadata["attribution_note"] = (
                    "波段高低点标签仅作事后归因；只有近期将启动上涨/下跌标签可交易"
                )
                train_slice = split_slices["train"]
                validation_slice = split_slices["validation"]
                holdout_slice = split_slices["holdout"]

                train_metrics = self._rank_panel(
                    panel,
                    labels,
                    resolved,
                    value_ids,
                    train_slice,
                    lambda: score_progress(fast_score_weight, "正在计算训练段指标"),
                )
                validation_scores = self._score_panel(
                    panel,
                    labels,
                    resolved,
                    value_ids,
                    validation_slice,
                    lambda _done, _stage: score_progress(
                        permutation_score_weight, "正在逐日横截面打分"
                    ),
                )
                validation_scores = annotate_significance(
                    validation_scores, alpha=resolved.alpha
                )

                combination_scores, combination_counts, pair_holdout_scores = (
                    self._score_combinations(
                        panel,
                        labels,
                        resolved,
                        value_ids,
                        train_metrics,
                        train_slice,
                        validation_slice,
                        holdout_slice,
                        tradeable_labels,
                        fast_score_weight,
                        pair_score_weight,
                        permutation_score_weight,
                        score_progress,
                    )
                )

                single_holdout_scores: list[IndicatorScore] = []
                value_index = {value_id: index for index, value_id in enumerate(value_ids)}
                label_index = {label: index for index, label in enumerate(resolved.labels)}
                for label in tradeable_labels:
                    finalists = _rank_single_comparison_candidates(
                        (
                            score
                            for score in validation_scores
                            if score.label == label and score.ic_days > 0
                        ),
                        train_metrics,
                    )[:HOLDOUT_FINALIST_LIMIT_PER_LABEL]
                    single_finalists_by_label[label] = finalists
                    for score in finalists:
                        index = value_index[score.value_id]
                        single_holdout_scores.append(
                            score_indicator(
                                np.asarray(panel[index][:, holdout_slice], dtype=float),
                                np.asarray(
                                    labels[label_index[label]][:, holdout_slice], dtype=float
                                ),
                                value_id=score.value_id,
                                display_name=score.display_name,
                                label=label,
                                min_cross_section=resolved.min_cross_section,
                                block_size=resolved.block_size,
                                permutations=resolved.permutations,
                                seed=DEFAULT_SEED,
                                alpha=resolved.alpha,
                            )
                        )
                        score_progress(
                            permutation_score_weight, "正在留出段复核单指标候选"
                        )
            finally:
                del panel

        report(total_steps, total_steps, "正在整理结论")

        ordered = _rank_validation_scores(validation_scores)
        significant_count = sum(1 for item in ordered if item.significant)
        all_holdout_scores = single_holdout_scores + pair_holdout_scores
        annotated_holdout = annotate_significance(all_holdout_scores, alpha=resolved.alpha)
        holdout_by_key = {
            (item.value_id, item.label): item for item in annotated_holdout
        }
        comparison_candidates = _build_comparison_board(
            tradeable_labels,
            single_finalists_by_label,
            combination_scores,
            train_metrics,
            holdout_by_key,
        )

        score_payloads: list[dict[str, object]] = []
        for score in ordered:
            key = (score.value_id, score.label)
            train = train_metrics.get(key)
            holdout = holdout_by_key.get(key)
            item = score.to_dict()
            item.update(
                {
                    "train": train,
                    "validation": _score_metrics(score),
                    "holdout": _score_metrics(holdout) if holdout is not None else None,
                    "holdout_evaluated": holdout is not None,
                    "training_validation_direction_stable": _directions_match(train, score),
                    "direction_stable": _direction_stable(train, score, holdout),
                }
            )
            score_payloads.append(item)

        for candidate in combination_scores:
            holdout = holdout_by_key.get((str(candidate["candidate_id"]), str(candidate["label"])))
            candidate["holdout"] = _score_metrics(holdout) if holdout is not None else None
            candidate["direction_stable"] = _direction_stable(
                candidate.get("train"), candidate.get("validation"), holdout
            )

        payload = _empty_study_payload(resolved, value_ids, stock_count=len(refs))
        payload.update(
            {
                "scanned_stocks": filled,
                "date_range": {
                    "start": str(calendar[0].astype("datetime64[D]")),
                    "end": str(calendar[-1].astype("datetime64[D]")),
                    "days": int(calendar.size),
                },
                "split_metadata": split_metadata,
                "scores": score_payloads,
                "comparison_candidates": comparison_candidates,
                "comparison_summary": {
                    "finalists_per_type_per_label": HOLDOUT_FINALIST_LIMIT_PER_LABEL,
                    "ranking_basis": (
                        "训练到验证方向一致优先，其次按验证段 |IC| 与 ICIR 排序"
                    ),
                    "validation_q_policy": (
                        "组合全量验证池未做置换检验；单指标验证 q 仅展示，不用于混合排序"
                    ),
                    "holdout_q_policy": (
                        "对入围单指标与组合统一做多重检验校正，仅用于复核"
                    ),
                    "holdout_usage": "不参与候选筛选或排序",
                },
                "combination_scores": combination_scores,
                "combination_summary": {
                    "component_limit_per_label": PAIR_COMPONENT_LIMIT,
                    "finalists_per_label": HOLDOUT_FINALIST_LIMIT_PER_LABEL,
                    "candidate_counts": combination_counts,
                    "operations": dict(PAIR_OPERATION_NAMES),
                    "ranking_basis": (
                        "训练段选组件，训练/验证方向一致优先，"
                        "再按验证段 |IC| 与 ICIR 排序"
                    ),
                    "holdout_usage": "仅展示验证段前列候选的独立表现，不改变候选排序",
                    "value_scale": "每个交易日先转为横截面分位；分位差、分位比及分位上/下穿",
                    "inverse_ratio_deduplicated": True,
                    "direct_rule_support": False,
                },
                "significant_count": significant_count,
                "reason": _study_reason(ordered, significant_count),
                # Ready-to-paste rules: the study's findings expressed in the
                # shape the condition research consumes as its baseline.
                "condition_plan": build_condition_plan(ordered),
            }
        )
        return payload

    def _resolve_value_ids(self, config: SwingIndicatorConfig) -> tuple[str, ...]:
        """Rankable technical outputs plus the allow-listed custom ones.

        The older custom formulas are allow-listed by prior cross-section
        measurements. 主力吸筹 and 突破信号 are also allow-listed for the requested
        exploration; their pulse outputs can be sparse, which is reported by the
        resulting sample counts.
        """

        catalog: IndicatorCatalog = default_indicator_catalog()
        technical = FORMULA_REGISTRY.formula("technical")
        available = {
            value_id for value_id in catalog.rankable_ids if value_id.startswith("technical.")
        }
        if config.include_custom:
            available.update(custom_value_ids())
        if config.value_ids:
            requested = tuple(catalog.canonical_id(item) for item in config.value_ids)
            unknown = tuple(item for item in requested if item not in available)
            if unknown:
                raise ValueError(
                    f"以下指标不能用于波段研究: {', '.join(unknown)}；"
                    "只支持 technical 公式中可排序的无量纲指标，"
                    "以及自定义指标白名单中的输出"
                )
            return tuple(item for item in requested if item in available)
        ordered = tuple(
            f"technical.{name}"
            for name in technical.value_names
            if f"technical.{name}" in available
        )
        if config.include_custom:
            ordered += tuple(item for item in custom_value_ids() if item in available)
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

        groups = formula_groups(value_ids)
        # Every involved formula needs its own warm-up, and the longest one wins:
        # a formula computed on too little history returns NaNs, which would
        # silently shrink the cross-section instead of failing loudly.
        warmup = max(FORMULA_REGISTRY.formula(formula_id).recommended_bars for formula_id in groups)
        # Warm-up only makes sense against a window: without a start date the
        # read already begins at the first bar on record, and the store rejects
        # a warm-up it cannot anchor.
        warmup = warmup if config.start_date else 0
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
                        groups,
                        len(value_ids),
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

    def _rank_panel(
        self,
        panel: np.memmap,
        labels: np.ndarray,
        config: SwingIndicatorConfig,
        value_ids: tuple[str, ...],
        dates: slice,
        on_score: Callable[[], None],
    ) -> dict[tuple[str, str], dict[str, object]]:
        """Measure all singles quickly for component selection and split display."""

        results: dict[tuple[str, str], dict[str, object]] = {}
        for value_index, value_id in enumerate(value_ids):
            values = np.asarray(panel[value_index][:, dates], dtype=float)
            for label_index, label in enumerate(config.labels):
                results[(value_id, label)] = fast_rank_ic(
                    values,
                    np.asarray(labels[label_index][:, dates], dtype=float),
                    min_cross_section=config.min_cross_section,
                )
                on_score()
        return results

    def _score_combinations(
        self,
        panel: np.memmap,
        labels: np.ndarray,
        config: SwingIndicatorConfig,
        value_ids: tuple[str, ...],
        train_metrics: dict[tuple[str, str], dict[str, object]],
        train_dates: slice,
        validation_dates: slice,
        holdout_dates: slice,
        tradeable_labels: tuple[str, ...],
        fast_score_weight: float,
        pair_score_weight: float,
        permutation_score_weight: float,
        on_score: ScoreProgressCallback,
    ) -> tuple[list[dict[str, object]], dict[str, object], list[IndicatorScore]]:
        """Search a bounded pair pool; validation ranks, holdout only evaluates."""

        catalog = default_indicator_catalog()
        value_index = {value_id: index for index, value_id in enumerate(value_ids)}
        label_index: dict[str, int] = {
            label: index for index, label in enumerate(config.labels)
        }
        candidates: list[dict[str, object]] = []
        holdout_scores: list[IndicatorScore] = []
        counts: dict[str, object] = {}

        for label in tradeable_labels:
            components = [
                (value_id, metric)
                for value_id in value_ids
                if (metric := train_metrics.get((value_id, label))) is not None
                and _metric_number(metric, "ic_days") >= MIN_IC_DAYS
                and _has_finite_metric(metric, "ic_mean")
            ]
            components.sort(
                key=lambda item: (
                    -_metric_number(item[1], "ic_mean"),
                    -abs(_metric_number(item[1], "icir")),
                )
            )
            selected = components[:PAIR_COMPONENT_LIMIT]
            selected_ids = [value_id for value_id, _ in selected]
            label_counts: dict[str, object] = {
                "selected_components": selected_ids,
                "generated": 0,
                "validation_eligible": 0,
                "holdout_evaluated": 0,
            }
            counts[label] = label_counts
            if len(selected_ids) < 2:
                continue

            # Rank-normalise just the bounded training-selected pool. Keeping
            # these float32 panels avoids materialising every possible pair.
            ranks: dict[str, np.ndarray] = {}
            for value_id in selected_ids:
                ranks[value_id] = percentile_rank_panel(
                    np.asarray(panel[value_index[value_id]], dtype=float)
                )
                if on_score is not None:
                    on_score(fast_score_weight, "正在归一化训练组件")
            label_values = np.asarray(labels[label_index[label]], dtype=float)
            label_candidates: list[dict[str, object]] = []
            generated_count = 0
            eligible_count = 0

            def add_candidate(left_id: str, right_id: str, operation: str) -> None:
                nonlocal generated_count, eligible_count
                feature = combine_rank_panels(ranks[left_id], ranks[right_id], operation)
                train_metric = fast_rank_ic(
                    feature[:, train_dates],
                    label_values[:, train_dates],
                    min_cross_section=config.min_cross_section,
                )
                validation_metric = fast_rank_ic(
                    feature[:, validation_dates],
                    label_values[:, validation_dates],
                    min_cross_section=config.min_cross_section,
                )
                if (
                    operation == "rank_ratio"
                    and _metric_number(validation_metric, "signed_ic_mean") < 0
                ):
                    left_id, right_id = right_id, left_id
                    train_metric = _reverse_fast_metric_direction(train_metric)
                    validation_metric = _reverse_fast_metric_direction(validation_metric)
                candidate_id = f"pair:{operation}:{left_id}:{right_id}"
                left_name = display_name_for(left_id, catalog)
                right_name = display_name_for(right_id, catalog)
                expression = _pair_expression(left_name, right_name, operation)
                if _metric_number(validation_metric, "ic_days") >= MIN_IC_DAYS:
                    eligible_count += 1
                    label_candidates.append(
                        {
                            "candidate_id": candidate_id,
                            "left_value_id": left_id,
                            "left_display_name": left_name,
                            "right_value_id": right_id,
                            "right_display_name": right_name,
                            "operation": operation,
                            "operation_display_name": PAIR_OPERATION_NAMES[operation],
                            "expression": expression,
                            "label": label,
                            "train": train_metric,
                            "validation": validation_metric,
                            "training_validation_direction_stable": _directions_match(
                                train_metric, validation_metric
                            ),
                            "holdout": None,
                            "direction_stable": None,
                        }
                    )
                generated_count += 1
                if on_score is not None:
                    on_score(pair_score_weight, "正在验证段筛选双指标组合")

            for left_position, left_id in enumerate(selected_ids):
                for right_id in selected_ids[left_position + 1 :]:
                    for operation in ("rank_gap", "rank_ratio", "cross_up", "cross_down"):
                        add_candidate(left_id, right_id, operation)

            label_candidates.sort(
                key=lambda item: (
                    not bool(item["training_validation_direction_stable"]),
                    *_fast_metric_rank_key(item["validation"]),
                )
            )
            label_counts["generated"] = generated_count
            label_counts["validation_eligible"] = eligible_count
            finalists = label_candidates[:HOLDOUT_FINALIST_LIMIT_PER_LABEL]
            for rank, candidate in enumerate(finalists, start=1):
                candidate["validation_rank"] = rank
                left_id = str(candidate["left_value_id"])
                right_id = str(candidate["right_value_id"])
                operation = str(candidate["operation"])
                feature = combine_rank_panels(ranks[left_id], ranks[right_id], operation)
                score = score_indicator(
                    np.asarray(feature[:, holdout_dates], dtype=float),
                    np.asarray(label_values[:, holdout_dates], dtype=float),
                    value_id=str(candidate["candidate_id"]),
                    display_name=str(candidate["expression"]),
                    label=label,
                    min_cross_section=config.min_cross_section,
                    block_size=config.block_size,
                    permutations=config.permutations,
                    seed=DEFAULT_SEED,
                    alpha=config.alpha,
                )
                holdout_scores.append(score)
                label_counts["holdout_evaluated"] = len(finalists)
                if on_score is not None:
                    on_score(permutation_score_weight, "正在留出段复核双指标候选")
                candidates.append(candidate)

        return candidates, counts, holdout_scores

    def _score_panel(
        self,
        panel: np.memmap,
        labels: np.ndarray,
        config: SwingIndicatorConfig,
        value_ids: tuple[str, ...],
        dates: slice,
        progress_callback: ScoreProgressCallback = None,
    ) -> list[IndicatorScore]:
        catalog = default_indicator_catalog()
        scores: list[IndicatorScore] = []
        done = 0
        for value_index, value_id in enumerate(value_ids):
            matrix = np.asarray(panel[value_index][:, dates], dtype=float)
            for label_index, label in enumerate(config.labels):
                scores.append(
                    score_indicator(
                        matrix,
                        np.asarray(labels[label_index][:, dates], dtype=float),
                        value_id=value_id,
                        display_name=display_name_for(value_id, catalog),
                        label=label,
                        min_cross_section=config.min_cross_section,
                        block_size=config.block_size,
                        permutations=config.permutations,
                        seed=DEFAULT_SEED,
                        alpha=config.alpha,
                    )
                )
                done += 1
                if progress_callback is not None:
                    progress_callback(float(done), "正在验证段逐日打分")
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


def _metric_number(
    metric: Mapping[str, object], key: str, *, default: float = 0.0
) -> float:
    value = metric.get(key)
    if isinstance(value, (int, float)) and np.isfinite(value):
        return float(value)
    return default


def _has_finite_metric(metric: Mapping[str, object], key: str) -> bool:
    value = metric.get(key)
    return isinstance(value, (int, float)) and bool(np.isfinite(value))


def _rank_validation_scores(scores: Iterable[IndicatorScore]) -> list[IndicatorScore]:
    """Order singles using only the validation split."""

    return sorted(
        scores,
        key=lambda item: (
            not item.significant,
            -item.ic_mean if np.isfinite(item.ic_mean) else float("inf"),
            -abs(item.icir) if item.icir is not None else 0.0,
        ),
    )


def _rank_single_comparison_candidates(
    scores: Iterable[IndicatorScore],
    train_metrics: Mapping[tuple[str, str], dict[str, object]],
) -> list[IndicatorScore]:
    """Choose holdout finalists with the same rule used by the mixed board."""

    return sorted(
        scores,
        key=lambda score: _comparison_rank_key(
            train_metrics.get((score.value_id, score.label)), score
        ),
    )


def _comparison_rank_key(train: object, validation: object) -> tuple[bool, float, float]:
    validation_key = _fast_metric_rank_key(validation)
    return (not _directions_match(train, validation), *validation_key)


def _build_comparison_board(
    tradeable_labels: tuple[str, ...],
    single_finalists_by_label: Mapping[str, list[IndicatorScore]],
    combination_scores: list[dict[str, object]],
    train_metrics: Mapping[tuple[str, str], dict[str, object]],
    holdout_by_key: Mapping[tuple[str, str], IndicatorScore],
) -> list[dict[str, object]]:
    """Build same-label single/pair rankings using validation only."""

    board: list[dict[str, object]] = []
    for label in tradeable_labels:
        candidates: list[dict[str, object]] = []
        for score in single_finalists_by_label.get(label, []):
            train = train_metrics.get((score.value_id, label))
            holdout = holdout_by_key.get((score.value_id, label))
            candidates.append(
                {
                    "candidate_id": f"single:{score.value_id}",
                    "candidate_type": "single",
                    "display_name": score.display_name,
                    "expression": score.display_name,
                    "value_ids": [score.value_id],
                    "label": label,
                    "train": train,
                    "validation": _score_metrics(score),
                    "validation_q": score.qvalue,
                    "holdout": _score_metrics(holdout),
                    "holdout_q": holdout.qvalue if holdout is not None else None,
                    "training_validation_direction_stable": _directions_match(train, score),
                    "direction_stable": _direction_stable(train, score, holdout),
                }
            )

        for combination in combination_scores:
            if combination.get("label") != label:
                continue
            candidate_id = str(combination["candidate_id"])
            holdout = holdout_by_key.get((candidate_id, label))
            candidates.append(
                {
                    "candidate_id": candidate_id,
                    "candidate_type": "pair",
                    "display_name": str(combination["expression"]),
                    "expression": str(combination["expression"]),
                    "value_ids": [
                        str(combination["left_value_id"]),
                        str(combination["right_value_id"]),
                    ],
                    "label": label,
                    "train": combination["train"],
                    "validation": combination["validation"],
                    "validation_q": None,
                    "holdout": _score_metrics(holdout),
                    "holdout_q": holdout.qvalue if holdout is not None else None,
                    "training_validation_direction_stable": bool(
                        combination["training_validation_direction_stable"]
                    ),
                    "direction_stable": _direction_stable(
                        combination["train"], combination["validation"], holdout
                    ),
                }
            )

        candidates.sort(
            key=lambda item: (
                not bool(item["training_validation_direction_stable"]),
                *_fast_metric_rank_key(item["validation"]),
            )
        )
        for rank, candidate in enumerate(candidates, start=1):
            candidate["comparison_rank"] = rank
            board.append(candidate)
    return board


def _reverse_fast_metric_direction(metric: Mapping[str, object]) -> dict[str, object]:
    """Flip the sign of a rank metric for the reciprocal ratio orientation."""

    reversed_metric = dict(metric)
    signed = metric.get("signed_ic_mean")
    if isinstance(signed, (int, float)) and np.isfinite(signed):
        reversed_metric["signed_ic_mean"] = -float(signed)
    order = metric.get("order")
    if order == "high":
        reversed_metric["order"] = "low"
    elif order == "low":
        reversed_metric["order"] = "high"
    return reversed_metric


def _fast_metric_rank_key(metric: object) -> tuple[float, float]:
    if isinstance(metric, IndicatorScore):
        ic = metric.ic_mean if np.isfinite(metric.ic_mean) else float("nan")
        icir = metric.icir if metric.icir is not None else 0.0
        return (-ic if np.isfinite(ic) else float("inf"), -abs(icir))
    if not isinstance(metric, Mapping):
        return (float("inf"), float("inf"))
    return (
        -_metric_number(metric, "ic_mean", default=float("inf")),
        -abs(_metric_number(metric, "icir")),
    )


def _signed_direction(metric: object) -> int | None:
    if isinstance(metric, IndicatorScore):
        if not np.isfinite(metric.ic_mean) or metric.ic_mean < _STABLE_DIRECTION_MIN_IC:
            return None
        return 1 if metric.order == "high" else -1
    if not isinstance(metric, Mapping):
        return None
    signed_ic = _metric_number(metric, "signed_ic_mean", default=float("nan"))
    if not np.isfinite(signed_ic) or abs(signed_ic) < _STABLE_DIRECTION_MIN_IC:
        return None
    return 1 if signed_ic > 0 else -1


def _directions_match(first: object, second: object) -> bool:
    first_direction = _signed_direction(first)
    second_direction = _signed_direction(second)
    return first_direction is not None and first_direction == second_direction


def _score_metrics(score: IndicatorScore | None) -> dict[str, object] | None:
    if score is None:
        return None
    sign = 1.0 if score.order == "high" else -1.0
    return {
        "ic_mean": score.ic_mean if np.isfinite(score.ic_mean) else None,
        "signed_ic_mean": sign * score.ic_mean if np.isfinite(score.ic_mean) else None,
        "icir": score.icir,
        "ic_days": score.ic_days,
        "order": score.order,
        "pvalue": score.pvalue,
        "qvalue": score.qvalue,
        "significant": score.significant,
        "ic_threshold": score.ic_threshold,
        "top_lift": score.top_lift,
        "threshold_hint": score.threshold_hint,
        "note": score.note,
    }


def _direction_stable(
    train: object,
    validation: object,
    holdout: IndicatorScore | None,
) -> bool | None:
    train_direction = _signed_direction(train)
    validation_direction = _signed_direction(validation)
    holdout_direction = _signed_direction(holdout)
    if None in (train_direction, validation_direction, holdout_direction):
        return None
    directions = [train_direction, validation_direction, holdout_direction]
    return len(set(directions)) == 1


def _pair_expression(left: str, right: str, operation: str) -> str:
    if operation == "rank_gap":
        return f"{left} 分位 − {right} 分位"
    if operation == "rank_ratio":
        return f"({left} 分位 + 0.02) ÷ ({right} 分位 + 0.02)"
    crossing = "上穿" if operation == "cross_up" else "下穿"
    return f"{left} 分位 {crossing} {right} 分位"


def _write_stock(
    group: pd.DataFrame,
    config: SwingIndicatorConfig,
    groups: dict[str, list[tuple[int, str]]],
    column_count: int,
    calendar_index: pd.DatetimeIndex,
    start_timestamp: pd.Timestamp | None,
    end_timestamp: pd.Timestamp | None,
    panel: np.memmap,
    labels: np.ndarray,
    row: int,
) -> bool:
    """Write one stock's indicator rows and swing labels into the panel.

    Each formula is evaluated once per stock and its requested outputs are
    scattered into their own panel columns. A formula that cannot produce
    values for this stock (too few bars, or a denominator it refuses to
    divide by) leaves those columns NaN rather than aborting the row: the
    study scores each column independently, and one silent formula should not
    cost the stock its swing labels.
    """

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

    values = np.full((len(group), column_count), np.nan, dtype=float)
    for formula_id, items in groups.items():
        result = FORMULA_REGISTRY.formula(formula_id).calculate(group)
        for index, name in items:
            array = np.asarray(result.values.get(name), dtype=float)
            if array.shape == (len(group),):
                values[:, index] = array
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
        "split_metadata": None,
        "scores": [],
        "comparison_candidates": [],
        "comparison_summary": None,
        "combination_scores": [],
        "combination_summary": None,
        "significant_count": 0,
        "reason": None,
    }


def _years_between(first: pd.Timestamp | None, last: pd.Timestamp | None) -> float | None:
    if first is None or last is None or pd.isna(first) or pd.isna(last):
        return None
    days = (last - first).days
    return days / 365.25 if days > 0 else None
