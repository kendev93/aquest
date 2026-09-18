"""Project-owned compatibility adapter for reading raw local day files."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
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


def suggested_vipdoc_path() -> str:
    configured = os.getenv("SELECTOR_VIPDOC_PATH")
    if configured:
        return str(Path(configured).expanduser())
    return "/data/vipdoc" if Path("/data/vipdoc").is_dir() else str(Path.home() / "vipdoc")


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
    "is_supported_a_stock",
    "is_supported_instrument",
    "suggested_vipdoc_path",
]
