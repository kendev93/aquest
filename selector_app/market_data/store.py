"""Compatibility facade for the embedded DuckDB market-data store."""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from .health import DEFAULT_EXAMPLE_LIMIT, HealthReport
from .models import InstrumentBoard, InstrumentRef, InstrumentType, StockRef
from .staging import StageMergeReport
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
from .store_health import DuckDbHealthStore, build_report
from .store_history import (
    STOCK_GROUP_SOURCE_MANUAL,
    DuckDbHistoryStore,
)
from .store_indicator_cache import DuckDbIndicatorCacheStore
from .store_indicator_cache_sqlite import SqliteIndicatorCacheStore

#: 派生指标缓存有两条后端可选：DuckDB（原实现）和独立的 SQLite 文件。
#: SQLite 落库实测快 22.5 倍、点查快 107 倍，批量读只是持平，所以默认走它；
#: 留环境变量是为了出问题时能一键退回旧实现，不用改代码重新发版。
INDICATOR_CACHE_BACKEND_ENV = "INDICATOR_CACHE_BACKEND"
SQLITE_INDICATOR_CACHE = "sqlite"
DUCKDB_INDICATOR_CACHE = "duckdb"

_IndicatorCacheStore = DuckDbIndicatorCacheStore | SqliteIndicatorCacheStore


def resolve_indicator_cache_backend(requested: str | None = None) -> str:
    """Pick the derived-cache backend from the explicit argument or the env."""

    raw = requested
    if raw is None:
        raw = os.environ.get(INDICATOR_CACHE_BACKEND_ENV) or SQLITE_INDICATOR_CACHE
    normalized = raw.strip().lower()
    if normalized not in (SQLITE_INDICATOR_CACHE, DUCKDB_INDICATOR_CACHE):
        raise ValueError(
            f"指标缓存后端必须是 {SQLITE_INDICATOR_CACHE} 或 {DUCKDB_INDICATOR_CACHE}，收到 {raw!r}"
        )
    return normalized


class DuckDbMarketDataStore(_DuckDbStoreBase):
    """Stable facade over focused DuckDB repositories.

    Callers keep the historical store API while each persistence concern lives
    in its own repository.  Shared connection, migration, locking, and frame
    normalization logic remains in :mod:`store_base`.
    """

    def __init__(
        self,
        database_path: str | Path | None = None,
        *,
        read_only: bool = False,
        pool_read_connections: bool = False,
        indicator_cache_backend: str | None = None,
    ) -> None:
        super().__init__(
            database_path, read_only=read_only, pool_read_connections=pool_read_connections
        )
        self._bars = DuckDbBarsStore(self)
        self._history = DuckDbHistoryStore(self)
        self._health = DuckDbHealthStore(self)
        self._indicator_cache_backend = resolve_indicator_cache_backend(indicator_cache_backend)
        self._indicator_cache: _IndicatorCacheStore = (
            DuckDbIndicatorCacheStore(self)
            if self._indicator_cache_backend == DUCKDB_INDICATOR_CACHE
            else SqliteIndicatorCacheStore(self)
        )

    @property
    def indicator_cache_backend(self) -> str:
        """Which backend holds the derived indicator caches."""

        return self._indicator_cache_backend

    @property
    def health(self) -> DuckDbHealthStore:
        """Read-only health scan over the stored daily bars."""

        return self._health

    def health_report(
        self,
        *,
        generated_at: str,
        duration_seconds: float = 0.0,
        example_limit: int = DEFAULT_EXAMPLE_LIMIT,
    ) -> HealthReport:
        """Run every health check and return the assembled report."""

        return build_report(
            self._health,
            generated_at=generated_at,
            duration_seconds=duration_seconds,
            example_limit=example_limit,
        )

    def update_instrument_names(self, names: Mapping[tuple[str, str], str | None]) -> int:
        return self._bars.update_instrument_names(names)

    def ensure_instruments(self, instruments: Iterable[InstrumentRef]) -> int:
        return self._bars.ensure_instruments(instruments)

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

    def upsert_local_bars(self, frame: pd.DataFrame) -> tuple[int, int]:
        return self._bars.upsert_local_bars(frame)

    def delete_bars(
        self,
        *,
        source: str | None = None,
        markets: Sequence[str] | None = None,
        codes: Sequence[str] | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> int:
        return self._bars.delete_bars(
            source=source,
            markets=markets,
            codes=codes,
            start_date=start_date,
            end_date=end_date,
        )

    def reset_instrument_coverage(self, refs: Sequence[tuple[str, str]]) -> int:
        return self._bars.reset_instrument_coverage(refs)

    def upsert_online_bars(
        self,
        frame: pd.DataFrame,
        *,
        provider: str = "eastmoney",
    ) -> tuple[int, int]:
        return self._bars.upsert_online_bars(frame, provider=provider)

    def merge_online_stage(self, stage_path: str | Path) -> StageMergeReport:
        return self._bars.merge_online_stage(stage_path)

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
        warmup_bars: int = 0,
        include_provisional: bool = False,
    ) -> pd.DataFrame:
        return self._bars.read_many_bars(
            instruments,
            start_date=start_date,
            end_date=end_date,
            warmup_bars=warmup_bars,
            include_provisional=include_provisional,
        )

    def read_close_matrix(
        self,
        instruments: Iterable[InstrumentRef | StockRef] | None = None,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        return self._bars.read_close_matrix(
            instruments,
            start_date=start_date,
            end_date=end_date,
        )

    def latest_trade_date(self) -> date | None:
        """最新一根已完成日线的日期，库里没有任何 bar 时给 ``None``。"""

        return self._bars.latest_trade_date()

    def save_market_trading_calendar(
        self,
        trade_dates: Iterable[date],
        *,
        source: str,
    ) -> int:
        return self._bars.save_market_trading_calendar(trade_dates, source=source)

    def latest_market_trading_date(self, on_or_before: date) -> date | None:
        return self._bars.latest_market_trading_date(on_or_before)

    def count_many_bars(
        self,
        instruments: Iterable[InstrumentRef | StockRef] | None = None,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        include_provisional: bool = False,
    ) -> int:
        return self._bars.count_many_bars(
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

    def save_research_trial(
        self,
        history_id: str,
        *,
        data_fingerprint: str,
        start_date: str,
        end_date: str,
        tested_conditions: int,
        created_at: datetime | str | None = None,
    ) -> None:
        self._history.save_research_trial(
            history_id,
            data_fingerprint=data_fingerprint,
            start_date=start_date,
            end_date=end_date,
            tested_conditions=tested_conditions,
            created_at=created_at,
        )

    def list_research_trials(self) -> list[dict[str, object]]:
        return self._history.list_research_trials()

    def reset_research_trials(self) -> int:
        return self._history.reset_research_trials()

    def list_backtest_history(self, backtest_type: str) -> list[dict[str, object]]:
        return self._history.list_backtest_history(backtest_type)

    def list_backtest_history_all(
        self,
        *,
        limit_per_type: int = BACKTEST_HISTORY_LIMIT,
    ) -> list[dict[str, object]]:
        return self._history.list_backtest_history_all(limit_per_type=limit_per_type)

    def read_backtest_history(self, history_id: str) -> dict[str, object] | None:
        return self._history.read_backtest_history(history_id)

    def read_backtest_histories(
        self,
        history_ids: Iterable[str],
    ) -> list[dict[str, object]]:
        return self._history.read_backtest_histories(history_ids)

    def get_meta(self, key: str) -> str | None:
        return super().get_meta(key)

    def delete_backtest_history(self, history_id: str) -> bool:
        return self._history.delete_backtest_history(history_id)

    def strategy_name_taken(self, name: str, *, exclude_strategy_id: str = "") -> bool:
        return self._history.strategy_name_taken(name, exclude_strategy_id=exclude_strategy_id)

    def save_strategy(
        self,
        strategy_id: str,
        *,
        name: str,
        request: Mapping[str, object],
        summary: Mapping[str, object],
        note: str = "",
        source_history_id: str | None = None,
        created_at: datetime | str | None = None,
    ) -> dict[str, object]:
        return self._history.save_strategy(
            strategy_id,
            name=name,
            request=request,
            summary=summary,
            note=note,
            source_history_id=source_history_id,
            created_at=created_at,
        )

    def list_strategies(self) -> list[dict[str, object]]:
        return self._history.list_strategies()

    def read_strategy(self, strategy_id: str) -> dict[str, object] | None:
        return self._history.read_strategy(strategy_id)

    def update_strategy(
        self,
        strategy_id: str,
        *,
        name: str | None = None,
        note: str | None = None,
    ) -> dict[str, object] | None:
        return self._history.update_strategy(strategy_id, name=name, note=note)

    def delete_strategy(self, strategy_id: str) -> bool:
        return self._history.delete_strategy(strategy_id)

    def screen_preset_name_taken(self, name: str, *, exclude_preset_id: str = "") -> bool:
        return self._history.screen_preset_name_taken(name, exclude_preset_id=exclude_preset_id)

    def save_screen_preset(
        self,
        preset_id: str,
        *,
        name: str,
        payload: Mapping[str, object],
        note: str = "",
        created_at: datetime | str | None = None,
    ) -> dict[str, object]:
        return self._history.save_screen_preset(
            preset_id,
            name=name,
            payload=payload,
            note=note,
            created_at=created_at,
        )

    def list_screen_presets(self) -> list[dict[str, object]]:
        return self._history.list_screen_presets()

    def read_screen_preset(self, preset_id: str) -> dict[str, object] | None:
        return self._history.read_screen_preset(preset_id)

    def update_screen_preset(
        self,
        preset_id: str,
        *,
        name: str | None = None,
        note: str | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object] | None:
        return self._history.update_screen_preset(preset_id, name=name, note=note, payload=payload)

    def delete_screen_preset(self, preset_id: str) -> bool:
        return self._history.delete_screen_preset(preset_id)

    def research_preset_name_taken(self, name: str, *, exclude_preset_id: str = "") -> bool:
        return self._history.research_preset_name_taken(name, exclude_preset_id=exclude_preset_id)

    def save_research_preset(
        self,
        preset_id: str,
        *,
        name: str,
        payload: Mapping[str, object],
        note: str = "",
        created_at: datetime | str | None = None,
    ) -> dict[str, object]:
        return self._history.save_research_preset(
            preset_id,
            name=name,
            payload=payload,
            note=note,
            created_at=created_at,
        )

    def list_research_presets(self) -> list[dict[str, object]]:
        return self._history.list_research_presets()

    def read_research_preset(self, preset_id: str) -> dict[str, object] | None:
        return self._history.read_research_preset(preset_id)

    def update_research_preset(
        self,
        preset_id: str,
        *,
        name: str | None = None,
        note: str | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object] | None:
        return self._history.update_research_preset(
            preset_id, name=name, note=note, payload=payload
        )

    def delete_research_preset(self, preset_id: str) -> bool:
        return self._history.delete_research_preset(preset_id)

    def stock_group_name_taken(self, name: str, *, exclude_group_id: str = "") -> bool:
        return self._history.stock_group_name_taken(name, exclude_group_id=exclude_group_id)

    def save_stock_group(
        self,
        group_id: str,
        *,
        name: str,
        items: Sequence[Mapping[str, object]],
        note: str = "",
        source: str = STOCK_GROUP_SOURCE_MANUAL,
        source_history_id: str | None = None,
        created_at: datetime | str | None = None,
    ) -> dict[str, object]:
        return self._history.save_stock_group(
            group_id,
            name=name,
            items=items,
            note=note,
            source=source,
            source_history_id=source_history_id,
            created_at=created_at,
        )

    def list_stock_groups(self) -> list[dict[str, object]]:
        return self._history.list_stock_groups()

    def list_stock_groups_with_items(self) -> list[dict[str, object]]:
        return self._history.list_stock_groups_with_items()

    def save_stock_group_criteria(
        self,
        group_id: str,
        *,
        criteria: Mapping[str, object],
        created_at: datetime | str | None = None,
    ) -> dict[str, object]:
        return self._history.save_stock_group_criteria(
            group_id, criteria=criteria, created_at=created_at
        )

    def read_stock_group_criteria(
        self,
        group_ids: Sequence[str],
    ) -> dict[str, dict[str, object]]:
        return self._history.read_stock_group_criteria(group_ids)

    def read_stock_group(self, group_id: str) -> dict[str, object] | None:
        return self._history.read_stock_group(group_id)

    def update_stock_group(
        self,
        group_id: str,
        *,
        name: str | None = None,
        note: str | None = None,
        items: Sequence[Mapping[str, object]] | None = None,
    ) -> dict[str, object] | None:
        return self._history.update_stock_group(group_id, name=name, note=note, items=items)

    def delete_stock_group(self, group_id: str) -> bool:
        return self._history.delete_stock_group(group_id)

    def find_candidate_by_source(
        self,
        source_history_id: str,
        source_candidate_id: str,
    ) -> dict[str, object] | None:
        return self._history.find_candidate_by_source(source_history_id, source_candidate_id)

    def save_candidate(
        self,
        candidate_id: str,
        *,
        source: str,
        source_history_id: str,
        source_candidate_id: str,
        side: str,
        label: str,
        condition: Mapping[str, object],
        evidence: Mapping[str, object],
        status: str,
        note: str = "",
        published_strategy_id: str | None = None,
        created_at: datetime | str | None = None,
    ) -> dict[str, object]:
        return self._history.save_candidate(
            candidate_id,
            source=source,
            source_history_id=source_history_id,
            source_candidate_id=source_candidate_id,
            side=side,
            label=label,
            condition=condition,
            evidence=evidence,
            status=status,
            note=note,
            published_strategy_id=published_strategy_id,
            created_at=created_at,
        )

    def list_candidates(self, status: str | None = None) -> list[dict[str, object]]:
        return self._history.list_candidates(status)

    def read_candidate(self, candidate_id: str) -> dict[str, object] | None:
        return self._history.read_candidate(candidate_id)

    def update_candidate(
        self,
        candidate_id: str,
        *,
        status: str | None = None,
        note: str | None = None,
        published_strategy_id: str | None = None,
        publish: bool = False,
    ) -> dict[str, object] | None:
        return self._history.update_candidate(
            candidate_id,
            status=status,
            note=note,
            published_strategy_id=published_strategy_id,
            publish=publish,
        )

    def delete_candidate(self, candidate_id: str) -> bool:
        return self._history.delete_candidate(candidate_id)

    def update_strategy_summary(
        self,
        strategy_id: str,
        summary: Mapping[str, object],
    ) -> dict[str, object] | None:
        return self._history.update_strategy_summary(strategy_id, summary)

    def start_strategy_tracking(
        self,
        strategy_id: str,
        *,
        launched_at: int,
        seed_points: Iterable[Mapping[str, object]] = (),
    ) -> dict[str, object] | None:
        return self._history.start_strategy_tracking(
            strategy_id,
            launched_at=launched_at,
            seed_points=seed_points,
        )

    def finish_strategy_tracking(
        self,
        strategy_id: str,
        *,
        last_tracked_date: int | None = None,
        error: str | None = None,
    ) -> dict[str, object] | None:
        return self._history.finish_strategy_tracking(
            strategy_id,
            last_tracked_date=last_tracked_date,
            error=error,
        )

    def stop_strategy_tracking(self, strategy_id: str) -> dict[str, object] | None:
        return self._history.stop_strategy_tracking(strategy_id)

    def save_tracking_points(
        self,
        strategy_id: str,
        points: Iterable[Mapping[str, object]],
        *,
        trades: Iterable[Mapping[str, object]] | None = None,
        plan: Mapping[str, object] | None = None,
        last_tracked_date: int | None = None,
        summary: Mapping[str, object] | None = None,
        tracking_state: Mapping[str, object] | None = None,
    ) -> int:
        """Save points alone, or atomically save a complete active refresh.

        ``last_tracked_date`` opts into the coordinated snapshot path. Its
        return value is then 1 for committed and 0 when the strategy was stopped
        or deleted; the legacy points-only form continues to return row count.
        """

        if last_tracked_date is not None:
            saved = self._history.save_strategy_tracking_snapshot(
                strategy_id,
                points=points,
                trades=trades or (),
                plan=plan,
                last_tracked_date=last_tracked_date,
                summary=summary,
                tracking_state=tracking_state,
            )
            return int(saved)
        if (
            trades is not None
            or plan is not None
            or summary is not None
            or tracking_state is not None
        ):
            raise ValueError("保存追踪明细时必须提供 last_tracked_date")
        return self._history.save_tracking_points(strategy_id, points)

    def save_strategy_tracking_snapshot(
        self,
        strategy_id: str,
        *,
        points: Iterable[Mapping[str, object]],
        trades: Iterable[Mapping[str, object]],
        plan: Mapping[str, object] | None,
        last_tracked_date: int,
        summary: Mapping[str, object] | None = None,
        tracking_state: Mapping[str, object] | None = None,
    ) -> bool:
        return self._history.save_strategy_tracking_snapshot(
            strategy_id,
            points=points,
            trades=trades,
            plan=plan,
            last_tracked_date=last_tracked_date,
            summary=summary,
            tracking_state=tracking_state,
        )

    def list_tracking_points(
        self,
        strategy_id: str,
        *,
        since: int | None = None,
    ) -> list[dict[str, object]]:
        return self._history.list_tracking_points(strategy_id, since=since)

    def save_tracking_trades(
        self,
        strategy_id: str,
        rows: Iterable[Mapping[str, object]],
    ) -> int:
        return self._history.save_tracking_trades(strategy_id, rows)

    def list_tracking_trades(self, strategy_id: str) -> list[dict[str, object]]:
        return self._history.list_tracking_trades(strategy_id)

    def page_tracking_trades(
        self,
        strategy_id: str,
        *,
        phase: str | None = None,
        date_from: int | None = None,
        date_to: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, object]:
        return self._history.page_tracking_trades(
            strategy_id,
            phase=phase,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
        )

    def save_tracking_plan(
        self,
        strategy_id: str,
        payload: Mapping[str, object],
    ) -> None:
        self._history.save_tracking_plan(strategy_id, payload)

    def read_tracking_plan(self, strategy_id: str) -> dict[str, object] | None:
        return self._history.read_tracking_plan(strategy_id)

    def list_tracked_strategies(self) -> list[dict[str, object]]:
        return self._history.list_tracked_strategies()

    def history_table_columns(self, table: str) -> tuple[str, ...]:
        return self._history.history_table_columns(table)

    def iter_history_rows(
        self,
        table: str,
        *,
        batch_size: int = 200,
    ) -> Iterator[dict[str, object]]:
        return self._history.iter_history_rows(table, batch_size=batch_size)

    def insert_history_rows(
        self,
        table: str,
        rows: Iterable[Mapping[str, object]],
        *,
        batch_size: int = 200,
    ) -> tuple[int, int]:
        return self._history.insert_history_rows(table, rows, batch_size=batch_size)

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
    ) -> list[dict[str, object]]:
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
        min_end_date: date | None = None,
    ) -> dict[str, list[dict[str, object]]]:
        return self._indicator_cache.read_indicator_caches(
            market=market,
            code=code,
            timeframe=timeframe,
            price_basis=price_basis,
            data_fingerprint=data_fingerprint,
            formula_versions=formula_versions,
            min_end_date=min_end_date,
        )

    def stale_indicator_symbols(
        self,
        formula_versions: Mapping[str, str],
    ) -> set[tuple[str, str]]:
        return self._indicator_cache.stale_symbols(formula_versions)

    def read_indicator_caches_batch(
        self,
        keys: Iterable[Mapping[str, str]],
    ) -> list[dict[str, object]]:
        return self._indicator_cache.read_indicator_caches_batch(keys)

    def indicator_cache_chunk_index(
        self,
        keys: Iterable[Mapping[str, str]],
    ) -> list[dict[str, object]]:
        return self._indicator_cache.indicator_cache_chunk_index(keys)

    def data_revisions(
        self,
        refs: Iterable[tuple[str, str]] | None = None,
    ) -> dict[tuple[str, str], str]:
        return self._indicator_cache.data_revisions(refs)

    def data_revision(self, market: str, code: str) -> str | None:
        return self._indicator_cache.data_revision(market, code)

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
        bar_offset: int = 0,
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
            bar_offset=bar_offset,
        )

    def write_indicator_caches(
        self,
        entries: Iterable[Mapping[str, object]],
        *,
        preserve_other_chunks: bool = False,
    ) -> None:
        self._indicator_cache.write_indicator_caches(
            entries, preserve_other_chunks=preserve_other_chunks
        )

    def retag_indicator_cache_chunks(
        self,
        entries: Iterable[Mapping[str, object]],
    ) -> None:
        self._indicator_cache.retag_indicator_cache_chunks(entries)

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

    def clear_indicator_caches(self) -> dict[str, int]:
        return self._indicator_cache.clear_indicator_caches()


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
