"""Read, aggregate, and enrich DuckDB daily bars for chart views."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from numbers import Real
from pathlib import Path
from typing import Literal, Protocol, cast

import pandas as pd

from selector_app.formulas.registry import FORMULA_REGISTRY, value_display_name
from selector_app.formulas.technical import calculate_technical_indicators

from .adapter import DuckDbMarketDataAdapter
from .models import InstrumentBoard, MarketCode, StockRef
from .store import DuckDbMarketDataStore

ChartPeriod = Literal["daily", "monthly", "yearly"]
MarketScope = Literal["all", "SH", "SZ"]
_MA_WINDOWS = (5, 10, 20, 60)
_REQUIRED_COLUMNS = ("date", "open", "high", "low", "close", "volume", "amount")


class LocalMarketDataAdapter(Protocol):
    def list_stock_refs(
        self,
        vipdoc_path: str | Path,
        universe: str,
        universe_file: str | Path | None = None,
    ) -> list[StockRef]: ...

    def stock_ref(self, vipdoc_path: str | Path, market: MarketCode, code: str) -> StockRef: ...

    def read_stock(self, ref: StockRef) -> pd.DataFrame: ...


@dataclass(frozen=True)
class LocalInstrument:
    market: MarketCode
    code: str
    bars: int
    data_start: str | None
    data_end: str | None
    last_close: float | None
    instrument_type: str = "stock"
    error: str | None = None
    board: InstrumentBoard = "main"
    name: str | None = None
    provider: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "market": self.market,
            "code": self.code,
            "name": self.name,
            "instrument_type": self.instrument_type,
            "board": self.board,
            "provider": self.provider,
            "bars": self.bars,
            "data_start": self.data_start,
            "data_end": self.data_end,
            "last_close": self.last_close,
            "error": self.error,
        }


@dataclass(frozen=True)
class LocalInstrumentPage:
    items: tuple[LocalInstrument, ...]
    total: int
    page: int
    page_size: int
    pages: int

    def to_dict(self) -> dict[str, object]:
        return {
            "items": [item.to_dict() for item in self.items],
            "total": self.total,
            "page": self.page,
            "page_size": self.page_size,
            "pages": self.pages,
        }


@dataclass(frozen=True)
class LocalMarketChart:
    market: MarketCode
    code: str
    period: ChartPeriod
    total_daily_bars: int
    bars: int
    available_data_start: str
    available_data_end: str
    data_start: str
    data_end: str
    candles: tuple[dict[str, object], ...]
    formula_panels: tuple[dict[str, object], ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "market": self.market,
            "code": self.code,
            "period": self.period,
            "total_daily_bars": self.total_daily_bars,
            "bars": self.bars,
            "available_data_start": self.available_data_start,
            "available_data_end": self.available_data_end,
            "data_start": self.data_start,
            "data_end": self.data_end,
            "candles": list(self.candles),
            "formula_panels": list(self.formula_panels),
        }


class LocalMarketDataService:
    """Expose imported DuckDB instruments as summaries and chart-ready data."""

    def __init__(
        self,
        adapter: LocalMarketDataAdapter | None = None,
        store: DuckDbMarketDataStore | None = None,
    ) -> None:
        self._adapter = adapter or DuckDbMarketDataAdapter(store or DuckDbMarketDataStore())

    def list_instruments(
        self,
        vipdoc_path: str | Path,
        *,
        market: MarketScope = "all",
        keyword: str = "",
        page: int = 1,
        page_size: int = 50,
    ) -> LocalInstrumentPage:
        if page < 1:
            raise ValueError("页码必须大于 0")
        if not 1 <= page_size <= 200:
            raise ValueError("每页数量必须在 1 到 200 之间")

        universe = "all" if market == "all" else market.lower()
        refs = self._adapter.list_stock_refs(vipdoc_path, universe)
        query = keyword.strip().upper()
        filtered = [
            ref
            for ref in sorted(refs, key=_instrument_sort_key)
            if not query
            or query in ref.code
            or query in ref.market
            or query in getattr(ref, "instrument_type", "")
        ]
        total = len(filtered)
        start = (page - 1) * page_size
        selected = filtered[start : start + page_size]
        items = tuple(self._summarize_instrument(ref) for ref in selected)
        pages = math.ceil(total / page_size) if total else 0
        return LocalInstrumentPage(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            pages=pages,
        )

    def get_chart(
        self,
        vipdoc_path: str | Path,
        market: MarketCode,
        code: str,
        *,
        period: ChartPeriod = "daily",
        start_date: date | None = None,
        end_date: date | None = None,
        formula_ids: tuple[str, ...] = (),
    ) -> LocalMarketChart:
        ref = self._adapter.stock_ref(vipdoc_path, market, code)
        daily = _normalise_frame(self._adapter.read_stock(ref))
        if daily.empty:
            raise ValueError(f"{market} {code} 没有可用日线数据")

        available_start = _date_text(daily["date"].iloc[0])
        available_end = _date_text(daily["date"].iloc[-1])
        enriched = _add_indicators(_aggregate(daily, period))
        visible = _filter_dates(enriched, start_date=start_date, end_date=end_date)
        if visible.empty:
            raise ValueError("指定日期范围内没有可用行情数据")

        records = cast(list[dict[str, object]], visible.to_dict(orient="records"))
        candles = tuple(_candle(record) for record in records)
        formula_panels = (
            _formula_panels(
                daily,
                formula_ids,
                start_date=start_date,
                end_date=end_date,
            )
            if period == "daily" and formula_ids
            else ()
        )
        return LocalMarketChart(
            market=market,
            code=code,
            period=period,
            total_daily_bars=len(daily),
            bars=len(candles),
            available_data_start=available_start,
            available_data_end=available_end,
            data_start=str(candles[0]["date"]),
            data_end=str(candles[-1]["date"]),
            candles=candles,
            formula_panels=formula_panels,
        )

    def _summarize_instrument(self, ref: StockRef) -> LocalInstrument:
        if ref.bar_count is not None:
            if ref.bar_count <= 0:
                return LocalInstrument(
                    market=ref.market,
                    code=ref.code,
                    name=getattr(ref, "name", None),
                    bars=0,
                    data_start=None,
                    data_end=None,
                    last_close=None,
                    error="没有可用日线数据",
                    instrument_type=getattr(ref, "instrument_type", "stock"),
                    board=ref.board,
                    provider=getattr(ref, "provider", None),
                )
            return LocalInstrument(
                market=ref.market,
                code=ref.code,
                name=getattr(ref, "name", None),
                instrument_type=getattr(ref, "instrument_type", "stock"),
                board=getattr(ref, "board", "main"),
                provider=getattr(ref, "provider", None),
                bars=ref.bar_count,
                data_start=_optional_date_text(ref.first_date),
                data_end=_optional_date_text(ref.last_date),
                last_close=_number(ref.last_close),
            )
        try:
            frame = _normalise_frame(self._adapter.read_stock(ref))
        except (OSError, ValueError) as exc:
            return LocalInstrument(
                market=ref.market,
                code=ref.code,
                name=getattr(ref, "name", None),
                bars=0,
                data_start=None,
                data_end=None,
                last_close=None,
                error=str(exc),
                instrument_type=getattr(ref, "instrument_type", "stock"),
                board=ref.board,
                provider=getattr(ref, "provider", None),
            )
        if frame.empty:
            return LocalInstrument(
                market=ref.market,
                code=ref.code,
                name=getattr(ref, "name", None),
                bars=0,
                data_start=None,
                data_end=None,
                last_close=None,
                error="没有可用日线数据",
                instrument_type=getattr(ref, "instrument_type", "stock"),
                board=ref.board,
                provider=getattr(ref, "provider", None),
            )
        return LocalInstrument(
            market=ref.market,
            code=ref.code,
            name=getattr(ref, "name", None),
            instrument_type=getattr(ref, "instrument_type", "stock"),
            board=getattr(ref, "board", "main"),
            provider=getattr(ref, "provider", None),
            bars=len(frame),
            data_start=_date_text(frame["date"].iloc[0]),
            data_end=_date_text(frame["date"].iloc[-1]),
            last_close=_number(frame["close"].iloc[-1]),
        )


def _normalise_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    missing = [column for column in _REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"行情数据缺少字段: {', '.join(missing)}")
    normalized = frame.loc[:, list(_REQUIRED_COLUMNS)].copy()
    normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce")
    for column in ("open", "high", "low", "close"):
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    for column in ("volume", "amount"):
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce").fillna(0.0)
    normalized = normalized.dropna(subset=["date", "open", "high", "low", "close"])
    return normalized.sort_values("date", kind="stable").reset_index(drop=True)


def _aggregate(frame: pd.DataFrame, period: ChartPeriod) -> pd.DataFrame:
    if period == "daily":
        return frame.copy()
    frequency = "M" if period == "monthly" else "Y"
    buckets = frame["date"].dt.to_period(frequency)
    rows: list[dict[str, object]] = []
    for _, group in frame.groupby(buckets, sort=True):
        rows.append(
            {
                "date": group["date"].iloc[-1],
                "open": group["open"].iloc[0],
                "high": group["high"].max(),
                "low": group["low"].min(),
                "close": group["close"].iloc[-1],
                "volume": group["volume"].sum(),
                "amount": group["amount"].sum(),
            }
        )
    return pd.DataFrame(rows, columns=list(_REQUIRED_COLUMNS))


def _add_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    enriched = frame.copy()
    result = calculate_technical_indicators(enriched)
    for window in _MA_WINDOWS:
        enriched[f"ma_{window}"] = result.values[f"ma{window}"]
    for value_name in ("rsi14", "macd", "macd_signal", "macd_histogram"):
        enriched[value_name] = result.values[value_name]
    return enriched


def _filter_dates(
    frame: pd.DataFrame,
    *,
    start_date: date | None,
    end_date: date | None,
) -> pd.DataFrame:
    visible = frame
    dates = visible["date"].dt.date
    if start_date is not None:
        visible = visible.loc[dates >= start_date]
        dates = visible["date"].dt.date
    if end_date is not None:
        visible = visible.loc[dates <= end_date]
    return visible.reset_index(drop=True)


def _candle(record: Mapping[str, object]) -> dict[str, object]:
    return {
        "date": _date_text(record["date"]),
        "open": _number(record["open"]),
        "high": _number(record["high"]),
        "low": _number(record["low"]),
        "close": _number(record["close"]),
        "volume": _number(record["volume"]),
        "amount": _number(record["amount"]),
        "ma": {f"ma{window}": _number(record.get(f"ma_{window}")) for window in _MA_WINDOWS},
        "rsi14": _number(record.get("rsi14")),
        "macd": _number(record.get("macd")),
        "macd_signal": _number(record.get("macd_signal")),
        "macd_histogram": _number(record.get("macd_histogram")),
    }


def _formula_panels(
    frame: pd.DataFrame,
    formula_ids: tuple[str, ...],
    *,
    start_date: date | None,
    end_date: date | None,
) -> tuple[dict[str, object], ...]:
    dates = pd.to_datetime(frame["date"], errors="coerce")
    mask = pd.Series(True, index=frame.index)
    if start_date is not None:
        mask &= dates.dt.date >= start_date
    if end_date is not None:
        mask &= dates.dt.date <= end_date
    indexes = list(frame.index[mask])
    visible_dates = [_date_text(frame.loc[index, "date"]) for index in indexes]
    panels: list[dict[str, object]] = []
    for formula_id in dict.fromkeys(formula_ids):
        try:
            definition = FORMULA_REGISTRY.formula(formula_id)
        except KeyError as exc:
            raise ValueError(f"不支持的指标面板: {formula_id}") from exc
        result = definition.calculate(frame.copy(deep=True))
        values: list[dict[str, object]] = []
        for value_name in definition.selectable_value_names:
            series = result.values.get(value_name)
            if series is None:
                continue
            visible_values = [_number(series[index]) for index in indexes]
            values.append(
                {
                    "id": f"{formula_id}.{value_name}",
                    "display_name": value_display_name(formula_id, value_name),
                    "values": visible_values,
                    "latest": visible_values[-1] if visible_values else None,
                }
            )
        panels.append(
            {
                "id": formula_id,
                "display_name": definition.display_name,
                "dates": visible_dates,
                "values": values,
            }
        )
    return tuple(panels)


def _date_text(value: object) -> str:
    return f"{pd.Timestamp(value):%Y-%m-%d}"


def _optional_date_text(value: object) -> str | None:
    return None if value is None else _date_text(value)


def _instrument_sort_key(ref: StockRef) -> tuple[int, str, str]:
    bar_count = ref.bar_count
    has_no_bars = bar_count is not None and bar_count <= 0
    return (1 if has_no_bars else 0, ref.market, ref.code)


def _number(value: object) -> float | None:
    if not isinstance(value, (Real, str)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
