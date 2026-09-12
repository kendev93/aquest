"""AKShare-backed stock universe discovery and daily-bar synchronization."""

from __future__ import annotations

import hashlib
import importlib.metadata
import re
import time
from collections import Counter
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from threading import Lock
from typing import Protocol, cast
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from selector_app.market_data.day_format import classify_board, classify_instrument
from selector_app.market_data.models import (
    InstrumentBoard,
    InstrumentRef,
    InstrumentType,
    MarketCode,
)
from selector_app.market_data.scope import InstrumentScope
from selector_app.market_data.store import DuckDbMarketDataStore

from .market_sync import (
    AbortCheck,
    DetailCallback,
    InstrumentDiscoveryReport,
    MarketDataCancelledError,
    MarketSyncConfig,
    MarketSyncReport,
    ProgressCallback,
    _instrument_detail,
    _report_detail,
)

_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_MARKET_CLOSE = (15, 5)
_FULL_HISTORY_START = date(1990, 1, 1)
_INCREMENTAL_OVERLAP_DAYS = 10
_MAX_RETRIES = 3
_RETRY_BASE_SECONDS = 0.25
_REQUEST_INTERVAL_SECONDS = 0.15
_STOCK_BOARDS = frozenset({"main", "star", "chinext"})
_EMPTY_COLUMNS = [
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "bar_status",
]


class AkShareProviderError(RuntimeError):
    """Base error for actionable AKShare provider failures."""


class AkShareProviderUnavailable(AkShareProviderError):
    """Raised when the required AKShare runtime dependency cannot be imported."""


class AkShareDataError(AkShareProviderError):
    """Raised when an upstream AKShare response cannot be trusted."""


class AkShareModule(Protocol):
    __version__: str

    def stock_info_a_code_name(self) -> pd.DataFrame: ...

    def stock_zh_a_hist(self, **kwargs: object) -> pd.DataFrame: ...


AkShareModuleFactory = Callable[[], AkShareModule]
SleepFunction = Callable[[float], None]
AkShareTarget = tuple[MarketCode, str, InstrumentType, InstrumentBoard, str | None]


@dataclass(frozen=True)
class _TargetSelection:
    targets: list[AkShareTarget]
    listed_instruments: int
    filtered_instruments: int
    target_source: str
    fingerprint: str | None = None


@dataclass(frozen=True)
class _BatchReport:
    processed: int
    updated_files: int
    unchanged_files: int
    written_bars: int
    errors: int
    failure_reasons: Mapping[str, int]
    history_sources: Mapping[str, int]


@dataclass(frozen=True)
class _SyncResult:
    written_bars: int = 0
    bar_count: int = 0
    range_start: str | None = None
    range_end: str | None = None
    retries: int = 0
    history_source: str = "stock_zh_a_hist"


class _RateLimiter:
    def __init__(self, interval_seconds: float, sleep: SleepFunction) -> None:
        self._interval_seconds = interval_seconds
        self._sleep = sleep
        self._lock = Lock()
        self._next_allowed = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_allowed - now)
            self._next_allowed = max(now, self._next_allowed) + self._interval_seconds
        if delay > 0:
            self._sleep(delay)


def _default_module() -> AkShareModule:
    try:
        import akshare as module
    except ImportError as exc:
        raise AkShareProviderUnavailable(
            "AKShare 未安装，请重新构建 Docker 镜像或选择 TDX 行情源。"
        ) from exc
    return cast(AkShareModule, module)


class AkShareMarketSync:
    """Synchronize SH/SZ stock data through the project-owned store contract."""

    def __init__(
        self,
        store: DuckDbMarketDataStore | None = None,
        module_factory: AkShareModuleFactory | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: SleepFunction = time.sleep,
        request_interval_seconds: float = _REQUEST_INTERVAL_SECONDS,
    ) -> None:
        if request_interval_seconds < 0:
            raise ValueError("AKShare 请求间隔不能小于 0")
        self._store = store or DuckDbMarketDataStore()
        self._module_factory = module_factory or _default_module
        self._clock = clock or (lambda: datetime.now(_SHANGHAI_TZ))
        self._sleep = sleep
        self._rate_limiter = _RateLimiter(request_interval_seconds, sleep)

    def provider_version(self) -> str | None:
        try:
            module = self._module_factory()
        except AkShareProviderUnavailable:
            return None
        version = getattr(module, "__version__", None)
        if isinstance(version, str) and version.strip():
            return version.strip()
        try:
            return importlib.metadata.version("akshare")
        except importlib.metadata.PackageNotFoundError:
            return None

    def provider_capabilities(self) -> dict[str, object]:
        try:
            version = self.provider_version()
            self._module_factory()
        except AkShareProviderUnavailable as exc:
            return {
                "provider": "akshare",
                "available": False,
                "version": None,
                "error": str(exc),
                "instrument_types": [],
                "boards": [],
            }
        return {
            "provider": "akshare",
            "available": True,
            "version": version,
            "instrument_types": ["stock"],
            "boards": sorted(_STOCK_BOARDS),
            "history_sources": [
                "stock_zh_a_hist",
                "stock_zh_a_hist_tx",
                "stock_zh_a_daily",
            ],
            "fallback_policy": "EastMoney → Tencent → restricted Sina",
            "adjustments": ["raw"],
            "history": "full_or_incremental",
        }

    def discover_instruments(
        self,
        config: MarketSyncConfig,
        *,
        detail_callback: DetailCallback | None = None,
    ) -> InstrumentDiscoveryReport:
        _report_detail(
            detail_callback,
            {
                "phase": "instrument_discovery",
                "provider": "akshare",
                "current_status": "fetching",
                "range_label": "正在刷新 AKShare 沪深证券列表…",
            },
        )
        selection, new_instruments, existing_instruments = self._prepare_targets(config)
        report = InstrumentDiscoveryReport(
            total_candidates=len(selection.targets),
            listed_instruments=selection.listed_instruments,
            filtered_instruments=selection.filtered_instruments,
            new_instruments=new_instruments,
            existing_instruments=existing_instruments,
            target_source=selection.target_source,
            provider="akshare",
            provider_version=self.provider_version(),
            instrument_list_fingerprint=selection.fingerprint,
        )
        _report_detail(
            detail_callback,
            {
                "phase": "instrument_discovery",
                "provider": "akshare",
                "current_status": "completed",
                "discovery_summary": report.to_dict(),
            },
        )
        return report

    def sync(
        self,
        config: MarketSyncConfig,
        progress_callback: ProgressCallback | None = None,
        *,
        detail_callback: DetailCallback | None = None,
        abort_check: AbortCheck | None = None,
    ) -> MarketSyncReport:
        if abort_check is not None and abort_check():
            raise MarketDataCancelledError("行情同步已取消")
        selection, new_instruments, existing_instruments = self._prepare_targets(config)
        if abort_check is not None and abort_check():
            raise MarketDataCancelledError("行情同步已取消")
        targets = selection.targets
        total = len(targets)
        failure_reasons: Counter[str] = Counter()
        processed = 0
        updated_files = 0
        unchanged_files = 0
        written_bars = 0
        history_sources: Counter[str] = Counter()
        _report_detail(
            detail_callback,
            {
                "phase": "instrument_discovery",
                "provider": "akshare",
                "current_status": "completed",
                "discovery_summary": {
                    "total_candidates": total,
                    "listed_instruments": selection.listed_instruments,
                    "filtered_instruments": selection.filtered_instruments,
                    "new_instruments": new_instruments,
                    "existing_instruments": existing_instruments,
                    "target_source": selection.target_source,
                    "provider": "akshare",
                },
            },
        )
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
                thread_name_prefix="akshare-sync",
            ) as executor:
                futures = {
                    executor.submit(
                        self._sync_batch,
                        batch,
                        config,
                        existing_by_key,
                        on_target_done,
                        detail_callback,
                        abort_check,
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
                    history_sources.update(batch_report.history_sources)
        now = self._clock().isoformat()
        self._store.set_meta("last_online_sync_at", now)
        self._store.set_meta("last_online_provider", "akshare")
        version = self.provider_version()
        if version is not None:
            self._store.set_meta("last_online_provider_version", version)
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
            provider="akshare",
            provider_version=version,
            instrument_list_fingerprint=selection.fingerprint,
            history_sources=dict(history_sources),
        )

    def fetch_daily_bars(
        self,
        code: str,
        *,
        market: MarketCode | None = None,
        start_date: date,
        end_date: date,
        timeout: float,
    ) -> pd.DataFrame:
        frame, _retries, _source = self._fetch_with_retries(
            self._module_factory(),
            market=market or _market_for_stock_code(code) or "SZ",
            code=code,
            start_date=start_date,
            end_date=end_date,
            timeout=timeout,
        )
        return frame

    def _prepare_targets(
        self,
        config: MarketSyncConfig,
    ) -> tuple[_TargetSelection, int, int]:
        if not _scope_can_include_stock(config):
            return _empty_selection("akshare_stock_info_a_code_name"), 0, 0
        if config.prefer_store_targets:
            stored = self._store.list_instruments(
                market=None if config.universe == "all" else config.universe.upper(),
                instrument_types=("stock",),
                boards=config.boards,
            )
            selection = _selection_from_stored(stored)
        else:
            module = self._module_factory()
            frame = module.stock_info_a_code_name()
            selection = _selection_from_akshare_list(frame, config)
        selection = replace(selection, fingerprint=_fingerprint_targets(selection.targets))
        if selection.fingerprint is not None:
            self._store.set_meta("instrument_list_fingerprint", selection.fingerprint)
        names: dict[tuple[str, str], str | None] = {
            (market, code): name
            for market, code, _instrument_type, _board, name in selection.targets
            if name is not None
        }
        if names:
            self._store.update_instrument_names(names)
        refs = [
            InstrumentRef(
                market=market,
                code=code,
                instrument_type=instrument_type,
                board=board,
                name=name,
            )
            for market, code, instrument_type, board, name in selection.targets
        ]
        new_instruments = self._store.ensure_instruments(refs) if refs else 0
        return selection, new_instruments, max(len(refs) - new_instruments, 0)

    def _latest_existing_bars(
        self,
        targets: list[AkShareTarget],
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
        latest = self._store.read_latest_bars(refs, count=2, include_provisional=True)
        if latest.empty:
            return {}
        return {
            (str(market), str(code)): group.reset_index(drop=True)
            for (market, code), group in latest.groupby(["market", "code"], sort=False)
        }

    def _sync_batch(
        self,
        batch: list[AkShareTarget],
        config: MarketSyncConfig,
        existing_by_key: Mapping[tuple[str, str], pd.DataFrame],
        target_done: Callable[[], None],
        detail_callback: DetailCallback | None,
        abort_check: AbortCheck | None,
    ) -> _BatchReport:
        failure_reasons: Counter[str] = Counter()
        processed = 0
        updated_files = 0
        unchanged_files = 0
        written_bars = 0
        history_sources: Counter[str] = Counter()
        module = self._module_factory()
        for market, code, instrument_type, board, name in batch:
            if abort_check is not None and abort_check():
                raise MarketDataCancelledError("行情同步已取消")
            _report_detail(
                detail_callback,
                _instrument_detail(
                    market,
                    code,
                    name,
                    status="fetching",
                    range_label="正在读取完整日线…",
                )
                | {"provider": "akshare", "retry": 0},
            )
            try:
                sync_result = self._sync_one(
                    module,
                    market,
                    code,
                    instrument_type,
                    board,
                    name,
                    config,
                    existing=existing_by_key.get((market, code), pd.DataFrame()),
                )
                written = sync_result.written_bars
                _report_detail(
                    detail_callback,
                    _instrument_detail(
                        market,
                        code,
                        name,
                        status="updated" if written > 0 else "unchanged",
                        bar_count=sync_result.bar_count,
                        range_start=sync_result.range_start,
                        range_end=sync_result.range_end,
                        retry=sync_result.retries,
                    )
                    | {
                        "provider": "akshare",
                        "history_source": sync_result.history_source,
                    },
                )
                if written > 0:
                    updated_files += 1
                    written_bars += written
                else:
                    unchanged_files += 1
                history_sources[sync_result.history_source] += 1
            except MarketDataCancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - isolate one instrument
                reason = str(exc).strip()[:200] or type(exc).__name__
                failure_reasons[reason] += 1
                _report_detail(
                    detail_callback,
                    _instrument_detail(
                        market,
                        code,
                        name,
                        status="error",
                        error=reason,
                    )
                    | {"provider": "akshare"},
                )
            processed += 1
            target_done()
        return _BatchReport(
            processed=processed,
            updated_files=updated_files,
            unchanged_files=unchanged_files,
            written_bars=written_bars,
            errors=sum(failure_reasons.values()),
            failure_reasons=dict(failure_reasons),
            history_sources=dict(history_sources),
        )

    def _sync_one(
        self,
        module: AkShareModule,
        market: MarketCode,
        code: str,
        instrument_type: InstrumentType,
        board: InstrumentBoard,
        name: str | None,
        config: MarketSyncConfig,
        *,
        existing: pd.DataFrame,
    ) -> _SyncResult:
        latest_date = _latest_date(existing)
        start_date = (
            _FULL_HISTORY_START
            if config.full_history or latest_date is None
            else latest_date - timedelta(days=_INCREMENTAL_OVERLAP_DAYS)
        )
        end_date = _as_shanghai(self._clock()).date()
        frame, retries, history_source = self._fetch_with_retries(
            module,
            market=market,
            code=code,
            start_date=start_date,
            end_date=end_date,
            timeout=config.timeout,
        )
        normalized = _normalize_akshare_frame(
            frame,
            market=market,
            code=code,
            instrument_type=instrument_type,
            board=board,
            name=name,
            now=self._clock(),
            volume_scale=0.01
            if history_source in {"stock_zh_a_hist_tx", "stock_zh_a_daily"}
            else 1.0,
        )
        range_start, range_end = _frame_date_range(normalized)
        if normalized.empty:
            raise AkShareDataError(f"AKShare {code} 没有可用日线")
        if (
            not config.full_history
            and not existing.empty
            and not _has_new_data(existing, normalized)
        ):
            return _SyncResult(
                bar_count=len(normalized),
                range_start=range_start,
                range_end=range_end,
                retries=retries,
                history_source=history_source,
            )
        written, _updated = self._store.upsert_online_bars(normalized, provider="akshare")
        return _SyncResult(
            written_bars=written,
            bar_count=len(normalized),
            range_start=range_start,
            range_end=range_end,
            retries=retries,
            history_source=history_source,
        )

    def _fetch_with_retries(
        self,
        module: AkShareModule,
        *,
        market: MarketCode,
        code: str,
        start_date: date,
        end_date: date,
        timeout: float,
    ) -> tuple[pd.DataFrame, int, str]:
        date_start = start_date.strftime("%Y%m%d")
        date_end = end_date.strftime("%Y%m%d")
        market_symbol = f"{market.lower()}{code}"
        source_requests: tuple[tuple[str, str, dict[str, object]], ...] = (
            (
                "stock_zh_a_hist",
                "stock_zh_a_hist",
                {
                    "symbol": code,
                    "period": "daily",
                    "start_date": date_start,
                    "end_date": date_end,
                    "adjust": "",
                    "timeout": max(1, int(timeout)),
                },
            ),
            (
                "stock_zh_a_hist_tx",
                "stock_zh_a_hist_tx",
                {
                    "symbol": market_symbol,
                    "start_date": date_start,
                    "end_date": date_end,
                    "adjust": "",
                    "timeout": max(1, int(timeout)),
                },
            ),
            (
                "stock_zh_a_daily",
                "stock_zh_a_daily",
                {
                    "symbol": market_symbol,
                    "start_date": date_start,
                    "end_date": date_end,
                    "adjust": "",
                },
            ),
        )
        errors: list[str] = []
        available = False
        retries = 0
        for source_name, method_name, kwargs in source_requests:
            method = getattr(module, method_name, None)
            if not callable(method):
                continue
            available = True
            for attempt in range(_MAX_RETRIES + 1):
                self._rate_limiter.wait()
                try:
                    frame = method(**kwargs)
                    if not isinstance(frame, pd.DataFrame):
                        raise AkShareDataError(f"{source_name} 响应不是 DataFrame")
                    if frame.empty:
                        break
                    return frame, retries, source_name
                except AkShareDataError as exc:
                    errors.append(f"{source_name}: {exc}")
                    break
                except Exception as exc:  # noqa: BLE001 - upstream errors are retriable
                    errors.append(f"{source_name}: {exc}")
                    if attempt == _MAX_RETRIES:
                        break
                    retries += 1
                    self._sleep(_RETRY_BASE_SECONDS * (2**attempt))
        if not available:
            raise AkShareProviderError("AKShare 未提供可用的股票日线接口")
        if errors:
            detail = "; ".join(errors[-3:])
            raise AkShareProviderError(f"AKShare 获取 {code} 日线失败: {detail}")
        return pd.DataFrame(), retries, "empty"


def _scope_can_include_stock(config: MarketSyncConfig) -> bool:
    if config.instrument_types is not None and "stock" not in config.instrument_types:
        return False
    return config.boards is None or bool(set(config.boards) & _STOCK_BOARDS)


def _empty_selection(target_source: str) -> _TargetSelection:
    return _TargetSelection([], 0, 0, target_source)


def _selection_from_stored(stored: list[InstrumentRef]) -> _TargetSelection:
    targets: list[AkShareTarget] = [
        (ref.market, ref.code, ref.instrument_type, ref.board, ref.name)
        for ref in stored
        if ref.instrument_type == "stock"
    ]
    return _TargetSelection(
        targets=targets,
        listed_instruments=len(targets),
        filtered_instruments=0,
        target_source="akshare_store",
    )


def _selection_from_akshare_list(
    frame: pd.DataFrame,
    config: MarketSyncConfig,
) -> _TargetSelection:
    if not isinstance(frame, pd.DataFrame):
        raise AkShareDataError("AKShare 证券列表响应不是 DataFrame")
    if frame.empty:
        raise AkShareDataError("AKShare 证券列表为空")
    code_column = _find_column(frame, ("代码", "code", "证券代码"))
    name_column = _find_column(frame, ("名称", "name", "证券简称"))
    if code_column is None:
        raise AkShareDataError("AKShare 证券列表缺少代码字段")
    scope = InstrumentScope.from_values(
        universe=config.universe,
        instrument_types=("stock",),
        boards=config.boards,
    )
    seen: set[tuple[MarketCode, str]] = set()
    listed: set[tuple[MarketCode, str]] = set()
    targets: list[AkShareTarget] = []
    for row in frame.to_dict(orient="records"):
        code = _clean_code(row.get(code_column))
        if code is None:
            continue
        market = _market_for_stock_code(code)
        if market is None:
            continue
        instrument_type = classify_instrument(market, code)
        board = classify_board(market, code)
        if instrument_type != "stock" or board is None:
            continue
        key = (market, code)
        listed.add(key)
        name = _clean_name(row.get(name_column)) if name_column is not None else None
        if key in seen or not scope.matches(market, instrument_type, board):
            continue
        seen.add(key)
        targets.append((market, code, instrument_type, board, name))
    return _TargetSelection(
        targets=targets,
        listed_instruments=len(listed),
        filtered_instruments=max(len(listed) - len(targets), 0),
        target_source="akshare_stock_info_a_code_name",
    )


def _fingerprint_targets(targets: list[AkShareTarget]) -> str:
    canonical = "\n".join(
        f"{market}|{code}|{instrument_type}|{board}|{name or ''}"
        for market, code, instrument_type, board, name in sorted(targets)
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _find_column(frame: pd.DataFrame, candidates: tuple[str, ...]) -> str | None:
    return next((candidate for candidate in candidates if candidate in frame.columns), None)


def _clean_code(value: object) -> str | None:
    if value is None:
        return None
    missing = pd.isna(value)
    if isinstance(missing, (bool, np.bool_)) and bool(missing):
        return None
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    if not re.fullmatch(r"\d{1,6}", text):
        return None
    return text.zfill(6)


def _clean_name(value: object) -> str | None:
    if value is None:
        return None
    missing = pd.isna(value)
    if isinstance(missing, (bool, np.bool_)) and bool(missing):
        return None
    text = str(value).strip()
    return text or None


def _market_for_stock_code(code: str) -> MarketCode | None:
    if code.startswith("6") or code.startswith("9"):
        return "SH"
    if code.startswith(("0", "2", "3")):
        return "SZ"
    # AKShare also returns Beijing A shares from the broad list.  The current
    # store and backtest contracts are SH/SZ-only, so leave them out explicitly.
    return None


def _normalize_akshare_frame(
    frame: pd.DataFrame,
    *,
    market: MarketCode,
    code: str,
    instrument_type: InstrumentType,
    board: InstrumentBoard,
    name: str | None,
    now: datetime,
    volume_scale: float = 1.0,
) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise AkShareDataError("AKShare 日线响应不是 DataFrame")
    if frame.empty:
        return pd.DataFrame(
            columns=[*_EMPTY_COLUMNS, "market", "code", "name", "instrument_type", "board"]
        )
    aliases = {
        "date": ("日期", "date"),
        "open": ("开盘", "open"),
        "high": ("最高", "high"),
        "low": ("最低", "low"),
        "close": ("收盘", "close"),
        "volume": ("成交量", "成交量(手)", "volume", "vol"),
        "amount": ("成交额", "amount"),
    }
    columns = {key: _find_column(frame, candidates) for key, candidates in aliases.items()}
    missing = [key for key, value in columns.items() if value is None]
    if missing:
        raise AkShareDataError(f"AKShare 日线响应缺少字段: {', '.join(missing)}")
    normalized = frame.loc[:, [cast(str, columns[key]) for key in aliases]].copy()
    normalized.columns = list(aliases)
    normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce")
    if normalized["date"].isna().any():
        raise AkShareDataError("AKShare 日线响应包含无效日期")
    for column in ("open", "high", "low", "close", "volume", "amount"):
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    normalized["volume"] = normalized["volume"] * volume_scale
    values = normalized[["open", "high", "low", "close", "volume", "amount"]].to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise AkShareDataError("AKShare 日线响应包含无效数值")
    if (normalized[["open", "high", "low", "close"]] <= 0).any().any():
        raise AkShareDataError("AKShare 日线响应包含非正价格")
    if (normalized[["volume", "amount"]] < 0).any().any():
        raise AkShareDataError("AKShare 日线响应包含负成交数据")
    if (
        (normalized["low"] > normalized[["open", "close"]].min(axis=1))
        | (normalized["high"] < normalized[["open", "close"]].max(axis=1))
    ).any():
        raise AkShareDataError("AKShare 日线响应的高低价无法覆盖开收盘价")
    current = _as_shanghai(now)
    if (normalized["date"].dt.date > current.date()).any():
        raise AkShareDataError("AKShare 日线响应包含未来日期")
    normalized["bar_status"] = normalized["date"].map(
        lambda value: (
            "provisional"
            if value.date() == current.date() and (current.hour, current.minute) < _MARKET_CLOSE
            else "completed"
        )
    )
    normalized = normalized.sort_values("date", kind="stable").drop_duplicates(
        subset=["date"], keep="last"
    )
    normalized["market"] = market
    normalized["code"] = code
    normalized["name"] = name
    normalized["instrument_type"] = instrument_type
    normalized["board"] = board
    return normalized.reset_index(drop=True)


def _latest_date(frame: pd.DataFrame) -> date | None:
    if frame.empty or "date" not in frame.columns:
        return None
    dates = pd.to_datetime(frame["date"], errors="coerce").dropna()
    return pd.Timestamp(dates.max()).date() if not dates.empty else None


def _has_new_data(existing: pd.DataFrame, latest: pd.DataFrame) -> bool:
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


def _frame_date_range(frame: pd.DataFrame) -> tuple[str | None, str | None]:
    if frame.empty or "date" not in frame.columns:
        return None, None
    dates = pd.to_datetime(frame["date"], errors="coerce").dropna()
    if dates.empty:
        return None, None
    return (
        pd.Timestamp(dates.iloc[0]).strftime("%Y-%m-%d"),
        pd.Timestamp(dates.iloc[-1]).strftime("%Y-%m-%d"),
    )


def _as_shanghai(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=_SHANGHAI_TZ)
    return value.astimezone(_SHANGHAI_TZ)


def _partition_targets(
    targets: list[AkShareTarget], worker_count: int
) -> list[list[AkShareTarget]]:
    batch_count = min(max(worker_count, 1), len(targets))
    return [targets[index::batch_count] for index in range(batch_count)]


__all__ = [
    "AkShareDataError",
    "AkShareMarketSync",
    "AkShareModule",
    "AkShareProviderError",
    "AkShareProviderUnavailable",
]
