"""Online TDX daily-bar synchronization into the project's DuckDB store."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from typing import Literal, Protocol, cast
from zoneinfo import ZoneInfo

import pandas as pd

from selector_app.concurrency import MAX_WORKERS
from selector_app.market_data.day_format import classify_board, classify_instrument
from selector_app.market_data.models import (
    InstrumentBoard,
    InstrumentRef,
    InstrumentType,
    MarketCode,
)
from selector_app.market_data.scope import InstrumentScope
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.tdx_protocol.client import TdxClient
from selector_app.tdx_protocol.types import KlineCategory, Market

SyncUniverse = Literal["all", "sh", "sz"]
_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_LATEST_PROBE_BARS = 2
InstrumentTarget = tuple[MarketCode, str, InstrumentType, InstrumentBoard, str | None]


@dataclass(frozen=True)
class MarketSyncConfig:
    """Configuration for one online-to-DuckDB synchronization run."""

    universe: SyncUniverse = "all"
    bars: int = 800
    timeout: float = 30.0
    instrument_types: tuple[InstrumentType, ...] | None = None
    boards: tuple[InstrumentBoard, ...] | None = None
    prefer_store_targets: bool = False
    refresh_names: bool = False
    sync_workers: int = 8

    def __post_init__(self) -> None:
        if self.universe not in {"all", "sh", "sz"}:
            raise ValueError(f"不支持的同步范围: {self.universe}")
        if not 1 <= self.bars <= 800:
            raise ValueError("每只股票同步的 K 线数量必须在 1 到 800 之间")
        if self.timeout <= 0:
            raise ValueError("行情服务器超时时间必须大于 0")
        if not isinstance(self.prefer_store_targets, bool):
            raise ValueError("行情同步目标来源开关必须是布尔值")
        if not isinstance(self.refresh_names, bool):
            raise ValueError("行情名称刷新开关必须是布尔值")
        if isinstance(self.sync_workers, bool) or not 1 <= self.sync_workers <= MAX_WORKERS:
            raise ValueError(f"行情同步并发数必须在 1 到 {MAX_WORKERS} 之间")
        scope = InstrumentScope.from_values(
            universe=self.universe,
            instrument_types=self.instrument_types,
            boards=self.boards,
        )
        object.__setattr__(self, "instrument_types", scope.instrument_types)
        object.__setattr__(self, "boards", scope.boards)


@dataclass(frozen=True)
class MarketSyncReport:
    total_candidates: int
    processed: int
    updated_files: int
    unchanged_files: int
    written_bars: int
    errors: int
    failure_reasons: Mapping[str, int]
    listed_instruments: int = 0
    filtered_instruments: int = 0
    new_instruments: int = 0
    existing_instruments: int = 0
    target_source: str = "tdx_security_list"

    def to_dict(self) -> dict[str, object]:
        return {
            "source": "online",
            "total_candidates": self.total_candidates,
            "processed": self.processed,
            "updated_files": self.updated_files,
            "unchanged_files": self.unchanged_files,
            "written_bars": self.written_bars,
            "errors": self.errors,
            "failure_reasons": dict(self.failure_reasons),
            "listed_instruments": self.listed_instruments,
            "filtered_instruments": self.filtered_instruments,
            "new_instruments": self.new_instruments,
            "existing_instruments": self.existing_instruments,
            "target_source": self.target_source,
        }


@dataclass(frozen=True)
class InstrumentDiscoveryReport:
    """Result of refreshing the online instrument universe without bars."""

    total_candidates: int
    listed_instruments: int
    filtered_instruments: int
    new_instruments: int
    existing_instruments: int
    target_source: str = "tdx_security_list"

    def to_dict(self) -> dict[str, object]:
        return {
            "source": "online_discovery",
            "total_candidates": self.total_candidates,
            "listed_instruments": self.listed_instruments,
            "filtered_instruments": self.filtered_instruments,
            "new_instruments": self.new_instruments,
            "existing_instruments": self.existing_instruments,
            "target_source": self.target_source,
        }


@dataclass(frozen=True)
class _SyncBatchReport:
    processed: int
    updated_files: int
    unchanged_files: int
    written_bars: int
    errors: int
    failure_reasons: Mapping[str, int]


@dataclass(frozen=True)
class _TargetSelection:
    targets: list[InstrumentTarget]
    listed_instruments: int
    filtered_instruments: int
    target_source: str


class TdxMarketClient(Protocol):
    def __enter__(self) -> TdxMarketClient: ...

    def __exit__(self, *args: object) -> None: ...

    def get_security_list_all(self, pages: str = "all") -> pd.DataFrame: ...

    def get_security_bars(
        self,
        market: object,
        code: str,
        category: object,
        start: int,
        count: int,
    ) -> pd.DataFrame: ...


ClientFactory = Callable[[float], TdxMarketClient]
ProgressCallback = Callable[[int, int], None]


def _default_client(timeout: float) -> TdxMarketClient:
    return cast(TdxMarketClient, TdxClient.from_best_host(timeout=timeout))


class TdxMarketSync:
    """Fetch online daily bars and write them only to DuckDB.

    The implementation and client are project-owned; the old class name below
    remains as a compatibility alias for embedders of the first release.
    """

    def __init__(
        self,
        store: DuckDbMarketDataStore | None = None,
        client_factory: ClientFactory | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store or DuckDbMarketDataStore()
        self._client_factory = client_factory
        self._clock = clock or (lambda: datetime.now(_SHANGHAI_TZ))

    def discover_instruments(self, config: MarketSyncConfig) -> InstrumentDiscoveryReport:
        """Refresh and persist the scoped online universe without requesting bars."""

        selection, new_instruments, existing_instruments = self._prepare_targets(config)
        return InstrumentDiscoveryReport(
            total_candidates=len(selection.targets),
            listed_instruments=selection.listed_instruments,
            filtered_instruments=selection.filtered_instruments,
            new_instruments=new_instruments,
            existing_instruments=existing_instruments,
            target_source=selection.target_source,
        )

    def sync(
        self,
        config: MarketSyncConfig,
        progress_callback: ProgressCallback | None = None,
    ) -> MarketSyncReport:
        selection, new_instruments, existing_instruments = self._prepare_targets(config)
        factory = self._client_factory or _default_client
        targets = selection.targets
        total = len(targets)
        failure_reasons: Counter[str] = Counter()
        processed = 0
        updated_files = 0
        unchanged_files = 0
        written_bars = 0
        if progress_callback is not None:
            progress_callback(0, total)
        if targets:
            existing_by_key = self._latest_existing_bars(targets)
            batches = _partition_targets(targets, config.sync_workers)
            progress_lock = Lock()
            progress_count = 0

            def on_target_done() -> None:
                nonlocal progress_count
                with progress_lock:
                    progress_count += 1
                    current = progress_count
                if progress_callback is not None:
                    progress_callback(current, total)

            with ThreadPoolExecutor(
                max_workers=len(batches),
                thread_name_prefix="tdx-sync",
            ) as executor:
                futures = {
                    executor.submit(
                        self._sync_batch,
                        batch,
                        factory,
                        config,
                        existing_by_key,
                        on_target_done,
                    ): batch
                    for batch in batches
                }
                for future in as_completed(futures):
                    batch_report = future.result()
                    processed += batch_report.processed
                    updated_files += batch_report.updated_files
                    unchanged_files += batch_report.unchanged_files
                    written_bars += batch_report.written_bars
                    failure_reasons.update(batch_report.failure_reasons)
        self._store.set_meta("last_online_sync_at", self._clock().isoformat())
        return MarketSyncReport(
            total_candidates=total,
            processed=processed,
            updated_files=updated_files,
            unchanged_files=unchanged_files,
            written_bars=written_bars,
            errors=sum(failure_reasons.values()),
            failure_reasons=dict(failure_reasons),
            listed_instruments=selection.listed_instruments,
            filtered_instruments=selection.filtered_instruments,
            new_instruments=new_instruments,
            existing_instruments=existing_instruments,
            target_source=selection.target_source,
        )

    def _prepare_targets(
        self,
        config: MarketSyncConfig,
    ) -> tuple[_TargetSelection, int, int]:
        factory = self._client_factory or _default_client
        with factory(config.timeout) as client:
            selection = self._list_targets(client, config)
        targets = selection.targets
        new_instruments = 0
        if selection.target_source == "tdx_security_list":
            refs = [
                InstrumentRef(
                    market=market,
                    code=code,
                    instrument_type=instrument_type,
                    board=board,
                    name=name,
                )
                for market, code, instrument_type, board, name in targets
            ]
            new_instruments = self._store.ensure_instruments(refs)
        existing_instruments = max(len(targets) - new_instruments, 0)
        return selection, new_instruments, existing_instruments

    def _sync_batch(
        self,
        batch: list[InstrumentTarget],
        factory: ClientFactory,
        config: MarketSyncConfig,
        existing_by_key: Mapping[tuple[str, str], pd.DataFrame],
        target_done: Callable[[], None],
    ) -> _SyncBatchReport:
        failure_reasons: Counter[str] = Counter()
        processed = 0
        updated_files = 0
        unchanged_files = 0
        written_bars = 0
        try:
            with factory(config.timeout) as client:
                for market, code, instrument_type, board, name in batch:
                    try:
                        written = self._sync_one(
                            client,
                            market,
                            code,
                            instrument_type,
                            board,
                            name,
                            config,
                            existing=existing_by_key.get((market, code), pd.DataFrame()),
                        )
                        if written > 0:
                            updated_files += 1
                            written_bars += written
                        else:
                            unchanged_files += 1
                    except Exception as exc:  # noqa: BLE001 - isolate one instrument
                        reason = str(exc).strip()[:200] or type(exc).__name__
                        failure_reasons[reason] += 1
                        # An exception for one target should not stop the batch.
                    processed += 1
                    target_done()
        except Exception as exc:  # noqa: BLE001 - isolate one failed worker connection
            reason = str(exc).strip()[:200] or type(exc).__name__
            remaining = len(batch) - processed
            failure_reasons[reason] += remaining
            for _ in range(remaining):
                processed += 1
                target_done()
        return _SyncBatchReport(
            processed=processed,
            updated_files=updated_files,
            unchanged_files=unchanged_files,
            written_bars=written_bars,
            errors=sum(failure_reasons.values()),
            failure_reasons=dict(failure_reasons),
        )

    def _latest_existing_bars(
        self,
        targets: list[InstrumentTarget],
    ) -> dict[tuple[str, str], pd.DataFrame]:
        refs = [
            InstrumentRef(
                market=market,
                code=code,
                instrument_type=instrument_type,
                board=board,
                name=name,
            )
            for market, code, instrument_type, board, name in targets
        ]
        latest = self._store.read_latest_bars(
            refs,
            count=_LATEST_PROBE_BARS,
            include_provisional=True,
        )
        if latest.empty:
            return {}
        return {
            (str(market), str(code)): group.reset_index(drop=True)
            for (market, code), group in latest.groupby(["market", "code"], sort=False)
        }

    def _list_targets(
        self,
        client: TdxMarketClient,
        config: MarketSyncConfig,
    ) -> _TargetSelection:
        if config.prefer_store_targets:
            stored = self._store.list_instruments(
                market=None if config.universe == "all" else config.universe.upper(),
                instrument_types=config.instrument_types,
                boards=config.boards,
            )
            if stored:
                names_by_key: dict[tuple[str, str], str | None] = {}
                if config.refresh_names and any(ref.name is None for ref in stored):
                    try:
                        security_list = client.get_security_list_all(pages="all")
                    except Exception:  # noqa: BLE001 - name refresh must not block bar sync
                        security_list = pd.DataFrame()
                    stored_keys = {(ref.market, ref.code) for ref in stored}
                    for row in security_list.to_dict(orient="records"):
                        market = _to_market(row.get("market"))
                        raw_code = str(row.get("code", "")).strip()
                        if market is None or not raw_code.isdigit() or not 1 <= len(raw_code) <= 6:
                            continue
                        key = (market, raw_code.zfill(6))
                        name = _clean_security_name(row.get("name"))
                        if key in stored_keys and name is not None:
                            names_by_key[key] = name
                    self._store.update_instrument_names(names_by_key)
                return _TargetSelection(
                    targets=[
                        (
                            ref.market,
                            ref.code,
                            ref.instrument_type,
                            ref.board,
                            names_by_key.get((ref.market, ref.code), ref.name),
                        )
                        for ref in stored
                    ],
                    listed_instruments=len(stored),
                    filtered_instruments=0,
                    target_source="store",
                )
        frame = client.get_security_list_all(pages="all")
        if frame.empty:
            return _TargetSelection(
                targets=[],
                listed_instruments=0,
                filtered_instruments=0,
                target_source="tdx_security_list",
            )
        scope = InstrumentScope.from_values(
            universe=config.universe,
            instrument_types=config.instrument_types,
            boards=config.boards,
        )
        targets: list[InstrumentTarget] = []
        names: dict[tuple[str, str], str | None] = {}
        seen: set[tuple[MarketCode, str]] = set()
        listed: set[tuple[MarketCode, str]] = set()
        for row in frame.to_dict(orient="records"):
            market = _to_market(row.get("market"))
            raw_code = str(row.get("code", "")).strip()
            if not raw_code.isdigit() or not 1 <= len(raw_code) <= 6:
                continue
            code = raw_code.zfill(6)
            if market is None:
                continue
            instrument_type = classify_instrument(market, code)
            board = classify_board(market, code)
            if instrument_type is None or board is None:
                continue
            listed.add((market, code))
            if not scope.matches(market, instrument_type, board):
                continue
            key = (market, code)
            name = _clean_security_name(row.get("name"))
            if key not in seen:
                seen.add(key)
                targets.append((key[0], code, instrument_type, board, name))
                if name is not None:
                    names[key] = name
        self._store.update_instrument_names(names)
        return _TargetSelection(
            targets=targets,
            listed_instruments=len(listed),
            filtered_instruments=max(len(listed) - len(targets), 0),
            target_source="tdx_security_list",
        )

    def _sync_one(
        self,
        client: TdxMarketClient,
        market: MarketCode,
        code: str,
        instrument_type: InstrumentType,
        board: InstrumentBoard,
        name: str | None,
        config: MarketSyncConfig,
        *,
        existing: pd.DataFrame | None = None,
    ) -> int:
        existing = (
            existing
            if existing is not None
            else self._store.read_bars(
                market,
                code,
                include_provisional=True,
            )
        )
        if existing.empty:
            frame = client.get_security_bars(
                _market_enum(market), code, KlineCategory.DAY, 0, config.bars
            )
        else:
            latest = client.get_security_bars(
                _market_enum(market), code, KlineCategory.DAY, 0, _LATEST_PROBE_BARS
            )
            latest_frame = _normalize_online_frame(
                latest,
                market=market,
                code=code,
                instrument_type=instrument_type,
                board=board,
                name=name,
                now=self._clock(),
            )
            if not _has_new_online_data(existing, latest_frame):
                return 0
            frame = client.get_security_bars(
                _market_enum(market), code, KlineCategory.DAY, 0, config.bars
            )
        normalized = _normalize_online_frame(
            frame,
            market=market,
            code=code,
            instrument_type=instrument_type,
            board=board,
            name=name,
            now=self._clock(),
        )
        if normalized.empty:
            return 0
        written, _updated = self._store.upsert_online_bars(normalized)
        return written


def _partition_targets(
    targets: list[InstrumentTarget], worker_count: int
) -> list[list[InstrumentTarget]]:
    batch_count = min(max(worker_count, 1), len(targets))
    return [targets[index::batch_count] for index in range(batch_count)]


def _to_market(value: object) -> MarketCode | None:
    if isinstance(value, Market):
        return "SH" if value is Market.SH else "SZ"
    if isinstance(value, str):
        normalized = value.upper().strip()
        return cast(MarketCode, normalized) if normalized in {"SH", "SZ"} else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        mapping: dict[int, MarketCode] = {0: "SZ", 1: "SH"}
        return mapping.get(int(value))
    name = getattr(value, "name", None)
    if isinstance(name, str) and name.upper() in {"SH", "SZ"}:
        return cast(MarketCode, name.upper())
    return None


def _market_enum(value: MarketCode) -> Market:
    return Market.SH if value == "SH" else Market.SZ


def _clean_security_name(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _normalize_online_frame(
    frame: pd.DataFrame,
    *,
    market: MarketCode,
    code: str,
    instrument_type: InstrumentType,
    board: InstrumentBoard,
    name: str | None = None,
    now: datetime,
) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(
            columns=[
                "date",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "amount",
                "bar_status",
            ]
        )
    volume_column = "vol" if "vol" in frame.columns else "volume"
    required = {"open", "high", "low", "close", "amount", volume_column}
    if not required.issubset(frame.columns):
        missing = sorted(required - set(frame.columns))
        raise ValueError(f"在线 K 线响应缺少字段: {', '.join(missing)}")
    dates = pd.to_datetime(
        frame["date"] if "date" in frame.columns else frame.apply(_row_timestamp, axis=1),
        errors="coerce",
    )
    if dates.isna().any():
        raise ValueError("在线 K 线响应包含无效日期")
    current = now if now.tzinfo is not None else now.replace(tzinfo=_SHANGHAI_TZ)
    current = current.astimezone(_SHANGHAI_TZ)
    normalized = pd.DataFrame(
        {
            "date": dates,
            "open": pd.to_numeric(frame["open"], errors="coerce"),
            "high": pd.to_numeric(frame["high"], errors="coerce"),
            "low": pd.to_numeric(frame["low"], errors="coerce"),
            "close": pd.to_numeric(frame["close"], errors="coerce"),
            "volume": pd.to_numeric(frame[volume_column], errors="coerce"),
            "amount": pd.to_numeric(frame["amount"], errors="coerce"),
        }
    )
    if normalized.iloc[:, 1:].isna().any().any():
        raise ValueError("在线 K 线响应包含无效数值")
    normalized["bar_status"] = normalized["date"].map(
        lambda value: (
            "provisional"
            if value.date() == current.date() and (current.hour, current.minute) < (15, 5)
            else "completed"
        )
    )
    normalized["market"] = market
    normalized["code"] = code
    normalized["name"] = name
    normalized["instrument_type"] = instrument_type
    normalized["board"] = board
    return (
        normalized.sort_values("date", kind="stable")
        .drop_duplicates(subset=["date"], keep="last")
        .reset_index(drop=True)
    )


def _row_timestamp(row: pd.Series) -> object:
    if {"year", "month", "day"}.issubset(row.index):
        return pd.Timestamp(year=int(row["year"]), month=int(row["month"]), day=int(row["day"]))
    raise ValueError("在线 K 线响应缺少日期字段")


def _has_new_online_data(existing: pd.DataFrame, latest: pd.DataFrame) -> bool:
    if latest.empty:
        return False
    if existing.empty:
        return True
    existing_by_date = {
        pd.Timestamp(row.date).date(): row for row in existing.itertuples(index=False)
    }
    for row in latest.itertuples(index=False):
        current_date = pd.Timestamp(row.date).date()
        previous = existing_by_date.get(current_date)
        if previous is None:
            return True
        if row.bar_status != previous.bar_status:
            return True
        for column in ("open", "high", "low", "close", "volume", "amount"):
            if float(getattr(row, column)) != float(getattr(previous, column)):
                return True
    return False


__all__ = [
    "InstrumentDiscoveryReport",
    "MarketSyncConfig",
    "MarketSyncReport",
    "TdxMarketClient",
    "TdxMarketSync",
]
