"""Process-isolated staging files for online market-data synchronization."""

from __future__ import annotations

import json
import shutil
import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

import duckdb
import pandas as pd

from .models import MarketDataProvider
from .store_base import DEFAULT_PRICE_BASIS

STAGE_DIRECTORY_NAME = ".sync-staging"
STAGE_WORKERS = 8
STAGE_SCHEMA_VERSION = 1
STAGE_MIN_FREE_BYTES = 512 * 1024 * 1024
_DEFAULT_REQUEST_INTERVAL_SECONDS = 0.15
_MIN_REQUEST_INTERVAL_SECONDS = 0.05
_MAX_REQUEST_INTERVAL_SECONDS = 2.0
_SUCCESS_STEP = 32
# 限流状态是按 provider 分桶存的，每个桶有 5 个键。腾讯在 2026-09 提为日线主源后
# 也必须有一份，否则 worker 第一次请求就会 KeyError——东财那个桶是历史遗留，
# 只给它建状态会让「主源」反而没有限流记录。
_RATE_LIMITED_PROVIDERS: tuple[MarketDataProvider, ...] = ("eastmoney", "tencent")
_RATE_STATE_KEYS = (
    "next_allowed_{provider}",
    "interval_{provider}",
    "successes_{provider}",
    "rate_limit_events_{provider}",
    "backoffs_{provider}",
)
_LATEST_COLUMNS = ("date", "open", "high", "low", "close", "volume", "amount", "bar_status")
_STAGE_COLUMNS = (
    "market",
    "code",
    "instrument_type",
    "board",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "source",
    "provider",
    "price_basis",
    "bar_status",
    "imported_at",
    "name",
)

StageStatus = Literal["updated", "unchanged", "error"]


@dataclass(frozen=True)
class StageTarget:
    provider: MarketDataProvider
    market: str
    code: str
    instrument_type: str
    board: str
    name: str | None
    requested_ranges: tuple[tuple[date, date], ...]
    existing_first_date: date | None
    existing_last_date: date | None
    existing_bar_count: int
    existing_latest_rows: tuple[tuple[object, ...], ...]


@dataclass(frozen=True)
class ProviderStagePlan:
    provider: MarketDataProvider
    targets: tuple[StageTarget, ...]
    listed_instruments: int
    filtered_instruments: int
    new_instruments: int
    existing_instruments: int
    target_source: str
    provider_version: str | None
    instrument_list_fingerprint: str | None
    as_of: datetime


@dataclass(frozen=True)
class StageTargetResult:
    status: StageStatus
    frame: pd.DataFrame
    written_bars: int = 0
    range_start: str | None = None
    range_end: str | None = None
    retries: int = 0
    history_source: str = "empty"
    error: str | None = None


@dataclass(frozen=True)
class StageWorkerRequest:
    worker_id: int
    stage_path: str
    provider: MarketDataProvider
    targets: tuple[StageTarget, ...]
    config: Any
    as_of: datetime
    cancel_event: Any
    rate_state: Any
    rate_lock: Any
    event_queue: Any


@dataclass(frozen=True)
class StageWorkerResult:
    worker_id: int
    stage_path: str
    processed: int
    updated_files: int
    unchanged_files: int
    staged_rows: int
    errors: int
    failure_reasons: Mapping[str, int]
    history_sources: Mapping[str, int]


@dataclass(frozen=True)
class StageMergeReport:
    staged_rows: int
    merged_rows: int
    existing_online_rows: int
    affected_instruments: int


class ProcessRateGate:
    """A small process-safe, adaptive request gate backed by Manager proxies."""

    def __init__(self, state: Any, lock: Any) -> None:
        self._state = state
        self._lock = lock

    def wait(self, provider: MarketDataProvider) -> None:
        next_allowed_key = f"next_allowed_{provider}"
        interval_key = f"interval_{provider}"
        with self._lock:
            _ensure_provider_state(self._state, provider)
            now = time.monotonic()
            delay = max(0.0, float(self._state[next_allowed_key]) - now)
            interval = max(
                _MIN_REQUEST_INTERVAL_SECONDS,
                min(_MAX_REQUEST_INTERVAL_SECONDS, float(self._state[interval_key])),
            )
            self._state[next_allowed_key] = (
                max(now, float(self._state[next_allowed_key])) + interval
            )
        if delay > 0:
            time.sleep(delay)

    def report_for_provider(
        self,
        provider: MarketDataProvider,
        *,
        success: bool,
        throttled: bool = False,
    ) -> None:
        interval_key = f"interval_{provider}"
        successes_key = f"successes_{provider}"
        events_key = f"rate_limit_events_{provider}"
        backoffs_key = f"backoffs_{provider}"
        with self._lock:
            _ensure_provider_state(self._state, provider)
            if throttled:
                self._state[interval_key] = min(
                    _MAX_REQUEST_INTERVAL_SECONDS,
                    float(self._state[interval_key]) * 2,
                )
                self._state[successes_key] = 0
                self._state[events_key] = int(self._state[events_key]) + 1
                self._state[backoffs_key] = int(self._state[backoffs_key]) + 1
            elif success:
                successes = int(self._state[successes_key]) + 1
                if successes >= _SUCCESS_STEP:
                    self._state[interval_key] = max(
                        _MIN_REQUEST_INTERVAL_SECONDS,
                        float(self._state[interval_key]) * 0.9,
                    )
                    successes = 0
                self._state[successes_key] = successes

    def report(
        self,
        *,
        success: bool,
        throttled: bool = False,
        provider: MarketDataProvider = "eastmoney",
    ) -> None:
        self.report_for_provider(provider, success=success, throttled=throttled)


def create_rate_state(manager: Any) -> tuple[Any, Any]:
    initial: dict[str, object] = {}
    for provider in _RATE_LIMITED_PROVIDERS:
        initial.update(_provider_state_defaults(provider))
    return manager.dict(initial), manager.Lock()


def _provider_state_defaults(provider: MarketDataProvider) -> dict[str, object]:
    """One provider's five counters, ready to be merged into the shared state."""

    return {
        f"next_allowed_{provider}": 0.0,
        f"interval_{provider}": _DEFAULT_REQUEST_INTERVAL_SECONDS,
        f"successes_{provider}": 0,
        f"rate_limit_events_{provider}": 0,
        f"backoffs_{provider}": 0,
    }


def _ensure_provider_state(state: Any, provider: MarketDataProvider) -> None:
    """Backfill a provider's counters if the state predates it.

    状态字典由父进程创建、经 Manager 代理共享。旧进程（或将来新增的数据源）可能
    带着没有某个 provider 的状态进来，这里补默认值，省得第一次请求就 KeyError。
    """

    if f"interval_{provider}" in state:
        return
    state.update(_provider_state_defaults(provider))


def rate_state_snapshot(state: Any) -> dict[str, object]:
    providers = tuple(_RATE_LIMITED_PROVIDERS) + tuple(
        provider for provider in ("baostock", "tdx") if f"interval_{provider}" in state
    )
    by_provider = {
        provider: {
            "request_interval_seconds": float(state[f"interval_{provider}"]),
            "rate_limit_events": int(state[f"rate_limit_events_{provider}"]),
            "backoffs": int(state[f"backoffs_{provider}"]),
        }
        for provider in providers
    }
    return {
        "request_interval_seconds": max(
            values["request_interval_seconds"] for values in by_provider.values()
        ),
        "rate_limit_events": sum(
            int(values["rate_limit_events"]) for values in by_provider.values()
        ),
        "backoffs": sum(int(values["backoffs"]) for values in by_provider.values()),
        "by_provider": by_provider,
    }


def is_throttling_error(error: BaseException) -> bool:
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "429",
            "rate limit",
            "too many",
            "频繁",
            "限流",
            "请求过于频繁",
            "timeout",
            "timed out",
            "超时",
        )
    )


class StageWriter:
    """Write one worker's normalized rows and target manifest to its own DB."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = duckdb.connect(str(self.path), read_only=False)
        self._initialize()

    def _initialize(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS staged_bars (
                market VARCHAR NOT NULL,
                code VARCHAR NOT NULL,
                instrument_type VARCHAR NOT NULL,
                board VARCHAR NOT NULL,
                trade_date DATE NOT NULL,
                open DOUBLE NOT NULL,
                high DOUBLE NOT NULL,
                low DOUBLE NOT NULL,
                close DOUBLE NOT NULL,
                volume DOUBLE NOT NULL,
                amount DOUBLE NOT NULL,
                source VARCHAR NOT NULL,
                provider VARCHAR NOT NULL,
                price_basis VARCHAR NOT NULL,
                bar_status VARCHAR NOT NULL,
                imported_at TIMESTAMP NOT NULL,
                name VARCHAR,
                UNIQUE (market, code, trade_date, price_basis)
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS stage_manifest (
                market VARCHAR NOT NULL,
                code VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                requested_ranges VARCHAR NOT NULL,
                range_start DATE,
                range_end DATE,
                bar_count BIGINT NOT NULL,
                staged_rows BIGINT NOT NULL,
                retries BIGINT NOT NULL,
                history_source VARCHAR NOT NULL,
                error VARCHAR,
                updated_at TIMESTAMP NOT NULL,
                PRIMARY KEY (market, code)
            )
            """
        )

    def write_target(
        self,
        target: StageTarget,
        frame: pd.DataFrame,
        *,
        status: StageStatus,
        range_start: str | None,
        range_end: str | None,
        retries: int,
        history_source: str,
        error: str | None = None,
    ) -> int:
        incoming = _stage_frame(target, frame)
        requested_ranges = json.dumps(
            [[start.isoformat(), end.isoformat()] for start, end in target.requested_ranges],
            ensure_ascii=False,
        )
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        self._connection.execute("BEGIN")
        try:
            self._connection.execute(
                "DELETE FROM staged_bars WHERE market = ? AND code = ?",
                [target.market, target.code],
            )
            if not incoming.empty:
                self._connection.register("_stage_incoming", incoming)
                try:
                    self._connection.execute(
                        f"INSERT INTO staged_bars ({', '.join(_STAGE_COLUMNS)}) "
                        f"SELECT {', '.join(_STAGE_COLUMNS)} FROM _stage_incoming"
                    )
                finally:
                    self._connection.unregister("_stage_incoming")
            self._connection.execute(
                """
                INSERT INTO stage_manifest (
                    market, code, status, requested_ranges, range_start, range_end,
                    bar_count, staged_rows, retries, history_source, error, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (market, code) DO UPDATE SET
                    status = excluded.status,
                    requested_ranges = excluded.requested_ranges,
                    range_start = excluded.range_start,
                    range_end = excluded.range_end,
                    bar_count = excluded.bar_count,
                    staged_rows = excluded.staged_rows,
                    retries = excluded.retries,
                    history_source = excluded.history_source,
                    error = excluded.error,
                    updated_at = excluded.updated_at
                """,
                [
                    target.market,
                    target.code,
                    status,
                    requested_ranges,
                    range_start,
                    range_end,
                    len(frame),
                    len(incoming),
                    retries,
                    history_source,
                    error,
                    now,
                ],
            )
            self._connection.execute("COMMIT")
        except Exception:
            self._connection.execute("ROLLBACK")
            raise
        return len(incoming)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> StageWriter:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @classmethod
    def read_manifest(cls, path: str | Path) -> list[dict[str, object]]:
        with duckdb.connect(str(Path(path).expanduser()), read_only=True) as connection:
            result = connection.execute(
                "SELECT market, code, status, requested_ranges, range_start, range_end, "
                "bar_count, staged_rows, retries, history_source, error, updated_at "
                "FROM stage_manifest ORDER BY market, code"
            )
            columns = [item[0] for item in result.description]
            return [dict(zip(columns, row, strict=True)) for row in result.fetchall()]


def _stage_frame(target: StageTarget, frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=_STAGE_COLUMNS)
    required = set(_LATEST_COLUMNS)
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"分片行情缺少字段: {', '.join(missing)}")
    incoming = frame.loc[:, list(_LATEST_COLUMNS)].copy()
    incoming["trade_date"] = pd.to_datetime(incoming.pop("date"), errors="raise").dt.date
    incoming["market"] = target.market
    incoming["code"] = target.code
    incoming["instrument_type"] = target.instrument_type
    incoming["board"] = target.board
    incoming["source"] = "online"
    # ``target.provider`` identifies the catalog used to build the target list;
    # each fetched bar carries its actual history provider independently.
    incoming["provider"] = (
        frame["provider"].to_numpy(copy=True) if "provider" in frame.columns else target.provider
    )
    # 行情统一为不复权口径：库里只存已经发生过的事实。复权在**计算期**由
    # market_data.adjustment 按分红送转事件派生，绝不在这里、也不在库里落任何
    # 复权后的价格 —— 落了就等于把"结果"存下来，事件一更新那份数据就静默过期。
    incoming["price_basis"] = DEFAULT_PRICE_BASIS
    incoming["imported_at"] = datetime.now(timezone.utc).replace(tzinfo=None)
    incoming["name"] = target.name
    return incoming.loc[:, list(_STAGE_COLUMNS)].drop_duplicates(
        subset=["market", "code", "trade_date", "price_basis"],
        keep="last",
    )


def serialize_latest_rows(frame: pd.DataFrame) -> tuple[tuple[object, ...], ...]:
    if frame.empty:
        return ()
    rows: list[tuple[object, ...]] = []
    for values in frame.loc[:, list(_LATEST_COLUMNS)].itertuples(index=False, name=None):
        row = list(values)
        row[0] = pd.Timestamp(row[0]).date()
        rows.append(tuple(row))
    return tuple(rows)


def latest_frame_from_rows(rows: tuple[tuple[object, ...], ...]) -> pd.DataFrame:
    return pd.DataFrame(list(rows), columns=list(_LATEST_COLUMNS))


def staging_directory(database_path: str | Path) -> Path:
    return Path(database_path).expanduser().resolve().parent / STAGE_DIRECTORY_NAME


def ensure_staging_capacity(database_path: str | Path) -> None:
    """Reject a staged run before network work if its volume is nearly full."""

    database = Path(database_path).expanduser().resolve()
    database_size = database.stat().st_size if database.is_file() else 0
    required = max(STAGE_MIN_FREE_BYTES, database_size * 2)
    available = shutil.disk_usage(database.parent).free
    if available < required:
        raise RuntimeError(
            f"行情分片需要至少 {required / 1024**3:.1f} GiB 可用空间，"
            f"当前仅剩 {available / 1024**3:.1f} GiB"
        )


def new_staging_root(database_path: str | Path, run_id: str) -> Path:
    root = staging_directory(database_path) / run_id
    root.mkdir(parents=True, exist_ok=False)
    return root


def cleanup_staging_root(root: str | Path) -> None:
    path = Path(root).expanduser().resolve()
    if path.is_dir() and path.name != STAGE_DIRECTORY_NAME:
        shutil.rmtree(path)


def cleanup_stale_staging(
    root: str | Path,
    *,
    max_age_seconds: float = 24 * 60 * 60,
    now: float | None = None,
) -> tuple[Path, ...]:
    path = Path(root).expanduser().resolve()
    if max_age_seconds < 0:
        raise ValueError("临时行情分片保留时间不能小于 0")
    if not path.is_dir():
        return ()
    cutoff = (time.time() if now is None else now) - max_age_seconds
    removed: list[Path] = []
    for candidate in sorted(path.iterdir(), key=lambda item: item.name):
        if not candidate.is_dir():
            continue
        try:
            if candidate.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(candidate)
            removed.append(candidate)
        except FileNotFoundError:
            continue
    return tuple(removed)


def run_stage_worker(request: StageWorkerRequest) -> StageWorkerResult:
    """Entry point executed in a spawned process; imports providers lazily."""

    from aquest.adapters.market_sync import MarketDataCancelledError
    from aquest.adapters.online_market_sync import OnlineMarketSync

    gate = ProcessRateGate(request.rate_state, request.rate_lock)

    def cancel() -> bool:
        return bool(request.cancel_event.is_set())

    processed = 0
    updated_files = 0
    unchanged_files = 0
    staged_rows = 0
    failures: Counter[str] = Counter()
    history_sources: Counter[str] = Counter()
    with StageWriter(request.stage_path) as writer:
        provider = OnlineMarketSync(
            store=None,
            clock=lambda: request.as_of,
        )
        for target in request.targets:
            if cancel():
                raise MarketDataCancelledError("行情同步已取消")
            _put_event(
                request.event_queue,
                {
                    "kind": "started",
                    "worker_id": request.worker_id,
                    "market": target.market,
                    "code": target.code,
                    "name": target.name,
                    "provider": target.provider,
                    "range_label": _target_range_label(target, request.config),
                },
            )
            try:
                result = provider.fetch_stage_target(
                    target,
                    request.config,
                    abort_check=cancel,
                    rate_gate=gate,
                )
                staged = writer.write_target(
                    target,
                    result.frame,
                    status=result.status,
                    range_start=result.range_start,
                    range_end=result.range_end,
                    retries=result.retries,
                    history_source=result.history_source,
                    error=result.error,
                )
                staged_rows += staged
                if result.status == "updated":
                    updated_files += 1
                else:
                    unchanged_files += 1
                history_sources[result.history_source] += 1
            except MarketDataCancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - isolate one instrument
                reason = str(exc).strip()[:200] or type(exc).__name__
                failures[reason] += 1
                writer.write_target(
                    target,
                    pd.DataFrame(),
                    status="error",
                    range_start=None,
                    range_end=None,
                    retries=0,
                    history_source="error",
                    error=reason,
                )
                history_sources["error"] += 1
                result = StageTargetResult(
                    status="error",
                    frame=pd.DataFrame(),
                    error=reason,
                    history_source="error",
                )
            processed += 1
            _put_event(
                request.event_queue,
                {
                    "kind": "finished",
                    "worker_id": request.worker_id,
                    "market": target.market,
                    "code": target.code,
                    "name": target.name,
                    "provider": target.provider,
                    "status": result.status,
                    "staged_rows": len(result.frame),
                    "detail": _target_detail(target, result),
                },
            )
    return StageWorkerResult(
        worker_id=request.worker_id,
        stage_path=request.stage_path,
        processed=processed,
        updated_files=updated_files,
        unchanged_files=unchanged_files,
        staged_rows=staged_rows,
        errors=sum(failures.values()),
        failure_reasons=dict(failures),
        history_sources=dict(history_sources),
    )


def _target_detail(target: StageTarget, result: StageTargetResult) -> dict[str, object]:
    detail: dict[str, object] = {
        "phase": "fetching",
        "provider": target.provider,
        "current_status": result.status,
        "current_instrument": {
            "market": target.market,
            "code": target.code,
            "name": target.name,
        },
    }
    if result.range_start is not None:
        detail["range_start"] = result.range_start
    if result.range_end is not None:
        detail["range_end"] = result.range_end
    detail["retry"] = result.retries
    detail["history_source"] = result.history_source
    if result.error is not None:
        detail["error"] = result.error
    return detail


def _target_range_label(target: StageTarget, config: Any) -> str:
    if target.requested_ranges:
        return "；".join(
            f"{start.isoformat()} → {end.isoformat()}" for start, end in target.requested_ranges
        )
    return f"最新 {getattr(config, 'bars', 0)} 根日线"


def _put_event(queue: Any, payload: dict[str, object]) -> None:
    queue.put(payload)


__all__ = [
    "ProviderStagePlan",
    "ProcessRateGate",
    "STAGE_DIRECTORY_NAME",
    "STAGE_SCHEMA_VERSION",
    "STAGE_MIN_FREE_BYTES",
    "STAGE_WORKERS",
    "StageMergeReport",
    "StageTarget",
    "StageTargetResult",
    "StageWorkerRequest",
    "StageWorkerResult",
    "StageWriter",
    "cleanup_staging_root",
    "cleanup_stale_staging",
    "create_rate_state",
    "ensure_staging_capacity",
    "is_throttling_error",
    "latest_frame_from_rows",
    "new_staging_root",
    "rate_state_snapshot",
    "run_stage_worker",
    "serialize_latest_rows",
    "staging_directory",
]
