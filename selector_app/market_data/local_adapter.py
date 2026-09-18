"""Project-owned compatibility adapter for reading raw local day files."""

from __future__ import annotations

import os
import re
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .adapter import MarketDataAdapter, parse_universe_code
from .day_format import classify_board, classify_instrument, discover_day_files, read_day_file
from .models import InstrumentBoard, InstrumentType, MarketCode, StockRef
from .scope import InstrumentScope

_CODE_PATTERN = re.compile(r"^\d{6}$")
_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

# Where a host directory may be mounted inside the container. ``/host`` is the
# conventional catch-all mount: bind a parent directory of the TongDaXin tree
# there and any path below it becomes reachable without editing compose again.
_DETECT_SCAN_ROOTS: tuple[str, ...] = ("/host", "/data", "/mnt", "/media")
_DETECT_SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "Library",
        "node_modules",
        "venv",
    }
)
_DETECT_MAX_DEPTH = 4
_DETECT_MAX_DIRS = 600


# ``/host`` is where a host directory gets mounted. When the operator also sets
# ``SELECTOR_HOST_MOUNT_PREFIX`` to the matching host path (e.g. ``/Users/me``),
# users may paste their own machine's path and it is rewritten automatically.
_HOST_MOUNT_POINT = "/host"
_HOST_MOUNT_PREFIX_ENV = "SELECTOR_HOST_MOUNT_PREFIX"


def host_mount_prefix() -> str:
    return os.getenv(_HOST_MOUNT_PREFIX_ENV, "").strip().rstrip("/")


def map_host_path(path: str | Path) -> str:
    """Rewrite a host-side path onto the container mount point when possible.

    Paths that already resolve inside the container are returned untouched, so
    both spellings work once the host tree is mounted at ``/host``.
    """

    resolved = Path(path).expanduser()
    if resolved.is_dir():
        return str(resolved)
    prefix = host_mount_prefix()
    candidate_path = Path(str(resolved))
    if prefix and str(candidate_path).startswith(prefix + "/"):
        mapped = Path(_HOST_MOUNT_POINT + str(candidate_path)[len(prefix) :])
        if mapped.is_dir():
            return str(mapped)
    return str(candidate_path)


def suggested_vipdoc_path() -> str:
    configured = os.getenv("SELECTOR_VIPDOC_PATH")
    if configured:
        return str(Path(configured).expanduser())
    return "/data/vipdoc" if Path("/data/vipdoc").is_dir() else str(Path.home() / "vipdoc")


@dataclass(frozen=True)
class VipdocCandidate:
    """A directory that looks like a TongDaXin ``vipdoc`` tree."""

    path: str
    file_count: int
    markets: tuple[str, ...]


def _day_markets(root: Path) -> tuple[str, ...]:
    return tuple(market for market in ("sh", "sz") if (root / market / "lday").is_dir())


def _count_day_files(root: Path, markets: tuple[str, ...]) -> int:
    total = 0
    for market in markets:
        try:
            total += sum(1 for entry in (root / market / "lday").iterdir() if entry.is_file())
        except OSError:
            continue
    return total


def _walk_for_vipdoc(root: Path) -> Iterator[Path]:
    """Yield directories named ``vipdoc`` below ``root`` within a small budget."""

    queue: deque[tuple[Path, int]] = deque([(root, 0)])
    visited = 0
    while queue:
        current, depth = queue.popleft()
        if depth >= _DETECT_MAX_DEPTH or visited >= _DETECT_MAX_DIRS:
            continue
        try:
            children = [child for child in current.iterdir() if child.is_dir()]
        except OSError:
            continue
        for child in children:
            visited += 1
            if child.name.startswith(".") or child.name in _DETECT_SKIP_DIRS:
                continue
            if child.name.lower() == "vipdoc":
                yield child
            else:
                queue.append((child, depth + 1))


def detect_vipdoc_roots(extra_roots: Iterable[str | Path] | None = None) -> list[VipdocCandidate]:
    """Find reachable ``vipdoc`` trees, best (most files) first.

    The backend usually runs inside a container, so host paths are invisible
    unless mounted. This only reports what the process can actually see.
    """

    roots: list[Path] = []
    for value in (suggested_vipdoc_path(), *(extra_roots or ()), *_DETECT_SCAN_ROOTS):
        candidate = Path(value).expanduser()
        if candidate.is_dir() and candidate not in roots:
            roots.append(candidate)
    found: dict[str, VipdocCandidate] = {}
    for root in roots:
        markets = _day_markets(root)
        if markets:
            _record_candidate(found, root, markets)
            continue
        for child in _walk_for_vipdoc(root):
            child_markets = _day_markets(child)
            if child_markets:
                _record_candidate(found, child, child_markets)
    return sorted(found.values(), key=lambda item: (-item.file_count, item.path))


def _record_candidate(
    found: dict[str, VipdocCandidate], path: Path, markets: tuple[str, ...]
) -> None:
    key = str(path)
    if key in found:
        return
    file_count = _count_day_files(path, markets)
    if file_count == 0:
        return  # 目录结构对但没有 .day 文件，多半是挂错了或还没下载
    found[key] = VipdocCandidate(path=key, file_count=file_count, markets=markets)


def is_supported_instrument(market: MarketCode, code: str) -> bool:
    return classify_instrument(market, code) is not None


def is_supported_a_stock(market: MarketCode, code: str) -> bool:
    """Legacy predicate retained for older callers that only scan A shares."""

    normalized = code.strip()
    if not _CODE_PATTERN.fullmatch(normalized):
        return False
    if market == "SH":
        return normalized.startswith(("60", "68"))
    return normalized.startswith(("00", "30"))


def _parse_universe_line(line: str) -> tuple[MarketCode, str] | None:
    try:
        return parse_universe_code(line)
    except ValueError as exc:
        raise ValueError(f"自定义股票列表中的格式无效: {line.strip()}") from exc


class LocalDayMarketDataAdapter:
    """Read normalized bars from a local vipdoc tree without third parties."""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(_SHANGHAI_TZ))

    def _resolve_root(self, vipdoc_path: str | Path | None) -> Path:
        return (
            Path(vipdoc_path).expanduser()
            if vipdoc_path is not None
            else Path(suggested_vipdoc_path())
        )

    def resolve_vipdoc(self, vipdoc_path: str | Path | None) -> Path:
        """Compatibility helper; it only resolves a path and never writes."""

        return self._resolve_root(vipdoc_path)

    @staticmethod
    def _path(root: Path, market: MarketCode, code: str) -> Path:
        return root / market.lower() / "lday" / f"{market.lower()}{code}.day"

    def stock_ref(self, vipdoc_path: str | Path, market: MarketCode, code: str) -> StockRef:
        normalized_code = code.strip()
        instrument_type = classify_instrument(market, normalized_code)
        if instrument_type is None:
            raise ValueError(f"不支持的行情品种: {market} {code}")
        root = self._resolve_root(vipdoc_path)
        return StockRef(
            market=market,
            code=normalized_code,
            path=self._path(root, market, normalized_code),
            instrument_type=instrument_type,
            board=classify_board(market, normalized_code) or "main",
        )

    def list_stock_refs(
        self,
        vipdoc_path: str | Path,
        universe: str,
        universe_file: str | Path | None = None,
        instrument_types: tuple[InstrumentType, ...] | None = None,
        boards: tuple[InstrumentBoard, ...] | None = None,
    ) -> list[StockRef]:
        root = self._resolve_root(vipdoc_path)
        scope = InstrumentScope.from_values(
            universe=universe.lower(),
            instrument_types=instrument_types,
            boards=boards,
        )
        if universe.lower() == "custom":
            if universe_file is None:
                raise ValueError("自定义股票范围必须提供股票列表文件")
            return self._list_custom_refs(
                root,
                Path(universe_file).expanduser(),
                scope=scope,
            )
        files = discover_day_files(root, universe)
        refs: list[StockRef] = []
        for path in files:
            market: MarketCode = "SH" if path.name[:2].upper() == "SH" else "SZ"
            code = path.stem[2:]
            instrument_type = classify_instrument(market, code)
            board = classify_board(market, code)
            if (
                instrument_type is not None
                and board is not None
                and scope.matches(market, instrument_type, board)
            ):
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

    def _list_custom_refs(
        self,
        root: Path,
        universe_file: Path,
        *,
        scope: InstrumentScope,
    ) -> list[StockRef]:
        if not universe_file.is_file():
            raise ValueError(f"自定义股票列表文件不存在: {universe_file}")
        refs: list[StockRef] = []
        seen: set[tuple[MarketCode, str]] = set()
        for line in universe_file.read_text(encoding="utf-8").splitlines():
            parsed = _parse_universe_line(line)
            if parsed is None or parsed in seen:
                continue
            seen.add(parsed)
            market, code = parsed
            instrument_type = classify_instrument(market, code)
            board = classify_board(market, code)
            if (
                instrument_type is not None
                and board is not None
                and scope.matches(market, instrument_type, board)
            ):
                refs.append(
                    StockRef(
                        market=market,
                        code=code,
                        path=self._path(root, market, code),
                        instrument_type=instrument_type,
                        board=board,
                    )
                )
        return refs

    def read_stock(self, ref: StockRef) -> pd.DataFrame:
        parsed = read_day_file(ref.path, now=self._clock())
        frame = parsed.frame
        if frame.empty:
            return frame
        completed = frame.loc[frame["bar_status"] == "completed"].copy()
        completed["instrument_type"] = ref.instrument_type
        return completed.reset_index(drop=True)


__all__ = [
    "LocalDayMarketDataAdapter",
    "MarketCode",
    "MarketDataAdapter",
    "StockRef",
    "VipdocCandidate",
    "detect_vipdoc_roots",
    "host_mount_prefix",
    "is_supported_a_stock",
    "is_supported_instrument",
    "map_host_path",
    "suggested_vipdoc_path",
]
