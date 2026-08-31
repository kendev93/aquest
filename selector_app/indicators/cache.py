"""Versioned, persistent cache for deterministic built-in formula results."""

from __future__ import annotations

import hashlib
import io
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from threading import RLock
from typing import cast

import numpy as np
import pandas as pd

from selector_app.formulas.registry import FORMULA_REGISTRY, FormulaDefinition, FormulaRegistry
from selector_app.formulas.types import FormulaResult
from selector_app.market_data.store import DuckDbMarketDataStore

_TIMEFRAME = "daily"
_DEFAULT_PRICE_BASIS = "raw"
_FINGERPRINT_COLUMNS = ("date", "open", "high", "low", "close", "volume", "amount")
_OPTIONAL_FINGERPRINT_COLUMNS = ("bar_status", "price_basis")
_MAX_CACHE_BLOB_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class _CacheKey:
    market: str
    code: str
    timeframe: str
    price_basis: str
    formula_id: str
    formula_version: str
    data_fingerprint: str


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


def _serialize_arrays(
    arrays: dict[str, np.ndarray], names: tuple[str, ...], *, dtype: type
) -> bytes:
    if names:
        matrix = np.stack([np.asarray(arrays[name], dtype=dtype) for name in names], axis=0)
    else:
        matrix = np.empty((0, 0), dtype=dtype)
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
        matrix = np.asarray(np.load(io.BytesIO(raw), allow_pickle=False), dtype=dtype)
    except (OSError, ValueError, EOFError) as exc:
        raise ValueError("指标缓存内容无效") from exc
    if matrix.ndim != 2 or matrix.shape != (len(names), data_length):
        raise ValueError("指标缓存内容无效")
    return {name: np.asarray(matrix[index]) for index, name in enumerate(names)}


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
        self._memory: OrderedDict[_CacheKey, FormulaResult] = OrderedDict()
        self._max_memory_entries = max_memory_entries
        self._lock = RLock()
        self._hits = 0
        self._misses = 0
        self._persistent_hits = 0
        self._computed = 0

    def get_or_compute(
        self,
        frame: pd.DataFrame,
        formula_ids: Iterable[str],
        *,
        market: str = "",
        code: str = "",
        timeframe: str = _TIMEFRAME,
    ) -> dict[str, FormulaResult]:
        """Load or calculate each requested built-in formula for one series."""

        requested = tuple(dict.fromkeys(formula_ids))
        if not requested:
            return {}
        fingerprint = frame_fingerprint(frame)
        normalized_market = market.strip().upper()
        normalized_code = code.strip()
        price_basis = _price_basis(frame)
        start, end = _date_bounds(frame)
        definitions = {formula_id: self._registry.formula(formula_id) for formula_id in requested}
        keys = {
            formula_id: _CacheKey(
                market=normalized_market,
                code=normalized_code,
                timeframe=timeframe,
                price_basis=price_basis,
                formula_id=formula_id,
                formula_version=definitions[formula_id].cache_version,
                data_fingerprint=fingerprint,
            )
            for formula_id in requested
        }
        results: dict[str, FormulaResult] = {}
        missing: dict[str, _CacheKey] = {}
        for formula_id in requested:
            result = self._memory_get(keys[formula_id])
            if result is not None:
                results[formula_id] = result
                continue
            missing[formula_id] = keys[formula_id]

        persistent_rows = self._persistent_get_many(missing)
        to_compute: list[str] = []
        for formula_id, key in missing.items():
            result = self._decode_persistent(
                persistent_rows.get(formula_id),
                definitions[formula_id],
                len(frame),
            )
            if result is not None:
                self._memory_put(key, result)
                with self._lock:
                    self._hits += 1
                    self._persistent_hits += 1
                results[formula_id] = result
                continue
            to_compute.append(formula_id)

            with self._lock:
                self._misses += 1
        computed_entries: list[tuple[_CacheKey, FormulaResult, date | None, date | None]] = []
        for formula_id in to_compute:
            key = keys[formula_id]
            definition = definitions[formula_id]
            result = definition.calculate(frame.copy(deep=True))
            with self._lock:
                self._computed += 1
            self._memory_put(key, result)
            computed_entries.append((key, result, start, end))
            results[formula_id] = result
        self._persistent_put_many(computed_entries)
        return results

    def metrics(self) -> dict[str, int | float]:
        """Return lightweight counters for benchmark and operational checks."""

        with self._lock:
            lookups = self._hits + self._misses
            return {
                "lookups": lookups,
                "hits": self._hits,
                "misses": self._misses,
                "persistent_hits": self._persistent_hits,
                "computed": self._computed,
                "hit_rate": self._hits / lookups if lookups else 0.0,
            }

    def _memory_get(self, key: _CacheKey) -> FormulaResult | None:
        with self._lock:
            result = self._memory.get(key)
            if result is not None:
                self._memory.move_to_end(key)
                self._hits += 1
            return result

    def _memory_put(self, key: _CacheKey, result: FormulaResult) -> None:
        with self._lock:
            self._memory[key] = result
            self._memory.move_to_end(key)
            while len(self._memory) > self._max_memory_entries:
                self._memory.popitem(last=False)

    def _persistent_get_many(
        self,
        keys: dict[str, _CacheKey],
    ) -> dict[str, dict[str, object]]:
        if self._store is None or not keys:
            return {}
        first = next(iter(keys.values()))
        try:
            return self._store.read_indicator_caches(
                market=first.market,
                code=first.code,
                timeframe=first.timeframe,
                price_basis=first.price_basis,
                data_fingerprint=first.data_fingerprint,
                formula_versions={
                    formula_id: key.formula_version for formula_id, key in keys.items()
                },
            )
        except RuntimeError:
            return {}

    def _decode_persistent(
        self,
        row: dict[str, object] | None,
        definition: FormulaDefinition,
        data_length: int,
    ) -> FormulaResult | None:
        if row is None or int(cast(int, row["data_length"])) != data_length:
            return None
        try:
            signal_names = tuple(signal.id.split(".", 1)[1] for signal in definition.signals)
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
        entries: list[tuple[_CacheKey, FormulaResult, date | None, date | None]],
    ) -> None:
        if self._store is None or not entries:
            return
        pending: list[dict[str, object]] = []
        for key, result, start, end in entries:
            if not key.market or not key.code or not result.sufficient_data:
                continue
            definition = self._registry.formula(key.formula_id)
            values = {name: np.asarray(value) for name, value in result.values.items()}
            signals = {name: np.asarray(signal) for name, signal in result.signals.items()}
            pending.append(
                {
                    "market": key.market,
                    "code": key.code,
                    "timeframe": key.timeframe,
                    "price_basis": key.price_basis,
                    "formula_id": key.formula_id,
                    "formula_version": key.formula_version,
                    "data_fingerprint": key.data_fingerprint,
                    "data_start": start,
                    "data_end": end,
                    "data_length": result.data_length,
                    "values_blob": _serialize_arrays(values, definition.value_names, dtype=float),
                    "signals_blob": _serialize_arrays(
                        signals,
                        tuple(signal.id.split(".", 1)[1] for signal in definition.signals),
                        dtype=bool,
                    ),
                }
            )
        if not pending:
            return
        try:
            first = pending[0]
            if not first["market"] or not first["code"]:
                return
            self._store.write_indicator_caches(pending)
        except RuntimeError:
            # A read-only store can still serve correct results; persistence is
            # an optimization and must not make a backtest fail.
            return
