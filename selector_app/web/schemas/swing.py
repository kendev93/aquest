"""波段研究请求体。"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from selector_app.market_data.models import InstrumentBoard, InstrumentType
from selector_app.swing_research.labels import ALL_LABELS as ALL_SWING_LABELS
from selector_app.swing_research.models import (
    DEFAULT_STOCK_LIMIT,
    DEFAULT_STUDY_PERMUTATIONS,
    SwingIndicatorConfig,
    SwingOverviewConfig,
)


class SwingOverviewRequest(BaseModel):
    """Swing segmentation overview - the calibration step before a study.

    Every field that decides what counts as a leg is exposed, because the
    first run exists to answer "what should these be?" rather than to produce
    a finding. ``reversal_multiples`` takes several thresholds at once so the
    trade-off between how many legs appear and how far they travel is visible
    in a single pass.
    """

    model_config = ConfigDict(extra="forbid")

    vipdoc_path: str = Field(default="", max_length=1024)
    universe: Literal["all", "sh", "sz"] = "all"
    instrument_types: list[InstrumentType] | None = Field(default=None, max_length=1)
    boards: list[InstrumentBoard] | None = Field(default=None, max_length=7)
    start_date: date | None = None
    end_date: date | None = None
    reversal_multiples: list[float] = Field(
        default_factory=lambda: [2.0, 3.0, 4.0],
        max_length=8,
    )
    min_bars: int = Field(default=5, ge=0, le=1_000)
    atr_window: int = Field(default=14, ge=1, le=1_000)
    include_pending: bool = False
    limit_stocks: int | None = Field(default=DEFAULT_STOCK_LIMIT, ge=1, le=100_000)

    def to_config(self) -> SwingOverviewConfig:
        return SwingOverviewConfig(
            universe=self.universe,
            instrument_types=tuple(self.instrument_types or ("stock",)),
            boards=tuple(self.boards or ()),
            vipdoc_path=self.vipdoc_path,
            start_date=self.start_date,
            end_date=self.end_date,
            reversal_multiples=tuple(self.reversal_multiples),
            min_bars=self.min_bars,
            atr_window=self.atr_window,
            include_pending=self.include_pending,
            limit_stocks=self.limit_stocks,
        )


SwingLabel = Literal[
    "up_leg_start",
    "down_leg_start",
    "up_onset_ahead",
    "down_onset_ahead",
]


class SwingIndicatorRequest(BaseModel):
    """Score technical indicators against swing-derived labels.

    This is the study proper, so unlike the overview it fixes a *single*
    threshold: sweeping several here would multiply the very multiplicity the
    significance test is trying to control.

    ``labels`` picks the question. ``*_leg_start`` attributes big moves to the
    state of the bar they began from; ``*_onset_ahead`` asks whether a move
    starts running soon, which is the tradable version.
    """

    model_config = ConfigDict(extra="forbid")

    vipdoc_path: str = Field(default="", max_length=1024)
    universe: Literal["all", "sh", "sz"] = "all"
    instrument_types: list[InstrumentType] | None = Field(default=None, max_length=1)
    boards: list[InstrumentBoard] | None = Field(default=None, max_length=7)
    start_date: date | None = None
    end_date: date | None = None
    reversal_multiple: float = Field(default=3.0, gt=0, le=20)
    min_bars: int = Field(default=5, ge=0, le=1_000)
    atr_window: int = Field(default=14, ge=1, le=1_000)
    horizon: int = Field(default=10, ge=1, le=250)
    labels: list[SwingLabel] | None = Field(default=None, max_length=4)
    value_ids: list[str] | None = Field(default=None, max_length=64)
    #: Study allow-listed custom outputs, including 主力吸筹 and 突破信号, alongside
    #: the technical family.
    include_custom: bool = True
    min_cross_section: int = Field(default=20, ge=2, le=10_000)
    permutations: int = Field(default=DEFAULT_STUDY_PERMUTATIONS, ge=0, le=5_000)
    block_size: int = Field(default=20, ge=1, le=500)
    alpha: float = Field(default=0.05, gt=0, lt=1)
    limit_stocks: int | None = Field(default=DEFAULT_STOCK_LIMIT, ge=1, le=100_000)
    #: 存档用，不参与计算。研究分两步跑，第一步（切分校准）的表单与结果
    #: 只存在于浏览器里；不带过来，历史记录就只能还原第二步，加载后第一步
    #: 的表格会是空的。后端原样存进 request_json，不做校验。
    overview_request: dict[str, object] | None = None
    overview_result: dict[str, object] | None = None

    def to_config(self) -> SwingIndicatorConfig:
        return SwingIndicatorConfig(
            universe=self.universe,
            instrument_types=tuple(self.instrument_types or ("stock",)),
            boards=tuple(self.boards or ()),
            vipdoc_path=self.vipdoc_path,
            start_date=self.start_date,
            end_date=self.end_date,
            reversal_multiple=self.reversal_multiple,
            min_bars=self.min_bars,
            atr_window=self.atr_window,
            horizon=self.horizon,
            labels=tuple(self.labels) if self.labels else ALL_SWING_LABELS,
            value_ids=tuple(self.value_ids or ()),
            include_custom=self.include_custom,
            min_cross_section=self.min_cross_section,
            permutations=self.permutations,
            block_size=self.block_size,
            alpha=self.alpha,
            limit_stocks=self.limit_stocks,
        )
