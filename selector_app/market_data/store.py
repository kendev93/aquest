"""Compatibility facade for the embedded DuckDB market-data store."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from .models import InstrumentBoard, InstrumentRef, InstrumentType, StockRef
from .store_bars import DuckDbBarsStore
from .store_base import (
    BACKTEST_HISTORY_LIMIT,
    SCHEMA_VERSION,
    SCREEN_HISTORY_LIMIT,
    STRATEGY_DISCOVERY_HISTORY_LIMIT,
    MarketDataStoreError,
    _DuckDbStoreBase,
    default_data_dir,
    default_database_path,
)
from .store_history import DuckDbHistoryStore
from .store_indicator_cache import DuckDbIndicatorCacheStore
from .store_source_files import DuckDbSourceFileStore


class DuckDbMarketDataStore(_DuckDbStoreBase):
    """Stable facade over focused DuckDB repositories.

    Callers keep the historical store API while each persistence concern lives
    in its own repository.  Shared connection, migration, locking, and frame
    normalization logic remains in :mod:`store_base`.
    """

    def __init__(self, database_path: str | Path | None = None, *, read_only: bool = False) -> None:
        super().__init__(database_path, read_only=read_only)
        self._bars = DuckDbBarsStore(self)
        self._history = DuckDbHistoryStore(self)
        self._indicator_cache = DuckDbIndicatorCacheStore(self)
        self._source_files = DuckDbSourceFileStore(self)

    def update_instrument_names(self, names: Mapping[tuple[str, str], str | None]) -> int:
        return self._bars.update_instrument_names(names)

    def replace_local_bars(
        self,
        market: str,
        code: str,
        instrument_type: InstrumentType,
        frame: pd.DataFrame,
        *,
        name: str | None = None,
        source_path: str | None = None,
        board: InstrumentBoard | None = None,
    ) -> int:
        return self._bars.replace_local_bars(
            market,
            code,
            instrument_type,
            frame,
            name=name,
            source_path=source_path,
            board=board,
        )

    def upsert_online_bars(self, frame: pd.DataFrame) -> tuple[int, int]:
        return self._bars.upsert_online_bars(frame)

    def read_bars(
        self,
        market: str,
        code: str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        include_provisional: bool = False,
    ) -> pd.DataFrame:
        return self._bars.read_bars(
            market,
            code,
            start_date=start_date,
            end_date=end_date,
            include_provisional=include_provisional,
        )

    def has_provisional_bars(self, market: str, code: str) -> bool:
        return self._bars.has_provisional_bars(market, code)

    def list_provisional_symbols(self) -> set[tuple[str, str]]:
        return self._bars.list_provisional_symbols()

    def read_many_bars(
        self,
        instruments: Iterable[InstrumentRef | StockRef] | None = None,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        include_provisional: bool = False,
    ) -> pd.DataFrame:
        return self._bars.read_many_bars(
            instruments,
            start_date=start_date,
            end_date=end_date,
            include_provisional=include_provisional,
        )

    def read_latest_bars(
        self,
        instruments: Iterable[InstrumentRef | StockRef],
        *,
        count: int = 2,
        include_provisional: bool = False,
    ) -> pd.DataFrame:
        return self._bars.read_latest_bars(
            instruments,
            count=count,
            include_provisional=include_provisional,
        )

    def list_instruments(
        self,
        *,
        market: str | None = None,
        keyword: str = "",
        include_missing: bool = False,
        instrument_types: Iterable[InstrumentType] | None = None,
        boards: Iterable[InstrumentBoard] | None = None,
    ) -> list[InstrumentRef]:
        return self._bars.list_instruments(
            market=market,
            keyword=keyword,
            include_missing=include_missing,
            instrument_types=instrument_types,
            boards=boards,
        )

    def save_screen_history(
        self,
        history_id: str,
        *,
        results: Iterable[Mapping[str, object]],
        meta: Mapping[str, object],
        created_at: datetime | None = None,
    ) -> None:
        self._history.save_screen_history(
            history_id,
            results=results,
            meta=meta,
            created_at=created_at,
        )

    def list_screen_history(self) -> list[dict[str, object]]:
        return self._history.list_screen_history()

    def read_screen_history(self, history_id: str) -> dict[str, object] | None:
        return self._history.read_screen_history(history_id)

    def save_strategy_discovery_history(
        self,
        history_id: str,
        result: Mapping[str, object],
        *,
        created_at: datetime | None = None,
        started_at: datetime | str | None = None,
        finished_at: datetime | str | None = None,
    ) -> None:
        self._history.save_strategy_discovery_history(
            history_id,
            result,
            created_at=created_at,
            started_at=started_at,
            finished_at=finished_at,
        )

    def list_strategy_discovery_history(self) -> list[dict[str, object]]:
        return self._history.list_strategy_discovery_history()

    def read_strategy_discovery_history(self, history_id: str) -> dict[str, object] | None:
        return self._history.read_strategy_discovery_history(history_id)

    def delete_strategy_discovery_history(self, history_id: str) -> bool:
        return self._history.delete_strategy_discovery_history(history_id)

    def save_strategy_discovery_replay_history(
        self,
        replay_id: str,
        result: Mapping[str, object],
        *,
        started_at: datetime | str,
        finished_at: datetime | str,
        created_at: datetime | str | None = None,
        duration_seconds: float | None = None,
    ) -> None:
        self._history.save_strategy_discovery_replay_history(
            replay_id,
            result,
            started_at=started_at,
            finished_at=finished_at,
            created_at=created_at,
            duration_seconds=duration_seconds,
        )

    def read_strategy_discovery_replay_history(
        self, experiment_id: str, leaderboard: str
    ) -> dict[str, object] | None:
        return self._history.read_strategy_discovery_replay_history(experiment_id, leaderboard)

    def save_backtest_history(
        self,
        history_id: str,
        *,
        backtest_type: str,
        request: Mapping[str, object],
        result: Mapping[str, object],
        started_at: datetime | str,
        finished_at: datetime | str,
        created_at: datetime | str | None = None,
        duration_seconds: float | None = None,
    ) -> None:
        self._history.save_backtest_history(
            history_id,
            backtest_type=backtest_type,
            request=request,
            result=result,
            started_at=started_at,
            finished_at=finished_at,
            created_at=created_at,
            duration_seconds=duration_seconds,
        )

    def list_backtest_history(self, backtest_type: str) -> list[dict[str, object]]:
        return self._history.list_backtest_history(backtest_type)

    def read_backtest_history(self, history_id: str) -> dict[str, object] | None:
        return self._history.read_backtest_history(history_id)

    def delete_backtest_history(self, history_id: str) -> bool:
        return self._history.delete_backtest_history(history_id)

    def read_indicator_cache(
        self,
        *,
        market: str,
        code: str,
        timeframe: str,
        price_basis: str,
        formula_id: str,
        formula_version: str,
        data_fingerprint: str,
    ) -> dict[str, object] | None:
        return self._indicator_cache.read_indicator_cache(
            market=market,
            code=code,
            timeframe=timeframe,
            price_basis=price_basis,
            formula_id=formula_id,
            formula_version=formula_version,
            data_fingerprint=data_fingerprint,
        )

    def read_indicator_caches(
        self,
        *,
        market: str,
        code: str,
        timeframe: str,
        price_basis: str,
        data_fingerprint: str,
        formula_versions: Mapping[str, str],
    ) -> dict[str, dict[str, object]]:
        return self._indicator_cache.read_indicator_caches(
            market=market,
            code=code,
            timeframe=timeframe,
            price_basis=price_basis,
            data_fingerprint=data_fingerprint,
            formula_versions=formula_versions,
        )

    def read_indicator_caches_batch(
        self,
        keys: Iterable[Mapping[str, str]],
    ) -> list[dict[str, object]]:
        return self._indicator_cache.read_indicator_caches_batch(keys)

    def read_custom_indicator_cache(
        self,
        *,
        market: str,
        code: str,
        timeframe: str,
        price_basis: str,
        formula_hash: str,
        parameter_hash: str,
        engine_version: str,
        data_fingerprint: str,
    ) -> dict[str, object] | None:
        return self._indicator_cache.read_custom_indicator_cache(
            market=market,
            code=code,
            timeframe=timeframe,
            price_basis=price_basis,
            formula_hash=formula_hash,
            parameter_hash=parameter_hash,
            engine_version=engine_version,
            data_fingerprint=data_fingerprint,
        )

    def write_indicator_cache(
        self,
        *,
        market: str,
        code: str,
        timeframe: str,
        price_basis: str,
        formula_id: str,
        formula_version: str,
        data_fingerprint: str,
        data_start: date | None,
        data_end: date | None,
        data_length: int,
        values_blob: bytes,
        signals_blob: bytes,
    ) -> None:
        self._indicator_cache.write_indicator_cache(
            market=market,
            code=code,
            timeframe=timeframe,
            price_basis=price_basis,
            formula_id=formula_id,
            formula_version=formula_version,
            data_fingerprint=data_fingerprint,
            data_start=data_start,
            data_end=data_end,
            data_length=data_length,
            values_blob=values_blob,
            signals_blob=signals_blob,
        )

    def write_indicator_caches(self, entries: Iterable[Mapping[str, object]]) -> None:
        self._indicator_cache.write_indicator_caches(entries)

    def write_custom_indicator_cache(
        self,
        *,
        market: str,
        code: str,
        timeframe: str,
        price_basis: str,
        formula_hash: str,
        parameter_hash: str,
        engine_version: str,
        parameter_json: str,
        value_names_json: str,
        signal_names_json: str,
        data_fingerprint: str,
        data_start: date | None,
        data_end: date | None,
        data_length: int,
        minimum_bars: int,
        recommended_bars: int,
        values_blob: bytes,
        signals_blob: bytes,
    ) -> None:
        self._indicator_cache.write_custom_indicator_cache(
            market=market,
            code=code,
            timeframe=timeframe,
            price_basis=price_basis,
            formula_hash=formula_hash,
            parameter_hash=parameter_hash,
            engine_version=engine_version,
            parameter_json=parameter_json,
            value_names_json=value_names_json,
            signal_names_json=signal_names_json,
            data_fingerprint=data_fingerprint,
            data_start=data_start,
            data_end=data_end,
            data_length=data_length,
            minimum_bars=minimum_bars,
            recommended_bars=recommended_bars,
            values_blob=values_blob,
            signals_blob=signals_blob,
        )

    def indicator_cache_status(self) -> dict[str, int | str | None]:
        return self._indicator_cache.indicator_cache_status()

    def source_file(self, source_root: str | Path, relative_path: str) -> dict[str, object] | None:
        return self._source_files.source_file(source_root, relative_path)

    def source_files(self, source_root: str | Path) -> list[dict[str, object]]:
        return self._source_files.source_files(source_root)

    def record_source_file(
        self,
        *,
        source_root: str | Path,
        relative_path: str,
        market: str,
        code: str,
        instrument_type: InstrumentType | None,
        size: int,
        mtime_ns: int,
        sha256: str | None,
        status: str,
        error: str | None = None,
        imported: bool = False,
    ) -> None:
        self._source_files.record_source_file(
            source_root=source_root,
            relative_path=relative_path,
            market=market,
            code=code,
            instrument_type=instrument_type,
            size=size,
            mtime_ns=mtime_ns,
            sha256=sha256,
            status=status,
            error=error,
            imported=imported,
        )

    def mark_source_missing(self, source_root: str | Path, relative_path: str) -> None:
        self._source_files.mark_source_missing(source_root, relative_path)


__all__ = [
    "BACKTEST_HISTORY_LIMIT",
    "SCREEN_HISTORY_LIMIT",
    "SCHEMA_VERSION",
    "STRATEGY_DISCOVERY_HISTORY_LIMIT",
    "DuckDbMarketDataStore",
    "MarketDataStoreError",
    "default_data_dir",
    "default_database_path",
]
