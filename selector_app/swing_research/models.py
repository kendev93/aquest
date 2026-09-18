"""Configuration for swing segmentation studies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from selector_app.market_data.models import InstrumentBoard, InstrumentType

from .segments import ATR_WINDOW, DEFAULT_MIN_BARS

#: Reference thresholds swept by default. They are deliberately spread around
#: the 3-ATR default: a leg that only survives at 2 ATR is usually noise, one
#: that only shows up at 4 ATR is rare enough to be hard to study.
REFERENCE_MULTIPLES: tuple[float, ...] = (2.0, 3.0, 4.0)


@dataclass(frozen=True)
class SwingOverviewConfig:
    """One request to describe how a market moves, before scoring indicators.

    The study deliberately sweeps several ``reversal_multiples`` in a single
    pass. Picking a threshold from a single run is guesswork; seeing how the
    segment count and the median travel move together is not.
    """

    universe: str = "all"
    instrument_types: tuple[InstrumentType, ...] = ("stock",)
    boards: tuple[InstrumentBoard, ...] = ()
    vipdoc_path: str = ""
    start_date: date | None = None
    end_date: date | None = None
    reversal_multiples: tuple[float, ...] = REFERENCE_MULTIPLES
    min_bars: int = DEFAULT_MIN_BARS
    atr_window: int = ATR_WINDOW
    include_pending: bool = False
    batch_size: int = 400
    limit_stocks: int | None = None

    def normalized(self) -> SwingOverviewConfig:
        """Validate and de-duplicate, keeping the caller's ordering."""

        multiples = tuple(
            dict.fromkeys(float(item) for item in self.reversal_multiples if float(item) > 0)
        )
        if not multiples:
            raise ValueError("至少要提供一个大于 0 的反转阈值倍数")
        if self.min_bars < 0:
            raise ValueError("最小持续天数不能为负数")
        if self.atr_window < 1:
            raise ValueError("ATR 窗口必须大于 0")
        if self.batch_size < 1:
            raise ValueError("分批读取的股票数必须大于 0")
        if self.limit_stocks is not None and self.limit_stocks < 1:
            raise ValueError("股票数量上限必须大于 0")
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("开始日期不能晚于结束日期")
        return SwingOverviewConfig(
            universe=self.universe,
            instrument_types=tuple(self.instrument_types) or ("stock",),
            boards=tuple(self.boards),
            vipdoc_path=self.vipdoc_path,
            start_date=self.start_date,
            end_date=self.end_date,
            reversal_multiples=multiples,
            min_bars=self.min_bars,
            atr_window=self.atr_window,
            include_pending=self.include_pending,
            batch_size=self.batch_size,
            limit_stocks=self.limit_stocks,
        )


__all__ = ["REFERENCE_MULTIPLES", "SwingOverviewConfig"]
