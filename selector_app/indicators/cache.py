"""Versioned, persistent cache for deterministic built-in formula results."""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import time
from collections import OrderedDict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from threading import RLock
from typing import cast

import numpy as np
import pandas as pd

from selector_app.formulas.custom import (
    ParsedFormula,
    evaluate_custom_formula,
    required_bars_for_parameters,
    resolve_formula_parameters,
)
from selector_app.formulas.registry import FORMULA_REGISTRY, FormulaDefinition, FormulaRegistry
from selector_app.formulas.types import FormulaResult
from selector_app.market_data.store import DuckDbMarketDataStore

_TIMEFRAME = "daily"
_DEFAULT_PRICE_BASIS = "raw"
_FINGERPRINT_COLUMNS = ("date", "open", "high", "low", "close", "volume", "amount")
_OPTIONAL_FINGERPRINT_COLUMNS = ("bar_status", "price_basis")
_MAX_CACHE_BLOB_BYTES = 64 * 1024 * 1024
_CUSTOM_ENGINE_VERSION = "1"
# 落库用 float32：价格只有两位小数、指标值不需要 float64 的 17 位有效数字，
# 实测缓存体积直接减半。读回来仍转成 float64，下游无感。
_VALUE_DTYPE = np.float32


@dataclass(frozen=True)
class Chunk:
    """缓存里的一个自然年分块。

    `offset` 是这一块在传入结果里的切片起点，`bar_offset` 是它写进库里的位置。
    两者多数时候相同；预计算是逐年算完就落库的，它手上的结果只有一年，切片
    起点恒为 0，而库里的位置要从前面几年累加出来——所以要分开记。
    """

    offset: int
    length: int
    start: date | None
    end: date | None
    bar_offset: int | None = None

    @property
    def store_offset(self) -> int:
        return self.offset if self.bar_offset is None else self.bar_offset


def _chunk_plan(frame: pd.DataFrame) -> tuple[Chunk, ...]:
    """Split one bar series into consecutive calendar-year chunks.

    缓存按自然年分块存储：行情每天只会在尾部多一根 K 线，整条重写意味着每天
    要把 5 年序列全部再写一遍（实测全市场约 13.6 秒）。分块之后只有当年那一块
    需要重写。块用「在全历史中的起始下标 + 长度」描述，读取时靠它校验连续性。
    """

    if frame.empty or "date" not in frame.columns:
        return ()
    dates = pd.to_datetime(frame["date"], errors="coerce")
    if dates.isna().any():
        return ()
    years = dates.dt.year.to_numpy()
    chunks: list[Chunk] = []
    start = 0
    for index in range(1, len(years)):
        if years[index] != years[start]:
            chunks.append(_make_chunk(dates, start, index))
            start = index
    chunks.append(_make_chunk(dates, start, len(years)))
    return tuple(chunks)


def _make_chunk(dates: pd.Series, start: int, stop: int) -> Chunk:
    head = dates.iloc[start]
    tail = dates.iloc[stop - 1]
    return Chunk(
        offset=int(start),
        length=int(stop - start),
        start=head.date() if hasattr(head, "date") else None,
        end=tail.date() if hasattr(tail, "date") else None,
    )


@dataclass(frozen=True)
class _CacheKey:
    market: str
    code: str
    timeframe: str
    price_basis: str
    formula_id: str
    formula_version: str
    # 标的级数据版本号（不是传入 frame 的哈希）。同一个标的无论请求哪个
    # 时间窗口都得到同一个版本号，缓存里存的是全历史序列，读取时按长度
    # 从尾部切片。落库时仍写进 data_fingerprint 列（沿用旧列名，避免迁移）。
    data_revision: str


@dataclass(frozen=True)
class _CustomCacheKey:
    market: str
    code: str
    timeframe: str
    price_basis: str
    formula_hash: str
    parameter_hash: str
    engine_version: str
    data_revision: str


def frame_fingerprint(frame: pd.DataFrame) -> str:
    """Return a stable fingerprint for the ordered input series.

    Only data that can affect a formula result is included.  The row order is
    intentionally preserved because all supported formulas are causal arrays.
    """

    columns = [
        column
        for column in (*_FINGERPRINT_COLUMNS, *_OPTIONAL_FINGERPRINT_COLUMNS)
        if column in frame.columns
    ]
    missing = [column for column in _FINGERPRINT_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"行情数据缺少字段: {', '.join(missing)}")
    canonical = frame.loc[:, columns].copy(deep=True)
    canonical["date"] = pd.to_datetime(canonical["date"], errors="coerce")
    for column in _FINGERPRINT_COLUMNS[1:]:
        if column in canonical:
            canonical[column] = pd.to_numeric(canonical[column], errors="coerce")
    digest = hashlib.sha256()
    digest.update(str(len(canonical)).encode("ascii"))
    digest.update(b"\0")
    hashed = pd.util.hash_pandas_object(canonical, index=False)
    digest.update(hashed.to_numpy(dtype=np.uint64, copy=False).tobytes())
    return digest.hexdigest()


def _price_basis(frame: pd.DataFrame) -> str:
    if "price_basis" not in frame.columns:
        return _DEFAULT_PRICE_BASIS
    values = tuple(frame["price_basis"].dropna().astype(str).unique())
    if len(values) > 1:
        raise ValueError("同一行情序列不能混用多个价格口径")
    return values[0] if values else _DEFAULT_PRICE_BASIS


def _date_bounds(frame: pd.DataFrame) -> tuple[date | None, date | None]:
    if frame.empty:
        return None, None
    dates = pd.to_datetime(frame["date"], errors="coerce")
    if dates.isna().any():
        raise ValueError("行情数据包含无效日期")
    return dates.iloc[0].date(), dates.iloc[-1].date()


def _tail_slice(result: FormulaResult, length: int) -> FormulaResult | None:
    """Return the trailing `length` bars of a cached full-history result."""

    if length <= 0 or result.data_length < length:
        return None
    if result.data_length == length:
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


def _row_ends_at(row: Mapping[str, object], end: date | None) -> bool:
    """Whether a cached full-history row ends on the requested last date."""

    if end is None:
        return False
    row_end = row.get("data_end")
    if isinstance(row_end, date) and not isinstance(row_end, datetime):
        return row_end == end
    if isinstance(row_end, str) and row_end:
        try:
            return date.fromisoformat(row_end) == end
        except ValueError:
            return False
    return False


def _row_end(row: Mapping[str, object]) -> date | None:
    """Return the last date covered by a cached row, if it records one."""

    row_end = row.get("data_end")
    if isinstance(row_end, date) and not isinstance(row_end, datetime):
        return row_end
    if isinstance(row_end, str) and row_end:
        try:
            return date.fromisoformat(row_end)
        except ValueError:
            return None
    return None


def _revision_bar_count(revision: str) -> int | None:
    """Extract the stored bar count from a revision, if it has one."""

    head = revision.split("|", 1)[0]
    try:
        return int(head)
    except ValueError:
        return None


def _serialize_arrays(
    arrays: dict[str, np.ndarray],
    names: tuple[str, ...],
    *,
    dtype: type,
    data_length: int | None = None,
) -> bytes:
    if names:
        matrix: np.ndarray = np.stack(
            [np.asarray(arrays[name], dtype=dtype) for name in names], axis=0
        )
    else:
        matrix = np.empty((0, data_length or 0), dtype=dtype)
    buffer = io.BytesIO()
    np.save(buffer, matrix, allow_pickle=False)
    return buffer.getvalue()


def _deserialize_arrays(
    blob: object,
    names: tuple[str, ...],
    *,
    dtype: type,
    data_length: int,
) -> dict[str, np.ndarray]:
    if isinstance(blob, memoryview):
        raw = blob.tobytes()
    elif isinstance(blob, bytearray):
        raw = bytes(blob)
    elif isinstance(blob, bytes):
        raw = blob
    else:
        raise ValueError("指标缓存内容无效")
    if len(raw) > _MAX_CACHE_BLOB_BYTES:
        raise ValueError("指标缓存内容过大")
    try:
        matrix: np.ndarray = np.asarray(np.load(io.BytesIO(raw), allow_pickle=False), dtype=dtype)
    except (OSError, ValueError, EOFError) as exc:
        raise ValueError("指标缓存内容无效") from exc
    if matrix.ndim != 2 or matrix.shape != (len(names), data_length):
        raise ValueError("指标缓存内容无效")
    return {name: np.asarray(matrix[index]) for index, name in enumerate(names)}


def _chunk_offset(row: Mapping[str, object]) -> int:
    """Start index of one cached chunk inside the full-history series."""

    try:
        return int(cast(int, row.get("bar_offset")))
    except (TypeError, ValueError):
        return -1


def _chunk_length(row: Mapping[str, object]) -> int:
    """Number of bars covered by one cached chunk."""

    try:
        return int(cast(int, row.get("data_length")))
    except (TypeError, ValueError):
        return -1


def merge_results(pieces: Sequence[FormulaResult]) -> FormulaResult:
    """Concatenate consecutive chunks back into one continuous result."""

    if not pieces:
        raise ValueError("没有可合并的指标结果")
    if len(pieces) == 1:
        return pieces[0]
    first = pieces[0]
    values = {
        name: np.concatenate([np.asarray(piece.values[name]) for piece in pieces])
        for name in first.values
    }
    signals = {
        name: np.concatenate([np.asarray(piece.signals[name]) for piece in pieces])
        for name in first.signals
    }
    return FormulaResult.build(
        formula_id=first.formula_id,
        display_name=first.display_name,
        minimum_bars=first.minimum_bars,
        recommended_bars=first.recommended_bars,
        data_length=sum(int(piece.data_length) for piece in pieces),
        values=values,
        signals=signals,
    )


class IndicatorCache:
    """Cache built-in formula arrays in memory and, when available, DuckDB."""

    def __init__(
        self,
        store: DuckDbMarketDataStore | None = None,
        *,
        registry: FormulaRegistry | None = None,
        max_memory_entries: int = 128,
    ) -> None:
        if isinstance(max_memory_entries, bool) or max_memory_entries < 1:
            raise ValueError("指标缓存内存条目上限必须大于 0")
        self._store = store
        self._registry = registry or FORMULA_REGISTRY
        self._memory: OrderedDict[
            _CacheKey | _CustomCacheKey, tuple[FormulaResult, date | None]
        ] = OrderedDict()
        self._max_memory_entries = max_memory_entries
        self._revisions: dict[tuple[str, str], str] = {}
        self._lock = RLock()
        self._hits = 0
        self._misses = 0
        self._persistent_hits = 0
        self._computed = 0
        self._compute_time_ms = 0.0
        self._persistent_read_time_ms = 0.0
        self._persistent_write_time_ms = 0.0
        self._latency_samples: deque[float] = deque(maxlen=512)

    def clear_memory(self) -> None:
        """Drop in-process results after the underlying market data changes."""

        with self._lock:
            self._memory.clear()
            self._revisions.clear()

    def warm_revisions(self, refs: Iterable[tuple[str, str]]) -> None:
        """Pre-fetch per-instrument data revisions in one store round-trip."""

        if self._store is None:
            return
        pending = [
            (str(market).strip().upper(), str(code).strip())
            for market, code in refs
            if (str(market).strip().upper(), str(code).strip()) not in self._revisions
        ]
        if not pending:
            return
        try:
            fetched = self._store.data_revisions(pending)
        except (AttributeError, RuntimeError, OSError):
            return
        with self._lock:
            self._revisions.update(fetched)

    def _revision(self, market: str, code: str, frame: pd.DataFrame) -> str:
        """Resolve the revision identifying one instrument's bar series."""

        key = (market.strip().upper(), code.strip())
        with self._lock:
            cached = self._revisions.get(key)
        if cached is not None:
            return cached
        revision: str | None = None
        if self._store is not None and key[0] and key[1]:
            try:
                revision = self._store.data_revision(key[0], key[1])
            except (AttributeError, RuntimeError, OSError):
                revision = None
        if not revision:
            # 拿不到版本号时退化为帧指纹：缓存退回"同一窗口才命中"，结果仍然正确。
            return frame_fingerprint(frame)
        with self._lock:
            self._revisions[key] = revision
        return revision

    def get_or_compute_custom(
        self,
        frame: pd.DataFrame,
        parsed: ParsedFormula,
        parameter_values: Mapping[str, float] | None,
        *,
        market: str = "",
        code: str = "",
        timeframe: str = _TIMEFRAME,
    ) -> dict[str, FormulaResult]:
        started = time.perf_counter()
        try:
            return self._get_or_compute_custom(
                frame,
                parsed,
                parameter_values,
                market=market,
                code=code,
                timeframe=timeframe,
            )
        finally:
            self._record_latency(started)

    def _get_or_compute_custom(
        self,
        frame: pd.DataFrame,
        parsed: ParsedFormula,
        parameter_values: Mapping[str, float] | None,
        *,
        market: str = "",
        code: str = "",
        timeframe: str = _TIMEFRAME,
    ) -> dict[str, FormulaResult]:
        """Load or calculate a custom formula for one resolved parameter set."""

        parameters = resolve_formula_parameters(parsed, parameter_values)
        revision = self._revision(market, code, frame)
        normalized_market = market.strip().upper()
        normalized_code = code.strip()
        price_basis = _price_basis(frame)
        start, end = _date_bounds(frame)
        target_length = len(frame)
        parameter_json = json.dumps(
            parameters,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        key = _CustomCacheKey(
            market=normalized_market,
            code=normalized_code,
            timeframe=timeframe,
            price_basis=price_basis,
            formula_hash=_formula_hash(parsed),
            parameter_hash=hashlib.sha256(parameter_json.encode("utf-8")).hexdigest(),
            engine_version=_CUSTOM_ENGINE_VERSION,
            data_revision=revision,
        )
        cached = self._memory_get(key, end)
        sliced = _tail_slice(cached, target_length) if cached is not None else None
        if sliced is not None:
            return {"custom": sliced}

        row = self._persistent_custom_get(key)
        decoded = (
            self._decode_custom_persistent(row, parsed, parameters)
            if row is not None and _row_ends_at(row, end)
            else None
        )
        result = _tail_slice(decoded, target_length) if decoded is not None else None
        if result is not None and decoded is not None:
            self._memory_put(key, decoded, end)
            with self._lock:
                self._hits += 1
            return {"custom": result}

        with self._lock:
            self._misses += 1
        started = time.perf_counter()
        result = evaluate_custom_formula(parsed, frame.copy(deep=True), parameters)
        self._record_phase("_compute_time_ms", started)
        with self._lock:
            self._computed += 1
        bar_count = _revision_bar_count(revision)
        if bar_count is None or bar_count == target_length:
            self._memory_put(key, result, end)
            self._persistent_put_custom(key, parsed, parameter_json, result, start, end)
        return {"custom": result}

    def stale_symbols(
        self,
        formula_ids: Iterable[str],
    ) -> set[tuple[str, str]] | None:
        """Instruments whose cache is missing or outdated, or None if unknown.

        返回 None 表示「无从判断」（没有持久化层或查询失败），调用方此时应当
        按全量处理，不能把空集合当成「都已缓存」。
        """

        if self._store is None:
            return None
        try:
            return self._store.stale_indicator_symbols(tuple(formula_ids))
        except (AttributeError, RuntimeError, OSError):
            return None

    def preload(
        self,
        frames: Mapping[tuple[str, str], pd.DataFrame],
        formula_ids: Iterable[str],
        *,
        timeframe: str = _TIMEFRAME,
    ) -> int:
        """Load many existing built-in cache rows with one persistent read."""

        requested = tuple(dict.fromkeys(formula_ids))
        if not requested or not frames:
            return 0
        self.warm_revisions(frames.keys())
        definitions = {formula_id: self._registry.formula(formula_id) for formula_id in requested}
        pending: list[tuple[_CacheKey, FormulaDefinition]] = []
        for (market, code), frame in frames.items():
            revision = self._revision(str(market), str(code), frame)
            price_basis = _price_basis(frame)
            for formula_id in requested:
                key = _CacheKey(
                    market=str(market).strip().upper(),
                    code=str(code).strip(),
                    timeframe=timeframe,
                    price_basis=price_basis,
                    formula_id=formula_id,
                    formula_version=definitions[formula_id].cache_version,
                    data_revision=revision,
                )
                if self._memory_get(key) is None:
                    pending.append((key, definitions[formula_id]))
        if self._store is None or not pending:
            return 0
        try:
            started = time.perf_counter()
            try:
                rows = self._store.read_indicator_caches_batch(
                    [
                        {
                            "market": key.market,
                            "code": key.code,
                            "timeframe": key.timeframe,
                            "price_basis": key.price_basis,
                            "formula_id": key.formula_id,
                            "formula_version": key.formula_version,
                            "data_fingerprint": key.data_revision,
                        }
                        for key, _ in pending
                    ]
                )
            finally:
                self._record_phase("_persistent_read_time_ms", started)
        except (RuntimeError, sqlite3.Error):
            return 0
        grouped: dict[tuple[str, ...], list[dict[str, object]]] = {}
        for row in rows:
            grouped.setdefault(
                (
                    str(row["market"]),
                    str(row["code"]),
                    str(row["timeframe"]),
                    str(row["price_basis"]),
                    str(row["formula_id"]),
                    str(row["formula_version"]),
                    str(row["data_fingerprint"]),
                ),
                [],
            ).append(row)
        loaded = 0
        for key, definition in pending:
            chunk_rows = grouped.get(
                (
                    key.market,
                    key.code,
                    key.timeframe,
                    key.price_basis,
                    key.formula_id,
                    key.formula_version,
                    key.data_revision,
                )
            )
            last = chunk_rows[-1] if chunk_rows else None
            # 预热要的是整条序列，不按窗口裁剪，所以这里没有总长度可校验；
            # 块本身连续且末块日期存在即认为可用。
            result = self._decode_chunks(
                chunk_rows or [], definition, _row_end(last) if last else None
            )
            if result is None:
                continue
            self._memory_put(key, result, _row_end(last) if last else None)
            with self._lock:
                self._hits += 1
                self._persistent_hits += 1
            loaded += 1
        return loaded

    def get_or_compute(
        self,
        frame: pd.DataFrame,
        formula_ids: Iterable[str],
        *,
        market: str = "",
        code: str = "",
        timeframe: str = _TIMEFRAME,
        persistent_read: bool = True,
        persist: bool = True,
    ) -> dict[str, FormulaResult]:
        started = time.perf_counter()
        try:
            return self._get_or_compute(
                frame,
                formula_ids,
                market=market,
                code=code,
                timeframe=timeframe,
                persistent_read=persistent_read,
                persist=persist,
            )
        finally:
            self._record_latency(started)

    def _get_or_compute(
        self,
        frame: pd.DataFrame,
        formula_ids: Iterable[str],
        *,
        market: str = "",
        code: str = "",
        timeframe: str = _TIMEFRAME,
        persistent_read: bool = True,
        persist: bool = True,
    ) -> dict[str, FormulaResult]:
        """Load or calculate each requested built-in formula for one series."""

        requested = tuple(dict.fromkeys(formula_ids))
        if not requested:
            return {}
        normalized_market = market.strip().upper()
        normalized_code = code.strip()
        revision = self._revision(market, code, frame)
        price_basis = _price_basis(frame)
        start, end = _date_bounds(frame)
        target_length = len(frame)
        definitions = {formula_id: self._registry.formula(formula_id) for formula_id in requested}
        keys = {
            formula_id: _CacheKey(
                market=normalized_market,
                code=normalized_code,
                timeframe=timeframe,
                price_basis=price_basis,
                formula_id=formula_id,
                formula_version=definitions[formula_id].cache_version,
                data_revision=revision,
            )
            for formula_id in requested
        }
        results: dict[str, FormulaResult] = {}
        missing: dict[str, _CacheKey] = {}
        for formula_id in requested:
            cached = self._memory_get(keys[formula_id], end)
            sliced = _tail_slice(cached, target_length) if cached is not None else None
            if sliced is not None:
                results[formula_id] = sliced
                continue
            missing[formula_id] = keys[formula_id]

        # 只取覆盖请求窗口的那几块：读最近 250 根不必把 5 年的 blob 都读出来。
        persistent_rows = (
            self._persistent_get_many(missing, min_end_date=start) if persistent_read else {}
        )
        to_compute: list[str] = []
        for formula_id, key in missing.items():
            rows = persistent_rows.get(formula_id) or []
            # 缓存存的是一段连续序列，只有当请求区间正好是它的后缀时才可切片复用。
            decoded = self._decode_chunks(rows, definitions[formula_id], end)
            sliced = _tail_slice(decoded, target_length) if decoded is not None else None
            if sliced is not None and decoded is not None:
                self._memory_put(key, decoded, end)
                with self._lock:
                    self._hits += 1
                    self._persistent_hits += 1
                results[formula_id] = sliced
                continue
            to_compute.append(formula_id)

            with self._lock:
                self._misses += 1
        computed_entries: list[
            tuple[_CacheKey, FormulaResult, date | None, date | None, tuple[Chunk, ...]]
        ] = []
        # 版本号拿不到时键已绑定窗口（帧指纹），缓存同样安全；拿得到时只有
        # 完整序列可缓存，否则窗口结果会污染后续更长的请求。
        full_history = _revision_bar_count(revision) is None or (
            _revision_bar_count(revision) == target_length
        )
        chunks = _chunk_plan(frame)
        for formula_id in to_compute:
            key = keys[formula_id]
            definition = definitions[formula_id]
            started = time.perf_counter()
            result = definition.calculate(frame.copy(deep=True))
            self._record_phase("_compute_time_ms", started)
            with self._lock:
                self._computed += 1
            # 只缓存完整序列：窗口结果按同一个键存进去会污染后续更长的请求。
            if full_history:
                self._memory_put(key, result, end)
            computed_entries.append((key, result, start, end, chunks))
            results[formula_id] = result
        if persist:
            self._persistent_put_many(computed_entries)
        return results

    def persist_results(
        self,
        frames: Mapping[tuple[str, str], pd.DataFrame],
        results_by_key: Mapping[tuple[str, str], Mapping[str, FormulaResult]],
        *,
        timeframe: str = _TIMEFRAME,
        chunks_by_key: Mapping[tuple[str, str], tuple[Chunk, ...]] | None = None,
    ) -> None:
        """Persist many already-computed results in one database write.

        `chunks_by_key` 让调用方直接给出分块位置：预计算是逐年算完就落库的，
        它知道每一年在全历史中的偏移，无需再凑出一条完整序列去推断块边界。
        """

        if self._store is None or not results_by_key:
            return
        pending: list[
            tuple[_CacheKey, FormulaResult, date | None, date | None, tuple[Chunk, ...]]
        ] = []
        for (market, code), results in results_by_key.items():
            frame = frames.get((market, code))
            if frame is None or not results:
                continue
            revision = self._revision(str(market), str(code), frame)
            price_basis = _price_basis(frame)
            start, end = _date_bounds(frame)
            chunks = (chunks_by_key or {}).get((market, code)) or _chunk_plan(frame)
            for formula_id, result in results.items():
                definition = self._registry.formula(formula_id)
                if not result.sufficient_data:
                    continue
                pending.append(
                    (
                        _CacheKey(
                            market=str(market).strip().upper(),
                            code=str(code).strip(),
                            timeframe=timeframe,
                            price_basis=price_basis,
                            formula_id=formula_id,
                            formula_version=definition.cache_version,
                            data_revision=revision,
                        ),
                        result,
                        start,
                        end,
                        chunks,
                    )
                )
        self._persistent_put_many(pending)

    def metrics(self) -> dict[str, int | float]:
        """Return lightweight counters for benchmark and operational checks."""

        with self._lock:
            lookups = self._hits + self._misses
            samples = sorted(self._latency_samples)
            p50 = _percentile(samples, 0.50)
            p95 = _percentile(samples, 0.95)
            return {
                "lookups": lookups,
                "hits": self._hits,
                "misses": self._misses,
                "persistent_hits": self._persistent_hits,
                "computed": self._computed,
                "hit_rate": self._hits / lookups if lookups else 0.0,
                "p50_ms": p50,
                "p95_ms": p95,
                "compute_time_ms": self._compute_time_ms,
                "persistent_read_time_ms": self._persistent_read_time_ms,
                "persistent_write_time_ms": self._persistent_write_time_ms,
            }

    def status(self) -> dict[str, object]:
        """Return memory counters plus persistent cache size metadata."""

        with self._lock:
            memory_entries = len(self._memory)
        persistent = self._store.indicator_cache_status() if self._store is not None else {}
        return {**self.metrics(), "memory_entries": memory_entries, "persistent": persistent}

    def _memory_get(
        self,
        key: _CacheKey | _CustomCacheKey,
        end: date | None = None,
    ) -> FormulaResult | None:
        # 缓存里存的是以 `end` 收尾的序列。只按 key（标的+公式+版本号）命中会
        # 拿最新一段去切一个历史窗口的尾部——长度够但内容全错，所以必须对齐
        # 结束日期，和持久层的判定保持一致。
        with self._lock:
            entry = self._memory.get(key)
            if entry is None:
                return None
            result, stored_end = entry
            if end is not None and stored_end is not None and stored_end != end:
                return None
            self._memory.move_to_end(key)
            self._hits += 1
            return result

    def _memory_put(
        self,
        key: _CacheKey | _CustomCacheKey,
        result: FormulaResult,
        end: date | None = None,
    ) -> None:
        with self._lock:
            self._memory[key] = (result, end)
            self._memory.move_to_end(key)
            while len(self._memory) > self._max_memory_entries:
                self._memory.popitem(last=False)

    def _persistent_get_many(
        self,
        keys: dict[str, _CacheKey],
        *,
        min_end_date: date | None = None,
    ) -> dict[str, list[dict[str, object]]]:
        if self._store is None or not keys:
            return {}
        first = next(iter(keys.values()))
        try:
            started = time.perf_counter()
            try:
                return self._store.read_indicator_caches(
                    market=first.market,
                    code=first.code,
                    timeframe=first.timeframe,
                    price_basis=first.price_basis,
                    data_fingerprint=first.data_revision,
                    formula_versions={
                        formula_id: key.formula_version for formula_id, key in keys.items()
                    },
                    min_end_date=min_end_date,
                )
            finally:
                self._record_phase("_persistent_read_time_ms", started)
        except (RuntimeError, sqlite3.Error):
            return {}

    def _persistent_custom_get(self, key: _CustomCacheKey) -> dict[str, object] | None:
        if self._store is None or not key.market or not key.code:
            return None
        try:
            started = time.perf_counter()
            try:
                return self._store.read_custom_indicator_cache(
                    market=key.market,
                    code=key.code,
                    timeframe=key.timeframe,
                    price_basis=key.price_basis,
                    formula_hash=key.formula_hash,
                    parameter_hash=key.parameter_hash,
                    engine_version=key.engine_version,
                    data_fingerprint=key.data_revision,
                )
            finally:
                self._record_phase("_persistent_read_time_ms", started)
        except (RuntimeError, sqlite3.Error):
            return None

    def _decode_chunks(
        self,
        rows: Sequence[Mapping[str, object]],
        definition: FormulaDefinition,
        end: date | None,
        total_length: int | None = None,
    ) -> FormulaResult | None:
        """Reassemble the cached chunks that cover the requested window.

        任一块缺失、长度对不上或末块日期不是请求的结束日，都按未命中处理——
        宁可重算也不要拿一条拼接错误的历史序列去回测。
        """

        if not rows:
            return None
        ordered = sorted(rows, key=_chunk_offset)
        pieces: list[FormulaResult] = []
        cursor: int | None = None
        for row in ordered:
            offset = _chunk_offset(row)
            length = _chunk_length(row)
            if offset < 0 or length <= 0:
                return None
            if cursor is None:
                cursor = offset
            elif offset != cursor:
                return None
            piece = self._decode_persistent(row, definition)
            if piece is None:
                return None
            pieces.append(piece)
            cursor = offset + length
        if cursor is None:
            return None
        if total_length is not None and cursor != total_length:
            return None
        if not _row_ends_at(ordered[-1], end):
            return None
        return merge_results(pieces)

    def _decode_persistent(
        self,
        row: Mapping[str, object] | None,
        definition: FormulaDefinition,
    ) -> FormulaResult | None:
        if row is None:
            return None
        try:
            data_length = int(cast(int, row["data_length"]))
        except (TypeError, ValueError):
            return None
        if data_length <= 0:
            return None
        try:
            signal_names = tuple(signal.id.split(".", 1)[1] for signal in definition.signals)
            # 落库是 float32，读回来统一升成 float64，下游不必感知存储精度。
            values = _deserialize_arrays(
                row["values_blob"],
                definition.value_names,
                dtype=float,
                data_length=data_length,
            )
            signals = _deserialize_arrays(
                row["signals_blob"],
                signal_names,
                dtype=bool,
                data_length=data_length,
            )
            return FormulaResult.build(
                formula_id=definition.id,
                display_name=definition.display_name,
                minimum_bars=definition.minimum_bars,
                recommended_bars=definition.recommended_bars,
                data_length=data_length,
                values=values,
                signals=signals,
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            # A corrupt or incomplete derived row is a cache miss. The source
            # market data remains authoritative and will be recalculated.
            return None

    def _persistent_put_many(
        self,
        entries: Sequence[
            tuple[_CacheKey, FormulaResult, date | None, date | None, tuple[Chunk, ...]]
        ],
    ) -> None:
        if self._store is None or not entries:
            return
        pending: list[dict[str, object]] = []
        for key, result, start, end, chunks in entries:
            if not key.market or not key.code or not result.sufficient_data:
                continue
            definition = self._registry.formula(key.formula_id)
            values = {name: np.asarray(value) for name, value in result.values.items()}
            signals = {name: np.asarray(signal) for name, signal in result.signals.items()}
            signal_names = tuple(signal.id.split(".", 1)[1] for signal in definition.signals)
            # 拿不到日期分块信息（例如调用方只给了结果）时退回整条单块写入。
            plan = chunks or (Chunk(0, result.data_length, start, end),)
            for chunk in plan:
                if chunk.length <= 0 or chunk.offset + chunk.length > result.data_length:
                    continue
                stop = chunk.offset + chunk.length
                pending.append(
                    {
                        "market": key.market,
                        "code": key.code,
                        "timeframe": key.timeframe,
                        "price_basis": key.price_basis,
                        "formula_id": key.formula_id,
                        "formula_version": key.formula_version,
                        "data_fingerprint": key.data_revision,
                        "bar_offset": chunk.store_offset,
                        "data_start": chunk.start,
                        "data_end": chunk.end,
                        "data_length": chunk.length,
                        "values_blob": _serialize_arrays(
                            {name: value[chunk.offset : stop] for name, value in values.items()},
                            definition.value_names,
                            dtype=_VALUE_DTYPE,
                            data_length=chunk.length,
                        ),
                        "signals_blob": _serialize_arrays(
                            {name: signal[chunk.offset : stop] for name, signal in signals.items()},
                            signal_names,
                            dtype=bool,
                            data_length=chunk.length,
                        ),
                    }
                )
        if not pending:
            return
        try:
            first = pending[0]
            if not first["market"] or not first["code"]:
                return
            started = time.perf_counter()
            try:
                self._store.write_indicator_caches(pending)
            finally:
                self._record_phase("_persistent_write_time_ms", started)
        except (RuntimeError, sqlite3.Error):
            # 只读存储、以及 SQLite 后端的锁冲突/表缺失都属于「派生缓存不可用」：
            # 结果仍然算得出来，缓存只是加速，不能反过来让同步或回测失败。
            return

    def _decode_custom_persistent(
        self,
        row: dict[str, object] | None,
        parsed: ParsedFormula,
        parameters: dict[str, float],
    ) -> FormulaResult | None:
        if row is None:
            return None
        try:
            data_length = int(cast(int, row["data_length"]))
        except (TypeError, ValueError):
            return None
        if data_length <= 0:
            return None
        try:
            value_names = _json_names(row["value_names_json"])
            signal_names = _json_names(row["signal_names_json"])
            expected_signals = tuple(signal.id for signal in parsed.signals)
            if value_names != parsed.value_names or signal_names != expected_signals:
                return None
            values = _deserialize_arrays(
                row["values_blob"],
                value_names,
                dtype=float,
                data_length=data_length,
            )
            signals = _deserialize_arrays(
                row["signals_blob"],
                signal_names,
                dtype=bool,
                data_length=data_length,
            )
            minimum_bars = int(cast(int, row["minimum_bars"]))
            recommended_bars = int(cast(int, row["recommended_bars"]))
            if minimum_bars != required_bars_for_parameters(parsed, parameters):
                return None
            return FormulaResult.build(
                formula_id="custom",
                display_name="自定义公式",
                minimum_bars=minimum_bars,
                recommended_bars=recommended_bars,
                data_length=data_length,
                values=values,
                signals=signals,
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            return None

    def _persistent_put_custom(
        self,
        key: _CustomCacheKey,
        parsed: ParsedFormula,
        parameter_json: str,
        result: FormulaResult,
        start: date | None,
        end: date | None,
    ) -> None:
        if self._store is None or not key.market or not key.code or not result.sufficient_data:
            return
        values = {name: np.asarray(value) for name, value in result.values.items()}
        signals = {name: np.asarray(signal) for name, signal in result.signals.items()}
        try:
            started = time.perf_counter()
            try:
                self._store.write_custom_indicator_cache(
                    market=key.market,
                    code=key.code,
                    timeframe=key.timeframe,
                    price_basis=key.price_basis,
                    formula_hash=key.formula_hash,
                    parameter_hash=key.parameter_hash,
                    engine_version=key.engine_version,
                    parameter_json=parameter_json,
                    value_names_json=json.dumps(
                        list(parsed.value_names), ensure_ascii=False, separators=(",", ":")
                    ),
                    signal_names_json=json.dumps(
                        [signal.id for signal in parsed.signals],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    data_fingerprint=key.data_revision,
                    data_start=start,
                    data_end=end,
                    data_length=result.data_length,
                    minimum_bars=result.minimum_bars,
                    recommended_bars=result.recommended_bars,
                    values_blob=_serialize_arrays(
                        values,
                        parsed.value_names,
                        dtype=_VALUE_DTYPE,
                        data_length=result.data_length,
                    ),
                    signals_blob=_serialize_arrays(
                        signals,
                        tuple(signal.id for signal in parsed.signals),
                        dtype=bool,
                        data_length=result.data_length,
                    ),
                )
            finally:
                self._record_phase("_persistent_write_time_ms", started)
        except (RuntimeError, sqlite3.Error):
            return

    def _record_phase(self, attribute: str, started: float) -> None:
        elapsed = (time.perf_counter() - started) * 1000
        with self._lock:
            setattr(self, attribute, getattr(self, attribute) + elapsed)

    def _record_latency(self, started: float) -> None:
        elapsed = (time.perf_counter() - started) * 1000
        with self._lock:
            self._latency_samples.append(elapsed)


def _formula_hash(parsed: ParsedFormula) -> str:
    source = parsed.source.strip().replace("\r\n", "\n")
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _json_names(value: object) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise ValueError("指标缓存名称元数据无效")
    parsed = json.loads(value)
    if not isinstance(parsed, list) or not all(isinstance(name, str) for name in parsed):
        raise ValueError("指标缓存名称元数据无效")
    return tuple(parsed)


def _percentile(samples: list[float], quantile: float) -> float:
    if not samples:
        return 0.0
    index = min(len(samples) - 1, int((len(samples) - 1) * quantile))
    return float(samples[index])
