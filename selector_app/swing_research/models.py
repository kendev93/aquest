"""Configuration for swing segmentation studies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from selector_app.market_data.models import InstrumentBoard, InstrumentType

from .labels import ALL_LABELS, DEFAULT_HORIZON, LabelKind
from .scoring import (
    DEFAULT_BLOCK_SIZE,
    DEFAULT_PERMUTATIONS,
    MIN_CROSS_SECTION,
)
from .segments import ATR_WINDOW, DEFAULT_MIN_BARS, DEFAULT_REVERSAL_MULTIPLE

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


#: Rough ceiling on the temporary panel file. A full-market, full-history run
#: would spill several gigabytes into /tmp; refusing early is friendlier than a
#: half-written file and an out-of-space crash ten minutes in.
MAX_PANEL_BYTES = 3 * 1024**3


@dataclass(frozen=True)
class SwingIndicatorConfig:
    """One request to score indicators against swing-derived labels.

    Unlike the overview, this studies a *single* threshold: sweeping several
    here would multiply the multiplicity problem the significance test is
    already struggling with, and the calibration pass already said which one to
    use.
    """

    universe: str = "all"
    instrument_types: tuple[InstrumentType, ...] = ("stock",)
    boards: tuple[InstrumentBoard, ...] = ()
    vipdoc_path: str = ""
    start_date: date | None = None
    end_date: date | None = None
    reversal_multiple: float = DEFAULT_REVERSAL_MULTIPLE
    min_bars: int = DEFAULT_MIN_BARS
    atr_window: int = ATR_WINDOW
    horizon: int = DEFAULT_HORIZON
    labels: tuple[LabelKind, ...] = ALL_LABELS
    value_ids: tuple[str, ...] = ()
    #: Study the continuous outputs of the three custom TDX formulas alongside
    #: the technical ones. On by default: measured on the same labels, they
    #: scored higher than most of the technical family.
    include_custom: bool = True
    min_cross_section: int = MIN_CROSS_SECTION
    permutations: int = DEFAULT_PERMUTATIONS
    block_size: int = DEFAULT_BLOCK_SIZE
    alpha: float = 0.05
    batch_size: int = 200
    limit_stocks: int | None = None

    def normalized(self) -> SwingIndicatorConfig:
        if self.reversal_multiple <= 0:
            raise ValueError("反转阈值倍数必须大于 0")
        if self.min_bars < 0:
            raise ValueError("最小持续天数不能为负数")
        if self.atr_window < 1:
            raise ValueError("ATR 窗口必须大于 0")
        if self.horizon < 1:
            raise ValueError("预警窗口至少要有 1 个交易日")
        if self.min_cross_section < 2:
            raise ValueError("每日横截面样本数至少为 2")
        if self.permutations < 0:
            raise ValueError("噪声门槛检验次数不能为负数")
        if self.block_size < 1:
            raise ValueError("置换块长度至少为 1 个交易日")
        if not 0 < self.alpha < 1:
            raise ValueError("显著性水平必须介于 0 与 1 之间")
        if self.batch_size < 1:
            raise ValueError("分批读取的股票数必须大于 0")
        if self.limit_stocks is not None and self.limit_stocks < 1:
            raise ValueError("股票数量上限必须大于 0")
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("开始日期不能晚于结束日期")
        unknown = tuple(item for item in self.labels if item not in ALL_LABELS)
        if unknown:
            raise ValueError(f"未知的标签类型: {', '.join(unknown)}")
        return SwingIndicatorConfig(
            universe=self.universe,
            instrument_types=tuple(self.instrument_types) or ("stock",),
            boards=tuple(self.boards),
            vipdoc_path=self.vipdoc_path,
            start_date=self.start_date,
            end_date=self.end_date,
            reversal_multiple=float(self.reversal_multiple),
            min_bars=self.min_bars,
            atr_window=self.atr_window,
            horizon=self.horizon,
            labels=tuple(dict.fromkeys(self.labels)) or ALL_LABELS,
            value_ids=tuple(dict.fromkeys(self.value_ids)),
            include_custom=bool(self.include_custom),
            min_cross_section=self.min_cross_section,
            permutations=self.permutations,
            block_size=self.block_size,
            alpha=self.alpha,
            batch_size=self.batch_size,
            limit_stocks=self.limit_stocks,
        )


__all__ = [
    "MAX_PANEL_BYTES",
    "REFERENCE_MULTIPLES",
    "SwingIndicatorConfig",
    "SwingOverviewConfig",
]
