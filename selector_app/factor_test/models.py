"""Configuration for forward-return factor tests."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date

from selector_app.market_data.models import InstrumentBoard, InstrumentType

#: A day with only a handful of stocks carries no cross-sectional information:
#: a rank correlation over three points is meaningless.
MIN_CROSS_SECTION = 20
#: Below this many signal days the standard error of the mean IC is of the same
#: order as the IC itself. Reported as a caveat, never as a hard stop - the
#: numbers are still shown, they just cannot carry a conclusion.
MIN_IC_DAYS = 60
#: Default look-ahead in trading days. Short enough that a year of data holds a
#: few dozen non-overlapping windows, long enough to clear a day's noise.
DEFAULT_HORIZON = 5
DEFAULT_PERMUTATIONS = 200
DEFAULT_BLOCK_SIZE = 20
DEFAULT_ALPHA = 0.05
#: Layers for the quantile breakdown. Five is the convention and the compromise:
#: enough to see a shape, few enough that each layer keeps a real sample.
QUANTILE_COUNT = 5
DEFAULT_SEED = 20240918
WEAK_IC = 0.02
STRONG_IC = 0.05
STABLE_ICIR = 0.3
#: How far past the permutation noise floor an effect has to sit before the
#: summary is allowed to call it strong.
CLEAR_IC_RATIO = 2.0
#: Fraction of the layer ladder's own span below which an upward step counts as
#: flat rather than as a break in monotonicity.
LAYER_INVERSION_TOLERANCE = 0.05
#: Below this share of winning days the top-minus-bottom spread is a coin flip.
#: It is a warning, not a verdict: the ranking can still be real, it just is
#: not obviously executable at daily frequency.
COINFLIP_WIN_RATE = 0.55

Order = str  # "high" (buy the largest values) or "low"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


#: Ceiling on the in-memory panel. Unlike the swing study this one never spills
#: to disk, so this is a real memory guard and not just a patience guard:
#: a full-market, five-year, all-indicator run asks for about 800 MB of values.
DEFAULT_MAX_PANEL_BYTES = 3 * 1024**3
MAX_PANEL_BYTES = _env_int("FACTOR_PANEL_MAX_BYTES", DEFAULT_MAX_PANEL_BYTES)


@dataclass(frozen=True)
class FactorTestConfig:
    """One request to test indicators against forward returns."""

    universe: str = "all"
    instrument_types: tuple[InstrumentType, ...] = ("stock",)
    boards: tuple[InstrumentBoard, ...] = ()
    vipdoc_path: str = ""
    start_date: date | None = None
    end_date: date | None = None
    #: Forward horizon in trading days.
    horizon: int = DEFAULT_HORIZON
    value_ids: tuple[str, ...] = ()
    #: Include the allow-listed continuous outputs of the three TDX formulas.
    include_custom: bool = True
    min_cross_section: int = MIN_CROSS_SECTION
    permutations: int = DEFAULT_PERMUTATIONS
    block_size: int = DEFAULT_BLOCK_SIZE
    alpha: float = DEFAULT_ALPHA
    quantile_count: int = QUANTILE_COUNT
    batch_size: int = 200
    limit_stocks: int | None = None

    def normalized(self) -> FactorTestConfig:
        if self.horizon < 1:
            raise ValueError("前瞻天数至少要有 1 个交易日")
        if self.horizon > 250:
            raise ValueError("前瞻天数不能超过 250 个交易日")
        if self.min_cross_section < 2:
            raise ValueError("每日横截面样本数至少为 2")
        if self.permutations < 0:
            raise ValueError("噪声门槛检验次数不能为负数")
        if self.block_size < 1:
            raise ValueError("置换块长度至少为 1 个交易日")
        if not 0 < self.alpha < 1:
            raise ValueError("显著性水平必须介于 0 与 1 之间")
        if self.quantile_count < 2:
            raise ValueError("分层数至少为 2")
        if self.batch_size < 1:
            raise ValueError("分批读取的股票数必须大于 0")
        if self.limit_stocks is not None and self.limit_stocks < 1:
            raise ValueError("股票数量上限必须大于 0")
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("开始日期不能晚于结束日期")
        return FactorTestConfig(
            universe=self.universe,
            instrument_types=tuple(self.instrument_types) or ("stock",),
            boards=tuple(self.boards),
            vipdoc_path=self.vipdoc_path,
            start_date=self.start_date,
            end_date=self.end_date,
            horizon=int(self.horizon),
            value_ids=tuple(dict.fromkeys(self.value_ids)),
            include_custom=bool(self.include_custom),
            min_cross_section=self.min_cross_section,
            permutations=self.permutations,
            block_size=self.block_size,
            alpha=self.alpha,
            quantile_count=self.quantile_count,
            batch_size=self.batch_size,
            limit_stocks=self.limit_stocks,
        )


__all__ = [
    "CLEAR_IC_RATIO",
    "COINFLIP_WIN_RATE",
    "DEFAULT_ALPHA",
    "DEFAULT_BLOCK_SIZE",
    "DEFAULT_HORIZON",
    "DEFAULT_PERMUTATIONS",
    "DEFAULT_SEED",
    "MAX_PANEL_BYTES",
    "MIN_CROSS_SECTION",
    "MIN_IC_DAYS",
    "LAYER_INVERSION_TOLERANCE",
    "QUANTILE_COUNT",
    "STABLE_ICIR",
    "STRONG_IC",
    "WEAK_IC",
    "FactorTestConfig",
]
