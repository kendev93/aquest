"""Batch materialization of built-in indicator results for the local store."""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol, cast

import numpy as np
import pandas as pd

from selector_app.concurrency import MAX_WORKERS
from selector_app.formulas.common import validate_market_data
from selector_app.formulas.registry import FORMULA_REGISTRY, FormulaRegistry
from selector_app.formulas.types import FormulaResult
from selector_app.indicators.cache import (
    INDICATOR_CACHE_WARMUP_BARS,
    Chunk,
    IndicatorCache,
    chunk_data_fingerprint,
    chunk_source_hash,
    frame_fingerprint,
    merge_results,
)
from selector_app.market_data.adjustment import CorporateAction, mark_adjusted
from selector_app.market_data.models import InstrumentBoard, InstrumentType, StockRef
from selector_app.market_data.store_base import DEFAULT_PRICE_BASIS

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int], None]
_PRECOMPUTE_CHUNK_SIZE = 32
# 预计算按自然年分块：一次只把"一年 + 预热"的行情读进内存，算完留下当年那一段，
# 各年段首尾相接后再落库。一次读全市场全历史会同时带来十几 GB 的峰值内存和
# 十几 GB 的 blob 写入，实测会把容器打崩。
# 8 年会把缓存顶到 ~19GB，而容器卷的写入只有本机磁盘的十分之一，一轮要跑近
# 一小时且期间会拖慢正常查询。5 年覆盖回测/条件研究，也让波段研究的默认区间
# 落在缓存内；需要更长区间时通过 `precompute_years` 单独指定。
DEFAULT_PRECOMPUTE_YEARS = 5
#: 每年段额外向前读取的根数。长周期指标（如 RSRS 的 250 日回归）没有这段预热，
#  年初那几十根的值是不可信的——它会被当成"该年一开始就是这样"参与后续计算。
DEFAULT_PRECOMPUTE_WARMUP_BARS = INDICATOR_CACHE_WARMUP_BARS
# 分批加载之后总根数不再是内存风险（每批只留 32 只一年的行情在内存里），
# 这个上限只是避免小机器上的同步任务被一轮全量预热拖太久。
PRECOMPUTE_MAX_BARS = 20_000_000


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
        self._action_cache: dict[tuple[str, str], tuple[CorporateAction, ...]] = {}

    def run(
        self,
        *,
        vipdoc_path: str | Path,
        universe: str = "all",
        universe_file: str | Path | None = None,
        instrument_types: tuple[InstrumentType, ...] | None = None,
        boards: tuple[InstrumentBoard, ...] | None = None,
        years: int = DEFAULT_PRECOMPUTE_YEARS,
        warmup_bars: int = DEFAULT_PRECOMPUTE_WARMUP_BARS,
        only_changed: bool = False,
        end_year: int | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> IndicatorPrecomputeReport:
        if isinstance(years, bool) or not isinstance(years, int) or years < 1:
            raise ValueError("预计算年份范围必须是正整数")
        if isinstance(warmup_bars, bool) or not isinstance(warmup_bars, int) or warmup_bars < 0:
            raise ValueError("预计算预热根数必须是非负整数")
        if end_year is not None and (
            isinstance(end_year, bool) or not isinstance(end_year, int) or end_year < 1
        ):
            raise ValueError("预计算年份锚点必须是正整数")
        refs = self._adapter.list_stock_refs(
            vipdoc_path,
            universe,
            universe_file,
            instrument_types=instrument_types,
            boards=boards,
        )
        formula_ids = self._registry.all_formula_ids()
        self._cache.refresh_revisions((ref.market, ref.code) for ref in refs)
        if only_changed:
            refs = self._changed_refs(refs, formula_ids)
        minimum_bars = {
            formula_id: self._registry.formula(formula_id).minimum_bars
            for formula_id in formula_ids
        }
        windows = _year_windows(years, end_year)
        failures: Counter[str] = Counter()
        processed = 0
        skipped = 0
        computed = 0
        cache_hits = 0
        progress_count = 0

        def report_progress() -> None:
            nonlocal progress_count
            progress_count += 1
            if progress_callback is not None:
                progress_callback(progress_count, len(refs))

        for offset in range(0, len(refs), _PRECOMPUTE_CHUNK_SIZE):
            batch = refs[offset : offset + _PRECOMPUTE_CHUNK_SIZE]
            batch_keys = {(str(ref.market).strip().upper(), str(ref.code).strip()) for ref in batch}
            cached_index = self._cache.cached_chunk_index(batch_keys, formula_ids)
            offsets_by_key: dict[tuple[str, str], int] = dict.fromkeys(batch_keys, 0)
            failed_keys: set[tuple[str, str]] = set()
            seen_keys: set[tuple[str, str]] = set()
            retags: list[dict[str, object]] = []
            for year_start, year_end in windows:
                year_frames = self._batch_frames(
                    batch,
                    start_date=year_start,
                    end_date=year_end,
                    warmup_bars=warmup_bars,
                )
                if not year_frames:
                    continue
                work: dict[
                    tuple[str, str],
                    tuple[pd.DataFrame, str, str, int, tuple[str, ...]],
                ] = {}
                for raw_key, frame in year_frames.items():
                    key = (str(raw_key[0]).strip().upper(), str(raw_key[1]).strip())
                    if key not in batch_keys:
                        continue
                    prepared = _prepare_frame(frame)
                    if prepared.empty:
                        continue
                    dates = pd.to_datetime(prepared["date"], errors="coerce")
                    in_year = dates >= pd.Timestamp(year_start)
                    year_bars = int(in_year.sum())
                    if year_bars <= 0:
                        continue
                    output_frame = prepared.loc[in_year].reset_index(drop=True)
                    chunk_start = pd.Timestamp(output_frame["date"].iloc[0]).date()
                    chunk_end = pd.Timestamp(output_frame["date"].iloc[-1]).date()
                    bar_offset = offsets_by_key.get(key, 0)
                    offsets_by_key[key] = bar_offset + year_bars
                    seen_keys.add(key)

                    source_hash = frame_fingerprint(prepared)
                    revision = self._cache.revision_for(key[0], key[1], prepared)
                    current_fingerprint = chunk_data_fingerprint(
                        revision, prepared, source_hash=source_hash
                    )
                    stale_formulas: list[str] = []
                    for formula_id in formula_ids:
                        definition = self._registry.formula(formula_id)
                        candidates = cached_index.get((key[0], key[1], formula_id), ())
                        matched = next(
                            (
                                row
                                for row in candidates
                                if str(row.get("formula_version")) == definition.cache_version
                                and _cache_date(row.get("data_start")) == chunk_start
                                and _cache_date(row.get("data_end")) == chunk_end
                                and _cache_int(row.get("data_length")) == year_bars
                                and chunk_source_hash(row.get("data_fingerprint")) == source_hash
                            ),
                            None,
                        )
                        if matched is None:
                            stale_formulas.append(formula_id)
                            continue
                        cache_hits += 1
                        old_offset = _cache_int(matched.get("bar_offset"))
                        if (
                            str(matched.get("data_fingerprint")) != current_fingerprint
                            or old_offset != bar_offset
                        ):
                            retags.append(
                                {
                                    "market": key[0],
                                    "code": key[1],
                                    "timeframe": "daily",
                                    "price_basis": _frame_price_basis(prepared),
                                    "formula_id": formula_id,
                                    "formula_version": definition.cache_version,
                                    "old_data_fingerprint": str(matched["data_fingerprint"]),
                                    "old_bar_offset": old_offset,
                                    "data_fingerprint": current_fingerprint,
                                    "bar_offset": bar_offset,
                                    "data_start": matched.get("data_start"),
                                }
                            )
                    if stale_formulas:
                        work[key] = (
                            prepared,
                            source_hash,
                            current_fingerprint,
                            bar_offset,
                            tuple(stale_formulas),
                        )

                if not work:
                    continue
                write_frames: dict[tuple[str, str], pd.DataFrame] = {}
                write_results: dict[tuple[str, str], dict[str, FormulaResult]] = {}
                chunks_by_key: dict[tuple[str, str], tuple[Chunk, ...]] = {}
                fingerprints_by_key: dict[tuple[str, str], str] = {}
                with ThreadPoolExecutor(
                    max_workers=min(MAX_WORKERS, len(work)),
                    thread_name_prefix="indicator-precompute",
                ) as executor:
                    futures = {
                        executor.submit(
                            self._compute_year,
                            frame,
                            year_start,
                            stale_formulas,
                            minimum_bars,
                        ): (key, source_hash, current_fingerprint, bar_offset)
                        for key, (
                            frame,
                            source_hash,
                            current_fingerprint,
                            bar_offset,
                            stale_formulas,
                        ) in work.items()
                    }
                    for future in as_completed(futures):
                        key, _source_hash, current_fingerprint, bar_offset = futures[future]
                        try:
                            outcome = future.result()
                        except Exception as exc:  # noqa: BLE001 - isolate one instrument
                            reason = str(exc).strip()[:200] or type(exc).__name__
                            failures[reason] += 1
                            failed_keys.add(key)
                            logger.warning("指标预计算 %s 失败，已跳过", key[1], exc_info=True)
                            continue
                        if outcome is None:
                            continue
                        year_frame, year_results = outcome
                        if not year_results:
                            continue
                        write_frames[key] = year_frame
                        write_results[key] = year_results
                        start, end = _frame_bounds(year_frame)
                        chunks_by_key[key] = (
                            Chunk(
                                offset=0,
                                length=len(year_frame),
                                start=start,
                                end=end,
                                bar_offset=bar_offset,
                            ),
                        )
                        fingerprints_by_key[key] = current_fingerprint
                        computed += len(year_results)
                if write_results:
                    try:
                        self._cache.persist_results(
                            write_frames,
                            write_results,
                            chunks_by_key=chunks_by_key,
                            data_fingerprint_overrides=fingerprints_by_key,
                            preserve_other_chunks=True,
                        )
                    except (OSError, RuntimeError, ValueError) as exc:
                        reason = str(exc).strip()[:200] or type(exc).__name__
                        failures[reason] += 1
                        logger.warning("指标缓存写入失败，本批跳过", exc_info=True)
                        failed_keys.update(write_results)
            self._cache.retag_cached_chunks(retags)
            processed += len(seen_keys - failed_keys)
            skipped += len(batch_keys - seen_keys) + len(failed_keys & seen_keys)
            for _ in batch:
                report_progress()

        return IndicatorPrecomputeReport(
            total_candidates=len(refs),
            processed=processed,
            skipped=skipped,
            errors=sum(failures.values()),
            computed=computed,
            cache_hits=cache_hits,
            failure_reasons=failures,
        )

    def _changed_refs(
        self,
        refs: list[StockRef],
        formula_ids: tuple[str, ...],
    ) -> list[StockRef]:
        """Keep only instruments whose cache is missing or out of date.

        行情更新往往只动一小部分标的，全市场重算等于把没变的也重写一遍。无从
        判断（返回 None）时按全量处理，宁可多算也不能漏算。
        """

        stale = self._cache.stale_symbols(formula_ids)
        if stale is None:
            return refs
        return [
            ref for ref in refs if (str(ref.market).strip().upper(), str(ref.code).strip()) in stale
        ]

    def _read_one(self, ref: StockRef) -> pd.DataFrame:
        return _prepare_frame(
            self._adapter.read_stock(ref),
            actions=self._corporate_actions(ref),
        )

    def _corporate_actions(self, ref: StockRef) -> tuple[CorporateAction, ...]:
        """取该标的的分红送转事件，按实例缓存一轮。

        预算是逐标的走的，不缓存的话全市场就是 5000 次查询；事件表极小（每股平均
        三十个事件），缓存住整轮 run 的量完全可以接受。适配器没有这个能力时
        （测试用的 FakeAdapter）退化成"无事件"，指标退化为不复权口径。
        """

        reader = getattr(self._adapter, "read_corporate_actions", None)
        if not callable(reader):
            return ()
        key = (ref.market, ref.code)
        if key not in self._action_cache:
            self._action_cache[key] = tuple(reader(ref))
        return self._action_cache[key]

    def _compute_year(
        self,
        frame: pd.DataFrame,
        year_start: date,
        formula_ids: tuple[str, ...],
        minimum_bars: Mapping[str, int],
    ) -> tuple[pd.DataFrame, dict[str, FormulaResult]] | None:
        """Compute one calendar year, keeping only that year's own bars.

        The frame carries `warmup_bars` extra bars before the year so rolling
        indicators are already warmed up on 1 January; those bars are dropped
        again so consecutive years concatenate without double counting.
        """

        prepared = _prepare_frame(frame)
        if prepared.empty:
            return None
        dates = pd.to_datetime(prepared["date"], errors="coerce")
        in_year = dates >= pd.Timestamp(year_start)
        year_bars = int(in_year.sum())
        if year_bars <= 0:
            return None
        eligible = tuple(
            formula_id for formula_id in formula_ids if len(prepared) >= minimum_bars[formula_id]
        )
        if not eligible:
            return None
        results: dict[str, FormulaResult] = {}
        for formula_id in eligible:
            definition = self._registry.formula(formula_id)
            computed = definition.calculate(prepared.copy(deep=True))
            results[formula_id] = _tail(computed, year_bars)
        return prepared.loc[in_year].reset_index(drop=True), results

    def _batch_frames(
        self,
        refs: list[StockRef],
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        warmup_bars: int = 0,
    ) -> Mapping[tuple[str, str], pd.DataFrame]:
        reader = getattr(self._adapter, "read_many_stocks", None)
        if reader is None:
            # 适配器不支持批量读取时退回逐个读取，但仍并行——串行 I/O 会把整批
            # 拖到单线程速度。年份过滤随后在 `_compute_year` 里做。
            frames: dict[tuple[str, str], pd.DataFrame] = {}
            with ThreadPoolExecutor(
                max_workers=min(MAX_WORKERS, len(refs)),
                thread_name_prefix="indicator-precompute-read",
            ) as executor:
                futures = {executor.submit(self._read_one, ref): ref for ref in refs}
                for future in as_completed(futures):
                    ref = futures[future]
                    try:
                        frame = future.result()
                    except (OSError, ValueError):
                        logger.warning("读取 %s 行情失败，已跳过", ref.code, exc_info=True)
                        continue
                    if not frame.empty:
                        selected = _restrict_frame_to_window(
                            frame,
                            start_date=start_date,
                            end_date=end_date,
                            warmup_bars=warmup_bars,
                        )
                        if not selected.empty:
                            frames[(ref.market, ref.code)] = selected
            return frames
        try:
            combined = reader(
                refs, start_date=start_date, end_date=end_date, warmup_bars=warmup_bars
            )
        except TypeError:
            combined = reader(refs)
        yearly: dict[tuple[str, str], pd.DataFrame] = {}
        if combined.empty:
            return yearly
        for key, group in combined.groupby(["market", "code"], sort=False):
            selected = _restrict_frame_to_window(
                group,
                start_date=start_date,
                end_date=end_date,
                warmup_bars=warmup_bars,
            )
            if not selected.empty:
                yearly[(str(key[0]), str(key[1]))] = selected
        return yearly


def _restrict_frame_to_window(
    frame: pd.DataFrame,
    *,
    start_date: date | None,
    end_date: date | None,
    warmup_bars: int,
) -> pd.DataFrame:
    """Keep the requested output interval plus a stable causal warm-up tail."""

    prepared = _prepare_frame(frame)
    if prepared.empty or start_date is None or end_date is None:
        return prepared.reset_index(drop=True)
    dates = pd.to_datetime(prepared["date"], errors="coerce")
    within = (dates >= pd.Timestamp(start_date)) & (dates <= pd.Timestamp(end_date))
    positions = np.flatnonzero(within.to_numpy(dtype=bool, copy=False))
    if positions.size == 0:
        return prepared.iloc[0:0].reset_index(drop=True)
    first = max(0, int(positions[0]) - warmup_bars)
    last = int(positions[-1])
    return prepared.iloc[first : last + 1].reset_index(drop=True)


def _cache_date(value: object) -> date | None:
    if isinstance(value, pd.Timestamp):
        return cast(date, value.date())
    if isinstance(value, date):
        return value
    parsed = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(parsed) else parsed.date()


def _cache_int(value: object) -> int:
    if isinstance(value, (int, np.integer)):
        return int(value)
    if not isinstance(value, str):
        return -1
    try:
        return int(value.strip())
    except (TypeError, ValueError, OverflowError):
        return -1


def _frame_price_basis(frame: pd.DataFrame) -> str:
    if "price_basis" not in frame.columns:
        return DEFAULT_PRICE_BASIS
    values = tuple(frame["price_basis"].dropna().astype(str).unique())
    if len(values) > 1:
        raise ValueError("同一行情序列不能混用多个价格口径")
    return values[0] if values else DEFAULT_PRICE_BASIS


def _year_windows(years: int, end_year: int | None = None) -> list[tuple[date, date]]:
    """Return the `[Jan 1, Dec 31]` windows of the last `years` calendar years.

    `end_year` 把窗口锚在行情上而不是墙上时钟：数据停在 2024 年而今天是 2026
    年时，默认锚点会给出 2022..2026——两个空年份白算，真正存着数据的 2020、
    2021 反而被切掉，而"最近五年"这个说法说的正是数据的最近五年。
    """

    last = end_year if end_year is not None else date.today().year
    if isinstance(last, bool) or not isinstance(last, int) or last < 1:
        raise ValueError("预计算年份锚点必须是正整数")
    return [(date(year, 1, 1), date(year, 12, 31)) for year in range(last - years + 1, last + 1)]


def _tail(result: FormulaResult, length: int) -> FormulaResult:
    """Return the trailing `length` bars of a computed result."""

    if length <= 0 or length >= result.data_length:
        return result
    values = {name: np.asarray(value)[length * -1 :] for name, value in result.values.items()}
    signals = {name: np.asarray(value)[length * -1 :] for name, value in result.signals.items()}
    return FormulaResult.build(
        formula_id=result.formula_id,
        display_name=result.display_name,
        minimum_bars=result.minimum_bars,
        recommended_bars=result.recommended_bars,
        data_length=length,
        values=values,
        signals=signals,
    )


def _frame_bounds(frame: pd.DataFrame) -> tuple[date | None, date | None]:
    """First and last bar date of one yearly segment."""

    if frame.empty:
        return None, None
    dates = pd.to_datetime(frame["date"], errors="coerce")
    head = dates.iloc[0]
    tail = dates.iloc[-1]
    return (
        head.date() if hasattr(head, "date") else None,
        tail.date() if hasattr(tail, "date") else None,
    )


def _merge_segments(segments: list[FormulaResult]) -> FormulaResult:
    """Concatenate consecutive yearly segments back into one series."""

    return merge_results(segments)


def _prepare_frame(
    frame: pd.DataFrame,
    *,
    actions: Sequence[CorporateAction] = (),
) -> pd.DataFrame:
    prepared = frame.copy(deep=True)
    if prepared.empty:
        return prepared
    validate_market_data(prepared)
    prepared["date"] = pd.to_datetime(prepared["date"], errors="coerce")
    if prepared["date"].isna().any():
        raise ValueError("行情数据包含无效日期")
    prepared = prepared.sort_values("date", kind="stable").reset_index(drop=True)
    if actions:
        # 指标必须算在除权校正后的价格上。raw 序列在除权日留着真实跳空（那是市场
        # 事实），但拿它算 ATR、动量、均线都是假的 —— 浦发银行 2017-05-25 一天
        # 跳空 24%，指标会把它当成暴跌。成交量与成交额是事实量，不参与复权。
        prepared = mark_adjusted(prepared, actions)
    return prepared
