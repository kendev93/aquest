"""Batch-import TongDaXin ``.day`` files into the local DuckDB store.

The online sync path owns discovery and incremental updates; this module is the
counterpart for a full local refresh: the user downloads the daily files from
TongDaXin, points the app at the ``vipdoc`` tree, and every parsed bar overwrites
whatever already sits on the same ``(market, code, trade_date)`` key.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from ..concurrency import MAX_WORKERS, visible_cpu_count
from .day_format import classify_board, classify_instrument, discover_day_files
from .local_adapter import LocalDayMarketDataAdapter
from .models import MarketCode, StockRef
from .scope import InstrumentScope

LocalImportProgress = Callable[[int, int], None]
LocalImportDetail = Callable[[dict[str, object]], None]
LocalImportAbortCheck = Callable[[], bool]

_WRITE_BATCH_SIZE = 64


class LocalDayImportError(ValueError):
    """Raised when a local import cannot be started safely."""


class LocalDayImportCancelled(RuntimeError):
    """Raised when a batch import is cancelled cooperatively."""


@dataclass(frozen=True)
class _ParsedFile:
    ref: StockRef
    frame: pd.DataFrame


def _frame_date_range(frame: pd.DataFrame) -> tuple[str | None, str | None]:
    if frame.empty or "date" not in frame.columns:
        return None, None
    dates = pd.to_datetime(frame["date"], errors="coerce").dropna()
    if dates.empty:
        return None, None
    return str(pd.Timestamp(dates.iloc[0]).date()), str(pd.Timestamp(dates.iloc[-1]).date())


def _merge_range(
    start: str | None,
    end: str | None,
    candidate_start: str | None,
    candidate_end: str | None,
) -> tuple[str | None, str | None]:
    starts = [str(value) for value in (start, candidate_start) if value]
    ends = [str(value) for value in (end, candidate_end) if value]
    return (min(starts) if starts else None, max(ends) if ends else None)


class LocalDayImportService:
    """Parse local day files concurrently and write them through the store."""

    def __init__(
        self,
        store: Any,
        *,
        adapter: LocalDayMarketDataAdapter | None = None,
        max_workers: int | None = None,
    ) -> None:
        self._store = store
        self._adapter = adapter or LocalDayMarketDataAdapter()
        self._max_workers = max(1, max_workers or min(MAX_WORKERS, visible_cpu_count()))

    def import_vipdoc(
        self,
        vipdoc_path: str | Path,
        *,
        universe: str = "all",
        instrument_types: Iterable[str] | None = None,
        boards: Iterable[str] | None = None,
        progress_callback: LocalImportProgress | None = None,
        detail_callback: LocalImportDetail | None = None,
        abort_check: LocalImportAbortCheck | None = None,
    ) -> dict[str, Any]:
        """Import every matching day file, overwriting bars on the same date."""

        self._check_aborted(abort_check)
        root = Path(vipdoc_path).expanduser()
        if not root.is_dir():
            raise LocalDayImportError(
                f"行情目录不存在或后端看不到: {root}。"
                "后端运行在 Docker 容器里，只能访问已挂载进容器的路径，"
                "直接填 mac/Windows 上的目录是找不到的；"
                "请先把宿主机目录挂载进容器（推荐挂到 /data/vipdoc，或把上级目录挂到 /host），"
                "再填容器内路径。"
            )
        scope = InstrumentScope.from_values(
            universe=universe,
            instrument_types=instrument_types,
            boards=boards,
        )
        discovered = discover_day_files(root, universe)
        candidates = _select_refs(discovered, scope)
        missing = [ref for ref in candidates if not ref.path.is_file()]
        refs = [ref for ref in candidates if ref.path.is_file()]
        report: dict[str, Any] = {
            "source": "local",
            "provider": "tdx",
            "vipdoc_path": str(root),
            "discovered_files": len(discovered),
            "selected_files": len(candidates),
            "filtered_files": len(discovered) - len(candidates),
            "imported_instruments": 0,
            "imported_bars": 0,
            "replaced_bars": 0,
            "skipped_files": 0,
            "missing_files": len(missing),
            "errors": 0,
            "failure_reasons": {},
            "range_start": None,
            "range_end": None,
        }
        failures: Counter[str] = Counter()
        for ref in missing:
            failures["day_file_missing"] += 1
        report["errors"] = len(missing)
        progress_count = 0

        def report_progress() -> None:
            nonlocal progress_count
            progress_count += 1
            if progress_callback is not None:
                progress_callback(progress_count, len(candidates))

        def report_detail(
            ref: StockRef,
            status: str,
            *,
            bars: int = 0,
            error: str | None = None,
        ) -> None:
            if detail_callback is None:
                return
            detail: dict[str, object] = {
                "phase": "local_import",
                "current_status": status,
                "current_instrument": {"market": ref.market, "code": ref.code},
                "source_path": str(ref.path),
                "bar_count": bars,
            }
            if error:
                detail["error"] = error
            detail_callback(detail)

        pending_frames: list[pd.DataFrame] = []
        pending_refs: list[StockRef] = []
        imported_instruments = 0
        batch_counts: list[tuple[StockRef, int]] = []

        def flush() -> None:
            nonlocal imported_instruments
            if not pending_frames:
                return
            combined = pd.concat(pending_frames, ignore_index=True)
            written, replaced = self._store.upsert_local_bars(combined)
            report["imported_bars"] = int(report["imported_bars"]) + written
            report["replaced_bars"] = int(report["replaced_bars"]) + replaced
            for ref, bars in batch_counts:
                report_detail(ref, "imported", bars=bars)
            imported_instruments += len(batch_counts)
            pending_frames.clear()
            pending_refs.clear()
            batch_counts.clear()

        if refs:
            with ThreadPoolExecutor(
                max_workers=min(self._max_workers, len(refs)),
                thread_name_prefix="local-day-import",
            ) as executor:
                futures = {executor.submit(self._parse, ref): ref for ref in refs}
                for future in as_completed(futures):
                    self._check_aborted(abort_check)
                    ref = futures[future]
                    try:
                        parsed = future.result()
                    except (OSError, ValueError) as exc:
                        reason = str(exc).strip()[:200] or type(exc).__name__
                        failures[reason] += 1
                        report["errors"] = int(report["errors"]) + 1
                        report["skipped_files"] = int(report["skipped_files"]) + 1
                        report_detail(ref, "error", error=reason)
                        report_progress()
                        continue
                    if parsed.frame.empty:
                        report["skipped_files"] = int(report["skipped_files"]) + 1
                        failures["empty_day_file"] += 1
                        report_detail(ref, "skipped")
                        report_progress()
                        continue
                    frame = parsed.frame.copy()
                    frame["market"] = ref.market
                    frame["code"] = ref.code
                    frame["instrument_type"] = ref.instrument_type
                    frame["board"] = ref.board
                    pending_frames.append(frame)
                    pending_refs.append(ref)
                    batch_counts.append((ref, len(frame)))
                    start, end = _frame_date_range(frame)
                    report["range_start"], report["range_end"] = _merge_range(
                        report["range_start"], report["range_end"], start, end
                    )
                    if len(pending_frames) >= _WRITE_BATCH_SIZE:
                        flush()
                    report_progress()

        flush()
        report["imported_instruments"] = imported_instruments
        report["failure_reasons"] = dict(failures)
        if progress_callback is not None:
            progress_callback(len(candidates), len(candidates))
        return report

    def _parse(self, ref: StockRef) -> _ParsedFile:
        return _ParsedFile(ref=ref, frame=self._adapter.read_stock(ref))

    @staticmethod
    def _check_aborted(abort_check: LocalImportAbortCheck | None) -> None:
        if abort_check is not None and abort_check():
            raise LocalDayImportCancelled("本地行情导入已取消")


def _select_refs(files: list[Path], scope: InstrumentScope) -> list[StockRef]:
    refs: list[StockRef] = []
    for path in files:
        market: MarketCode = "SH" if path.name[:2].upper() == "SH" else "SZ"
        code = path.stem[2:]
        instrument_type = classify_instrument(market, code)
        board = classify_board(market, code)
        if instrument_type is None or board is None:
            continue
        if not scope.matches(market, instrument_type, board):
            continue
        refs.append(
            StockRef(
                market=market,
                code=code,
                path=path,
                instrument_type=instrument_type,
                board=board,
            )
        )
    return refs


__all__ = [
    "LocalDayImportCancelled",
    "LocalDayImportError",
    "LocalDayImportService",
]
