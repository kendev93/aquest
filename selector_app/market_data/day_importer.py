"""Incremental local ``.day`` to DuckDB ingestion."""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import pandas as pd

from selector_app.adapters.market_sync import AbortCheck, MarketDataCancelledError
from selector_app.concurrency import MAX_WORKERS

from .day_format import (
    DayFileError,
    DayFileResult,
    classify_board,
    classify_instrument,
    discover_day_files,
    read_day_file,
)
from .models import ImportReport, InstrumentBoard, InstrumentType
from .scope import InstrumentScope
from .store import DuckDbMarketDataStore

ProgressCallback = Callable[[int, int], None]
DetailCallback = Callable[[Mapping[str, object]], None]
_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class _ImportCandidate:
    index: int
    path: Path
    relative_path: str
    market: str
    code: str
    instrument_type: InstrumentType
    board: InstrumentBoard
    old: Mapping[str, object] | None
    needs_finalize: bool


@dataclass(frozen=True)
class _PreparedImport:
    candidate: _ImportCandidate
    size: int
    mtime_ns: int
    checksum: str | None
    status: Literal["unchanged", "ready", "failed"]
    parsed: DayFileResult | None = None
    error: str | None = None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class LocalDayImporter:
    """Import local files without ever writing to the user's vipdoc tree."""

    def __init__(
        self,
        store: DuckDbMarketDataStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(_SHANGHAI_TZ))

    def import_vipdoc(
        self,
        vipdoc_path: str | Path,
        *,
        universe: str = "all",
        instrument_types: Iterable[InstrumentType] | None = None,
        boards: Iterable[InstrumentBoard] | None = None,
        progress_callback: ProgressCallback | None = None,
        detail_callback: DetailCallback | None = None,
        abort_check: AbortCheck | None = None,
    ) -> ImportReport:
        if abort_check is not None and abort_check():
            raise MarketDataCancelledError("行情导入已取消")
        root = Path(vipdoc_path).expanduser().resolve()
        files = discover_day_files(root, universe)
        if detail_callback is not None:
            detail_callback(
                {
                    "phase": "local_import",
                    "current_status": "fetching",
                    "range_label": f"正在检查 {len(files)} 个本地文件…",
                }
            )
        normalized_universe = universe.strip().lower()
        scanned_markets = {"sh", "sz"} if normalized_universe == "all" else {normalized_universe}
        scope = InstrumentScope.from_values(
            universe=universe,
            instrument_types=instrument_types,
            boards=boards,
        )
        previous: dict[str, Mapping[str, object]] = {
            str(item["relative_path"]): item for item in self._store.source_files(root)
        }
        failure_reasons: Counter[str] = Counter()
        imported_files = 0
        updated_files = 0
        unchanged_files = 0
        skipped_files = 0
        filtered_files = 0
        imported_instruments = 0
        replaced_instruments = 0
        imported_bars = 0
        provisional_bars = 0
        errors = 0
        seen: set[str] = set()
        progress_count = 0
        names_by_key: dict[tuple[str, str], str | None] = (
            {
                (ref.market, ref.code): ref.name
                for ref in self._store.list_instruments(include_missing=True)
            }
            if detail_callback is not None
            else {}
        )

        def report_detail(
            candidate: _ImportCandidate,
            *,
            status: str,
            parsed: DayFileResult | None = None,
            error: str | None = None,
            range_label: str | None = None,
        ) -> None:
            if detail_callback is None:
                return
            detail: dict[str, object] = {
                "phase": "local_import",
                "current_status": status,
                "current_instrument": {
                    "market": candidate.market,
                    "code": candidate.code,
                    "name": names_by_key.get((candidate.market, candidate.code)),
                },
                "source_path": candidate.relative_path,
            }
            if parsed is not None:
                range_start, range_end = _frame_date_range(parsed.frame)
                if range_start is not None:
                    detail["range_start"] = range_start
                if range_end is not None:
                    detail["range_end"] = range_end
                detail["bar_count"] = len(parsed.frame)
            if range_label is not None:
                detail["range_label"] = range_label
            if error is not None:
                detail["error"] = error
            detail_callback(detail)

        def report_progress() -> None:
            nonlocal progress_count
            progress_count += 1
            self._report_progress(progress_callback, progress_count, len(files))

        candidates: list[_ImportCandidate] = []
        current = self._clock()
        after_market_close = (current.hour, current.minute) >= (15, 5)
        provisional_symbols = (
            self._store.list_provisional_symbols() if after_market_close else set()
        )

        for index, path in enumerate(files, start=1):
            if abort_check is not None and abort_check():
                raise MarketDataCancelledError("行情导入已取消")
            relative_path = path.relative_to(root).as_posix()
            seen.add(relative_path)
            market = path.name[:2].upper()
            code = path.stem[2:]
            instrument_type = classify_instrument(market, code)
            stat = path.stat()
            board = classify_board(market, code)
            if instrument_type is None or board is None:
                skipped_files += 1
                failure_reasons["unknown_instrument"] += 1
                self._store.record_source_file(
                    source_root=root,
                    relative_path=relative_path,
                    market=market,
                    code=code,
                    instrument_type=None,
                    size=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                    sha256=None,
                    status="skipped",
                    error="unknown_instrument",
                )
                report_progress()
                continue
            if not scope.matches(market, instrument_type, board):
                skipped_files += 1
                filtered_files += 1
                failure_reasons["scope_filtered"] += 1
                report_progress()
                continue
            old = previous.get(relative_path)
            needs_finalize = old is not None and (market, code) in provisional_symbols
            candidates.append(
                _ImportCandidate(
                    index=index,
                    path=path,
                    relative_path=relative_path,
                    market=market,
                    code=code,
                    instrument_type=instrument_type,
                    board=board,
                    old=old,
                    needs_finalize=needs_finalize,
                )
            )

        if candidates:
            with ThreadPoolExecutor(
                max_workers=min(MAX_WORKERS, len(candidates)),
                thread_name_prefix="day-import",
            ) as executor:
                futures = {
                    executor.submit(self._prepare_import, candidate, current): candidate
                    for candidate in candidates
                }
                for future in as_completed(futures):
                    if abort_check is not None and abort_check():
                        raise MarketDataCancelledError("行情导入已取消")
                    prepared = future.result()
                    candidate = prepared.candidate
                    old = candidate.old
                    if prepared.status == "unchanged":
                        unchanged_files += 1
                        if prepared.checksum is not None:
                            self._store.record_source_file(
                                source_root=root,
                                relative_path=candidate.relative_path,
                                market=candidate.market,
                                code=candidate.code,
                                instrument_type=candidate.instrument_type,
                                size=prepared.size,
                                mtime_ns=prepared.mtime_ns,
                                sha256=prepared.checksum,
                                status="imported",
                            )
                        report_detail(candidate, status="unchanged", range_label="已有数据未变化")
                        report_progress()
                        continue
                    if prepared.status == "failed":
                        errors += 1
                        reason = prepared.error or "日线文件解析失败"
                        failure_reasons[reason] += 1
                        self._store.record_source_file(
                            source_root=root,
                            relative_path=candidate.relative_path,
                            market=candidate.market,
                            code=candidate.code,
                            instrument_type=candidate.instrument_type,
                            size=prepared.size,
                            mtime_ns=prepared.mtime_ns,
                            sha256=prepared.checksum,
                            status="failed",
                            error=reason,
                        )
                        report_detail(candidate, status="error", error=reason)
                        report_progress()
                        continue

                    assert prepared.parsed is not None
                    try:
                        count = self._store.replace_local_bars(
                            candidate.market,
                            candidate.code,
                            candidate.instrument_type,
                            prepared.parsed.frame,
                            source_path=str(candidate.path),
                            board=candidate.board,
                        )
                    except (OSError, ValueError) as exc:
                        errors += 1
                        reason = str(exc).strip()[:200] or type(exc).__name__
                        failure_reasons[reason] += 1
                        self._store.record_source_file(
                            source_root=root,
                            relative_path=candidate.relative_path,
                            market=candidate.market,
                            code=candidate.code,
                            instrument_type=candidate.instrument_type,
                            size=prepared.size,
                            mtime_ns=prepared.mtime_ns,
                            sha256=prepared.checksum,
                            status="failed",
                            error=reason,
                        )
                        report_detail(candidate, status="error", error=reason)
                        report_progress()
                        continue

                    if old is None:
                        imported_files += 1
                        imported_instruments += 1
                    else:
                        updated_files += 1
                        replaced_instruments += 1
                    imported_bars += count
                    provisional_bars += int(
                        (prepared.parsed.frame["bar_status"] == "provisional").sum()
                    )
                    self._store.record_source_file(
                        source_root=root,
                        relative_path=candidate.relative_path,
                        market=candidate.market,
                        code=candidate.code,
                        instrument_type=candidate.instrument_type,
                        size=prepared.size,
                        mtime_ns=prepared.mtime_ns,
                        sha256=prepared.checksum,
                        status="imported",
                        imported=True,
                    )
                    report_detail(
                        candidate,
                        status="imported" if old is None else "updated",
                        parsed=prepared.parsed,
                    )
                    report_progress()

        missing_files = 0
        for relative_path in previous:
            if abort_check is not None and abort_check():
                raise MarketDataCancelledError("行情导入已取消")
            relative_market = relative_path.split("/", 1)[0].lower()
            if (
                relative_market in scanned_markets
                and relative_path not in seen
                and previous[relative_path]["status"] != "missing"
            ):
                self._store.mark_source_missing(root, relative_path)
                missing_files += 1
        self._store.set_meta("last_local_import_at", self._clock().isoformat())
        self._report_progress(progress_callback, len(files), len(files))
        return ImportReport(
            source="local",
            discovered_files=len(files),
            imported_files=imported_files,
            updated_files=updated_files,
            unchanged_files=unchanged_files,
            skipped_files=skipped_files,
            filtered_files=filtered_files,
            missing_files=missing_files,
            imported_instruments=imported_instruments,
            replaced_instruments=replaced_instruments,
            imported_bars=imported_bars,
            provisional_bars=provisional_bars,
            errors=errors,
            failure_reasons=dict(failure_reasons),
        )

    def _prepare_import(
        self,
        candidate: _ImportCandidate,
        current: datetime,
    ) -> _PreparedImport:
        stat = candidate.path.stat()
        old = candidate.old
        if (
            old is not None
            and old["status"] == "imported"
            and int(str(old["size"])) == stat.st_size
            and int(str(old["mtime_ns"])) == stat.st_mtime_ns
            and not candidate.needs_finalize
        ):
            return _PreparedImport(
                candidate=candidate,
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                checksum=None,
                status="unchanged",
            )

        checksum = _sha256(candidate.path)
        if (
            old is not None
            and old.get("sha256") == checksum
            and old["status"] == "imported"
            and not candidate.needs_finalize
        ):
            return _PreparedImport(
                candidate=candidate,
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                checksum=checksum,
                status="unchanged",
            )

        try:
            parsed = read_day_file(candidate.path, now=current)
            if parsed.frame.empty:
                raise DayFileError("日线文件没有完整记录")
        except (OSError, ValueError) as exc:
            reason = str(exc).strip()[:200] or type(exc).__name__
            return _PreparedImport(
                candidate=candidate,
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                checksum=checksum,
                status="failed",
                error=reason,
            )
        return _PreparedImport(
            candidate=candidate,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            checksum=checksum,
            status="ready",
            parsed=parsed,
        )

    @staticmethod
    def _report_progress(callback: ProgressCallback | None, current: int, total: int) -> None:
        if callback is not None:
            callback(current, total)


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
