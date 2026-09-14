"""Bounded full-market daily synchronization with a single database writer."""

from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Protocol, cast

import pandas as pd

from selector_app.adapters.full_market_source import OnlineFullMarketSource
from selector_app.market_data.staging import ProcessRateGate, create_rate_state

ProgressCallback = Callable[[int, int], None]
DetailCallback = Callable[[Mapping[str, object]], None]
AbortCheck = Callable[[], bool]

_ASSET_TYPES = ("Stock", "ETF", "LOF", "Index")
_DEFAULT_START_DATE = date(1970, 1, 1)
_DEFAULT_MAX_WORKERS = 8
_DEFAULT_WORKER_MEMORY_MB = 256
_MAX_ATTEMPTS = 3
SyncRangePreset = Literal["all", "5y", "3y", "1y", "ytd", "custom"]


class MarketSyncCancelledError(RuntimeError):
    """Raised when a full-market synchronization is cancelled cooperatively."""


class FullMarketStorage(Protocol):
    def get_trade_calendar(self) -> pd.DataFrame: ...

    def get_symbol_date_coverage(
        self, symbol: str, data_type: str = "daily"
    ) -> dict[str, object]: ...

    def get_symbol_dates(
        self,
        symbol: str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        data_type: str = "daily",
    ) -> list[date]: ...

    def upsert_asset_info(self, frame: pd.DataFrame) -> int | None: ...

    def write_daily_bars(self, frame: pd.DataFrame) -> int: ...

    def save_system_setting(self, key: str, value: str) -> None: ...


class FullMarketSource(Protocol):
    def get_stock_list(self) -> pd.DataFrame: ...

    def get_etf_list(self) -> pd.DataFrame: ...

    def get_index_list(self) -> pd.DataFrame: ...

    def get_daily_data_for_asset(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        asset_type: str,
        *,
        adjust_type: str = "qfq",
    ) -> pd.DataFrame: ...


@dataclass(frozen=True)
class MarketSyncTarget:
    symbol: str
    name: str
    asset_type: str


@dataclass(frozen=True)
class MarketSyncJob:
    target: MarketSyncTarget
    start_date: str
    end_date: str


def _parse_date(value: object, *, default: date) -> date:
    if value is None or not str(value).strip():
        return default
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        raise ValueError("同步日期必须是 YYYY-MM-DD")
    return date.fromisoformat(str(parsed)[:10])


def _available_memory_mb() -> float | None:
    try:
        import psutil

        return float(psutil.virtual_memory().available) / (1024 * 1024)
    except Exception:  # noqa: BLE001 - optional dependency and platform fallback
        pass
    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        available_pages = int(os.sysconf("SC_AVPHYS_PAGES"))
        return float(page_size * available_pages) / (1024 * 1024)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _positive_int_env(name: str, default: int) -> int:
    raw = str(os.getenv(name) or "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def resolve_sync_range(
    preset: SyncRangePreset,
    start_date: date | None = None,
    end_date: date | None = None,
    *,
    today: date | None = None,
) -> tuple[date | None, date | None]:
    """Resolve a UI preset without changing the all-history compatibility default."""

    if preset not in {"all", "5y", "3y", "1y", "ytd", "custom"}:
        raise ValueError("行情同步区间无效")
    if preset == "custom":
        if start_date is None or end_date is None:
            raise ValueError("自定义同步区间必须同时设置开始日期和结束日期")
        if start_date > end_date:
            raise ValueError("同步开始日期不能晚于结束日期")
        return start_date, end_date
    if start_date is not None or end_date is not None:
        raise ValueError("快速同步区间不能同时设置自定义日期")
    if preset == "all":
        return None, None
    current = today or date.today()
    years = {"5y": 5, "3y": 3, "1y": 1}
    if preset in years:
        target_year = current.year - years[preset]
        try:
            start = current.replace(year=target_year)
        except ValueError:
            start = current.replace(year=target_year, day=28)
        return start, current
    return current.replace(month=1, day=1), current


def resolve_market_sync_workers(
    *,
    cpu_count: int | None = None,
    available_memory_mb: float | None = None,
    per_worker_mb: int = _DEFAULT_WORKER_MEMORY_MB,
) -> int:
    """Resolve a safe process count from CPU, memory, and explicit limits."""

    cpu_limit = max(1, int(cpu_count or (os.cpu_count() or 1)))
    max_workers = min(
        cpu_limit,
        _positive_int_env("ETFQUEST_MARKET_SYNC_MAX_WORKERS", _DEFAULT_MAX_WORKERS),
    )
    configured = str(os.getenv("ETFQUEST_MARKET_SYNC_WORKERS") or "auto").strip().lower()
    if configured not in {"", "auto"}:
        try:
            max_workers = min(max_workers, max(1, int(configured)))
        except (TypeError, ValueError):
            pass
    memory = available_memory_mb if available_memory_mb is not None else _available_memory_mb()
    if memory is None:
        return max(1, min(max_workers, 2))
    budget_raw = str(os.getenv("ETFQUEST_MARKET_SYNC_MEMORY_BUDGET_MB") or "auto").strip().lower()
    if budget_raw in {"", "auto"}:
        budget = max(1.0, memory * 0.6)
    else:
        try:
            budget = max(1.0, float(budget_raw))
        except (TypeError, ValueError):
            budget = max(1.0, memory * 0.6)
    safe_per_worker = max(1.0, float(per_worker_mb) * 1.5)
    memory_limit = max(1, int(budget // safe_per_worker))
    return max(1, min(max_workers, memory_limit))


def _normalise_symbol(raw_symbol: object) -> str:
    symbol = str(raw_symbol or "").strip()
    return "" if symbol.lower() in {"", "nan", "none", "null"} else symbol


def _normalise_asset_type(raw_type: object, default: str) -> str:
    value = str(raw_type or default).strip().lower()
    aliases = {
        "stock": "Stock",
        "股票": "Stock",
        "etf": "ETF",
        "lof": "LOF",
        "index": "Index",
        "指数": "Index",
    }
    return aliases.get(value, default)


def _discover_catalog(
    source: FullMarketSource,
    *,
    detail_callback: DetailCallback | None = None,
) -> tuple[list[MarketSyncTarget], dict[str, int]]:
    loaders: list[tuple[str, Any]] = [
        ("Stock", source.get_stock_list),
        ("ETF", source.get_etf_list),
    ]
    lof_loader = getattr(source, "get_lof_list", None)
    if callable(lof_loader):
        loaders.append(("LOF", lof_loader))
    loaders.append(("Index", source.get_index_list))
    priority = {"Stock": 1, "ETF": 2, "LOF": 3, "Index": 4}
    by_symbol: dict[str, MarketSyncTarget] = {}
    for index, (default_type, loader) in enumerate(loaders, start=1):
        frame = loader()
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            raise ValueError(f"{default_type}资产目录为空，已停止全市场同步")
        _emit_detail(
            detail_callback,
            {
                "phase": "instrument_discovery",
                "current_status": "completed",
                "category": default_type,
                "category_count": len(frame),
                "category_index": index,
                "category_total": len(loaders),
            },
        )
        code_column = _find_column(frame, ("symbol", "代码", "code"))
        name_column = _find_column(frame, ("name", "名称", "证券简称"))
        if code_column is None:
            raise ValueError(f"{default_type}资产目录缺少代码字段")
        for row in frame.to_dict(orient="records"):
            symbol = _normalise_symbol(row.get(code_column))
            if not symbol:
                continue
            asset_type = _normalise_asset_type(row.get("type"), default_type)
            target = MarketSyncTarget(
                symbol=symbol,
                name=_normalise_symbol(row.get(name_column)) if name_column else symbol,
                asset_type=asset_type,
            )
            previous = by_symbol.get(symbol)
            if previous is None or priority.get(asset_type, 0) >= priority.get(
                previous.asset_type, 0
            ):
                by_symbol[symbol] = target
    targets = sorted(by_symbol.values(), key=lambda item: (item.asset_type, item.symbol))
    counts = {
        asset_type: sum(item.asset_type == asset_type for item in targets)
        for asset_type in _ASSET_TYPES
    }
    return targets, counts


def _find_column(frame: pd.DataFrame, candidates: tuple[str, ...]) -> str | None:
    return next((candidate for candidate in candidates if candidate in frame.columns), None)


def _calendar_dates(storage: FullMarketStorage, start: date, end: date) -> list[date]:
    try:
        calendar = storage.get_trade_calendar()
    except Exception:  # noqa: BLE001 - a missing calendar is a valid fallback
        return []
    if not isinstance(calendar, pd.DataFrame) or calendar.empty:
        return []
    column = "trade_date" if "trade_date" in calendar.columns else "date"
    if column not in calendar.columns:
        return []
    values = pd.to_datetime(calendar[column], errors="coerce").dropna().dt.date
    return sorted({value for value in values.tolist() if start <= value <= end})


def _group_calendar_dates(values: Iterable[date]) -> list[tuple[date, date]]:
    dates = sorted(set(values))
    if not dates:
        return []
    ranges: list[tuple[date, date]] = []
    range_start = previous = dates[0]
    for current in dates[1:]:
        if current <= previous + timedelta(days=4):
            previous = current
            continue
        ranges.append((range_start, previous))
        range_start = previous = current
    ranges.append((range_start, previous))
    return ranges


def target_ranges(
    storage: FullMarketStorage,
    target: MarketSyncTarget,
    start: date,
    end: date,
    *,
    initial_start: date | None = None,
) -> list[tuple[date, date]]:
    """Return missing ranges and repair the whole interval when a gap exists."""

    coverage = storage.get_symbol_date_coverage(target.symbol, "daily") or {}
    earliest = (
        _parse_date(coverage.get("earliest_date"), default=start)
        if coverage.get("earliest_date")
        else None
    )
    latest = (
        _parse_date(coverage.get("latest_date"), default=start)
        if coverage.get("latest_date")
        else None
    )
    if latest is None:
        return [(max(start, initial_start or start), end)]
    calendar = _calendar_dates(storage, start, end)
    if not calendar:
        if latest >= end:
            return [] if earliest is not None and earliest <= start else [(start, end)]
        return [(max(start, latest + timedelta(days=1)), end)]
    expected = calendar
    try:
        existing = set(
            storage.get_symbol_dates(
                target.symbol,
                start_date=start,
                end_date=end,
                data_type="daily",
            )
        )
    except Exception:  # noqa: BLE001 - a failed probe must be repaired by download
        existing = set()
    if (
        latest >= end
        and earliest is not None
        and earliest <= start
        and len(existing) >= len(expected)
    ):
        return []
    local_end = min(latest, end)
    coverage_start = max(start, earliest or start)
    internal_expected = [value for value in expected if coverage_start <= value <= local_end]
    if any(value not in existing for value in internal_expected):
        return [(start, end)]
    missing = [value for value in expected if value not in existing]
    if not missing:
        return []
    return _group_calendar_dates(missing)


def _standardise_result(frame: pd.DataFrame, target: MarketSyncTarget) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return pd.DataFrame()
    result = frame.rename(
        columns={
            "日期": "date",
            "开盘": "open",
            "收盘": "close",
            "最高": "high",
            "最低": "low",
            "成交量": "volume",
        }
    ).copy()
    required = ["date", "open", "close", "high", "low"]
    missing = [column for column in required if column not in result.columns]
    if missing:
        raise ValueError(f"数据源返回字段不完整: {', '.join(missing)}")
    if "volume" not in result.columns:
        result["volume"] = 0
    result["date"] = pd.to_datetime(result["date"], errors="coerce").dt.date
    for column in ("open", "close", "high", "low", "volume"):
        result[column] = pd.to_numeric(result[column], errors="coerce")
    result = result.dropna(subset=required)
    result = result.loc[result["date"].notna()]
    result["symbol"] = target.symbol
    result["data_type"] = "daily"
    result["update_time"] = datetime.now(timezone.utc).replace(tzinfo=None)
    return result[
        ["symbol", "date", "open", "close", "high", "low", "volume", "data_type", "update_time"]
    ].drop_duplicates(["symbol", "date", "data_type"], keep="last")


def _write_stage(path: Path, frame: pd.DataFrame) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(frame.to_dict(orient="records"), default=str, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_stage(path: Path) -> pd.DataFrame:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("同步临时结果格式无效")
    return pd.DataFrame(payload)


def _fetch_target_to_stage(
    source: FullMarketSource,
    job: MarketSyncJob,
    stage_dir: Path,
    rate_gate: ProcessRateGate | None = None,
) -> dict[str, object]:
    last_error: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            if rate_gate is not None:
                rate_gate.wait("eastmoney")
            frame = source.get_daily_data_for_asset(
                job.target.symbol,
                job.start_date,
                job.end_date,
                job.target.asset_type,
            )
            normalized = _standardise_result(frame, job.target)
            requested_start = date.fromisoformat(job.start_date)
            requested_end = date.fromisoformat(job.end_date)
            normalized = normalized.loc[
                (normalized["date"] >= requested_start) & (normalized["date"] <= requested_end)
            ].reset_index(drop=True)
            if normalized.empty:
                raise ValueError("数据源未返回请求区间内的有效日线")
            if rate_gate is not None:
                rate_gate.report(provider="eastmoney", success=True)
            path = stage_dir / f"{uuid.uuid4().hex}.json"
            _write_stage(path, normalized)
            return {
                "success": True,
                "symbol": job.target.symbol,
                "name": job.target.name,
                "asset_type": job.target.asset_type,
                "provider": str(getattr(source, "last_provider", "eastmoney")),
                "requested_start": job.start_date,
                "requested_end": job.end_date,
                "actual_start": str(normalized["date"].min()),
                "actual_end": str(normalized["date"].max()),
                "rows": len(normalized),
                "retries": attempt - 1,
                "stage_path": str(path),
            }
        except Exception as exc:  # noqa: BLE001 - retry provider boundaries
            last_error = exc
            if rate_gate is not None:
                rate_gate.report(provider="eastmoney", success=False)
            if attempt < _MAX_ATTEMPTS:
                time.sleep(0.25 * (2 ** (attempt - 1)))
    return {
        "success": False,
        "symbol": job.target.symbol,
        "name": job.target.name,
        "asset_type": job.target.asset_type,
        "provider": "eastmoney",
        "requested_start": job.start_date,
        "requested_end": job.end_date,
        "retries": _MAX_ATTEMPTS - 1,
        "error": str(last_error or "同步失败")[:500],
    }


def _fetch_target_to_stage_process(
    job_payload: tuple[MarketSyncJob, str, Any, Any],
) -> dict[str, object]:
    job, stage_dir, rate_state, rate_lock = job_payload
    return _fetch_target_to_stage(
        OnlineFullMarketSource(),
        job,
        Path(stage_dir),
        ProcessRateGate(rate_state, rate_lock),
    )


def _terminate_process_pool(executor: ProcessPoolExecutor, futures: Iterable[Any]) -> None:
    for future in futures:
        future.cancel()
    processes = list(getattr(executor, "_processes", {}).values())
    for process in processes:
        try:
            if process.is_alive():
                process.terminate()
        except (AttributeError, OSError, RuntimeError, ValueError):
            continue
    executor.shutdown(wait=False, cancel_futures=True)
    for process in processes:
        try:
            process.join(timeout=1)
        except (AttributeError, OSError, RuntimeError, ValueError):
            continue


def _emit_detail(callback: DetailCallback | None, detail: Mapping[str, object]) -> None:
    if callback is not None:
        callback(dict(detail))


class FullMarketSync:
    """Coordinate catalog discovery, bounded fetches, and single-writer merges."""

    def __init__(
        self,
        storage: FullMarketStorage,
        source: FullMarketSource | None = None,
        *,
        temp_root: str | Path | None = None,
    ) -> None:
        self.storage = storage
        self.source = source or OnlineFullMarketSource()
        self._use_process_pool = source is None
        self.temp_root = Path(temp_root).expanduser() if temp_root is not None else None

    def run(
        self,
        *,
        start_date: object = None,
        end_date: object = None,
        initial_start_date: object = None,
        workers: int | None = None,
        progress_callback: ProgressCallback | None = None,
        detail_callback: DetailCallback | None = None,
        abort_check: AbortCheck | None = None,
    ) -> dict[str, object]:
        started = time.monotonic()
        normalized_start = _parse_date(start_date, default=_DEFAULT_START_DATE)
        normalized_end = _parse_date(end_date, default=date.today())
        if normalized_start > normalized_end:
            raise ValueError("同步开始日期不能晚于结束日期")
        normalized_initial_start = (
            max(normalized_start, _parse_date(initial_start_date, default=normalized_start))
            if initial_start_date is not None and str(initial_start_date).strip()
            else None
        )
        if normalized_initial_start is not None and normalized_initial_start > normalized_end:
            raise ValueError("首次补齐开始日期不能晚于同步结束日期")
        if abort_check is not None and abort_check():
            raise MarketSyncCancelledError("全市场行情同步已取消")

        _emit_detail(
            detail_callback,
            {
                "phase": "instrument_discovery",
                "current_status": "fetching",
                "range_label": "正在发现全市场资产目录",
            },
        )
        targets, catalog_counts = _discover_catalog(self.source, detail_callback=detail_callback)
        if not targets:
            raise ValueError("全市场资产目录为空，无法开始同步")
        catalog_frame = pd.DataFrame(
            [
                {"symbol": item.symbol, "name": item.name, "type": item.asset_type}
                for item in targets
            ]
        )
        self.storage.upsert_asset_info(catalog_frame)

        jobs: list[MarketSyncJob] = []
        skipped = 0
        for target in targets:
            if abort_check is not None and abort_check():
                raise MarketSyncCancelledError("全市场行情同步已取消")
            ranges = target_ranges(
                self.storage,
                target,
                normalized_start,
                normalized_end,
                initial_start=normalized_initial_start,
            )
            if not ranges:
                skipped += 1
            jobs.extend(
                MarketSyncJob(target, range_start.isoformat(), range_end.isoformat())
                for range_start, range_end in ranges
            )

        if workers is not None and (isinstance(workers, bool) or workers < 1):
            raise ValueError("全市场同步并发数必须是正整数")
        if workers is not None:
            requested_workers = max(1, min(int(workers), _DEFAULT_MAX_WORKERS))
            effective_workers = min(
                requested_workers,
                resolve_market_sync_workers(cpu_count=requested_workers),
            )
        else:
            effective_workers = resolve_market_sync_workers()
        if jobs:
            effective_workers = min(effective_workers, len(jobs))
        report: dict[str, object] = {
            "success": True,
            "operation": "full_market_sync",
            "source": "online",
            "provider": "eastmoney",
            "requested_start": normalized_start.isoformat(),
            "requested_end": normalized_end.isoformat(),
            "catalog_counts": catalog_counts,
            "total_targets": len(targets),
            "planned_jobs": len(jobs),
            "worker_count": effective_workers,
            "execution_mode": (
                "process_pool_staging"
                if self._use_process_pool and effective_workers > 1
                else "direct"
            ),
            "completed": 0,
            "skipped": skipped,
            "failed": 0,
            "errors": 0,
            "processed": 0,
            "updated_files": 0,
            "unchanged_files": skipped,
            "written_bars": 0,
            "rows": 0,
            "updated_symbols": [],
            "failures": [],
            "actual_start": None,
            "actual_end": None,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        if progress_callback is not None:
            progress_callback(0, len(jobs))
        if not jobs:
            report["duration_seconds"] = max(0.0, time.monotonic() - started)
            report["finished_at"] = datetime.now(timezone.utc).isoformat()
            self._save_summary(report)
            return report

        if self.temp_root is not None:
            self.temp_root.mkdir(parents=True, exist_ok=True)
        stage_dir = Path(
            tempfile.mkdtemp(
                prefix="indicator-lab-market-sync-",
                dir=str(self.temp_root) if self.temp_root else None,
            )
        )
        try:
            _emit_detail(detail_callback, {"phase": "fetching", "current_status": "fetching"})
            if self._use_process_pool and effective_workers > 1:
                self._run_process_pool(
                    jobs,
                    stage_dir,
                    effective_workers,
                    report,
                    progress_callback,
                    detail_callback,
                    abort_check,
                )
            else:
                for job in jobs:
                    if abort_check is not None and abort_check():
                        raise MarketSyncCancelledError("全市场行情同步已取消")
                    result = _fetch_target_to_stage(self.source, job, stage_dir)
                    self._merge_result(
                        result, report, progress_callback, detail_callback, abort_check
                    )
            report["duration_seconds"] = max(0.0, time.monotonic() - started)
            report["success"] = _int_value(report["failed"]) == 0
            report["finished_at"] = datetime.now(timezone.utc).isoformat()
            self._save_summary(report)
            return report
        except Exception:
            report["duration_seconds"] = max(0.0, time.monotonic() - started)
            report["success"] = False
            report["finished_at"] = datetime.now(timezone.utc).isoformat()
            self._save_summary(report)
            raise
        finally:
            shutil.rmtree(stage_dir, ignore_errors=True)

    def _run_process_pool(
        self,
        jobs: list[MarketSyncJob],
        stage_dir: Path,
        worker_count: int,
        report: dict[str, object],
        progress_callback: ProgressCallback | None,
        detail_callback: DetailCallback | None,
        abort_check: AbortCheck | None,
    ) -> None:
        context = multiprocessing.get_context("spawn")
        executor = ProcessPoolExecutor(max_workers=worker_count, mp_context=context)
        manager = multiprocessing.Manager()
        rate_state, rate_lock = create_rate_state(manager)
        iterator = iter(jobs)
        pending: dict[Any, MarketSyncJob] = {}
        for _ in range(min(worker_count, len(jobs))):
            job = next(iterator, None)
            if job is None:
                break
            pending[
                executor.submit(
                    _fetch_target_to_stage_process,
                    (job, str(stage_dir), rate_state, rate_lock),
                )
            ] = job
        cancelled = False
        try:
            while pending:
                if abort_check is not None and abort_check():
                    raise MarketSyncCancelledError("全市场行情同步已取消")
                done, _ = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
                for future in done:
                    job = pending.pop(future)
                    try:
                        result = cast(dict[str, object], future.result())
                    except Exception as exc:  # noqa: BLE001 - isolate one worker
                        result = {
                            "success": False,
                            "symbol": job.target.symbol,
                            "name": job.target.name,
                            "asset_type": job.target.asset_type,
                            "provider": "eastmoney",
                            "requested_start": job.start_date,
                            "requested_end": job.end_date,
                            "retries": 0,
                            "error": str(exc)[:500] or "worker 执行失败",
                        }
                    self._merge_result(
                        result, report, progress_callback, detail_callback, abort_check
                    )
                    next_job = next(iterator, None)
                    if next_job is not None:
                        pending[
                            executor.submit(
                                _fetch_target_to_stage_process,
                                (next_job, str(stage_dir), rate_state, rate_lock),
                            )
                        ] = next_job
        except BaseException:
            cancelled = True
            raise
        finally:
            if cancelled:
                _terminate_process_pool(executor, pending)
            else:
                executor.shutdown(wait=True)
            manager.shutdown()

    def _merge_result(
        self,
        result: Mapping[str, object],
        report: dict[str, object],
        progress_callback: ProgressCallback | None,
        detail_callback: DetailCallback | None,
        abort_check: AbortCheck | None,
    ) -> None:
        if abort_check is not None and abort_check():
            raise MarketSyncCancelledError("全市场行情同步已取消")
        if not bool(result.get("success")):
            report["failed"] = _int_value(report["failed"]) + 1
            report["errors"] = _int_value(report["failed"])
            report["processed"] = _int_value(report["processed"]) + 1
            failures = cast(list[dict[str, object]], report["failures"])
            failures.append(
                {
                    "symbol": result.get("symbol"),
                    "asset_type": result.get("asset_type"),
                    "message": result.get("error", "同步失败"),
                    "retries": result.get("retries", 0),
                }
            )
        else:
            stage_path = Path(str(result["stage_path"]))
            frame = _read_stage(stage_path)
            self.storage.write_daily_bars(frame)
            stage_path.unlink(missing_ok=True)
            report["completed"] = _int_value(report["completed"]) + 1
            rows = _int_value(result.get("rows"))
            report["rows"] = _int_value(report["rows"]) + rows
            report["written_bars"] = _int_value(report["written_bars"]) + rows
            report["processed"] = _int_value(report["processed"]) + 1
            report["updated_files"] = _int_value(report["updated_files"]) + 1
            updated_symbols = cast(list[str], report["updated_symbols"])
            symbol = str(result.get("symbol") or "")
            if symbol and symbol not in updated_symbols:
                updated_symbols.append(symbol)
            report["actual_start"] = _min_date(
                report.get("actual_start"), result.get("actual_start")
            )
            report["actual_end"] = _max_date(report.get("actual_end"), result.get("actual_end"))
        processed = _int_value(report["completed"]) + _int_value(report["failed"])
        total = max(1, _int_value(report["planned_jobs"]))
        if progress_callback is not None:
            progress_callback(processed, total)
        _emit_detail(
            detail_callback,
            {
                "phase": "merging",
                "current_status": "updated" if result.get("success") else "error",
                "current_instrument": {
                    "symbol": result.get("symbol"),
                    "name": result.get("name"),
                    "asset_type": result.get("asset_type"),
                },
                "requested_start": result.get("requested_start"),
                "requested_end": result.get("requested_end"),
                "actual_start": result.get("actual_start"),
                "actual_end": result.get("actual_end"),
                "provider": result.get("provider"),
                "retry": result.get("retries", 0),
                "error": result.get("error"),
                "completed": report["completed"],
                "skipped": report["skipped"],
                "failed": report["failed"],
                "planned_jobs": report["planned_jobs"],
                "total_targets": report["total_targets"],
                "worker_count": report["worker_count"],
                "execution_mode": report["execution_mode"],
            },
        )

    def _save_summary(self, report: Mapping[str, object]) -> None:
        persisted = dict(report)
        persisted.pop("updated_symbols", None)
        self.storage.save_system_setting(
            "market_sync_last_summary",
            json.dumps(persisted, ensure_ascii=False, separators=(",", ":"), default=str),
        )


def _min_date(current: object, candidate: object) -> str | None:
    if not candidate:
        return str(current) if current else None
    if not current:
        return str(candidate)
    return min(str(current), str(candidate))


def _max_date(current: object, candidate: object) -> str | None:
    if not candidate:
        return str(current) if current else None
    if not current:
        return str(candidate)
    return max(str(current), str(candidate))


def _int_value(value: object) -> int:
    try:
        return int(str(value or 0))
    except (TypeError, ValueError):
        return 0


__all__ = [
    "FullMarketSync",
    "FullMarketSource",
    "MarketSyncCancelledError",
    "MarketSyncJob",
    "MarketSyncTarget",
    "resolve_market_sync_workers",
    "resolve_sync_range",
    "target_ranges",
]
