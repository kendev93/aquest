"""Project-owned NumPy/Pandas implementations of the required TDX functions."""

from __future__ import annotations

from typing import Any, cast

import numpy as np
import pandas as pd


def ABS(values: Any) -> np.ndarray:
    return np.asarray(np.abs(values))


def MAX(left: Any, right: Any) -> np.ndarray:
    return np.asarray(np.maximum(left, right))


def MIN(left: Any, right: Any) -> np.ndarray:
    return np.asarray(np.minimum(left, right))


def REF(values: Any, period: int = 1) -> np.ndarray:
    source = np.asarray(values)
    if isinstance(period, bool) or not isinstance(period, (int, np.integer)) or period < 0:
        raise ValueError("REF 周期必须是非负整数")
    return np.asarray(pd.Series(source).shift(int(period)), dtype=float)


def SUM(values: Any, period: int) -> np.ndarray:
    source = np.asarray(values)
    if isinstance(period, bool) or not isinstance(period, (int, np.integer)):
        raise ValueError("SUM 周期必须是整数")
    if period > 0:
        return np.asarray(pd.Series(source).rolling(int(period)).sum(), dtype=float)
    return np.asarray(pd.Series(source).cumsum(), dtype=float)


def HHV(values: Any, period: int) -> np.ndarray:
    return _rolling(values, period, "max")


def LLV(values: Any, period: int) -> np.ndarray:
    return _rolling(values, period, "min")


def MA(values: Any, period: int) -> np.ndarray:
    return _rolling(values, period, "mean")


def _rolling(values: Any, period: int, operation: str) -> np.ndarray:
    source = np.asarray(values)
    if isinstance(period, bool) or not isinstance(period, (int, np.integer)) or period <= 0:
        raise ValueError("滚动周期必须是大于 0 的整数")
    rolling = pd.Series(source).rolling(int(period))
    if operation == "max":
        return np.asarray(rolling.max(), dtype=float)
    if operation == "min":
        return np.asarray(rolling.min(), dtype=float)
    if operation == "std":
        # Sample standard deviation (ddof=1), matching the Bollinger bands of
        # the built-in technical formula so a hand-written STD formula agrees
        # with the registered indicator.
        return np.asarray(rolling.std(), dtype=float)
    return np.asarray(rolling.mean(), dtype=float)


def STD(values: Any, period: int) -> np.ndarray:
    return _rolling(values, period, "std")


def AVEDEV(values: Any, period: int) -> np.ndarray:
    """Average absolute deviation over the window, the CCI denominator.

    ``mean(|x - mean(x)|)`` has no direct rolling method, and
    ``rolling().apply`` would call back into Python once per bar. The sliding
    window view keeps it vectorised; windows that contain a NaN yield NaN,
    which is the same contract as ``min_periods=period``.
    """

    source = np.asarray(values, dtype=float)
    if isinstance(period, bool) or not isinstance(period, (int, np.integer)) or period <= 0:
        raise ValueError("滚动周期必须是大于 0 的整数")
    window = int(period)
    result = np.full(source.shape, np.nan, dtype=float)
    if source.size < window:
        return result
    windows = np.lib.stride_tricks.sliding_window_view(source, window)
    deviations = np.abs(windows - windows.mean(axis=1, keepdims=True))
    result[window - 1 :] = deviations.mean(axis=1)
    return result


def SQRT(values: Any) -> np.ndarray:
    source = np.asarray(values, dtype=float)
    with np.errstate(invalid="ignore"):
        result = np.sqrt(source)
    # Negative inputs are NaN rather than a runtime warning: user formulas
    # routinely square-root expressions that dip below zero (RS- or variance
    # style terms) and a silent warning would be harder to debug than a NaN.
    return np.asarray(np.where(source < 0, np.nan, result), dtype=float)


def EMA(values: Any, period: int) -> np.ndarray:
    if isinstance(period, bool) or not isinstance(period, (int, np.integer)) or period <= 0:
        raise ValueError("EMA 周期必须是大于 0 的整数")
    return np.asarray(
        pd.Series(np.asarray(values)).ewm(span=int(period), adjust=False).mean(), dtype=float
    )


def SMA(values: Any, period: float, weight: float = 1) -> np.ndarray:
    if isinstance(period, bool) or not isinstance(period, (int, float, np.integer, np.floating)):
        raise ValueError("SMA 周期必须是数字")
    if not np.isfinite(period) or period <= 0:
        raise ValueError("SMA 周期必须是大于 0 的有限数字")
    if not np.isfinite(weight) or weight <= 0:
        raise ValueError("SMA 权重必须是大于 0 的有限数字")
    result = (
        pd.Series(np.asarray(values, dtype=float))
        .ewm(alpha=float(weight) / float(period), adjust=False)
        .mean()
        .to_numpy(dtype=float)
    )
    return cast(np.ndarray, result)


def COUNT(values: Any, period: int) -> np.ndarray:
    """周期内条件成立的根数；无效值（NaN/inf）按「不成立」计。

    ``dtype=bool`` 把 NaN 变成 True，于是预热段每根都被当成"条件成立过"：
    ``COUNT(CROSS(...), 5) >= 3`` 这类判据在前几根几乎必然为真，和筛选侧
    ``valid &`` 以及同模块 :func:`BARSLAST` 的口径都相反。
    """

    source = np.asarray(values, dtype=float)
    matched = np.isfinite(source) & (source != 0)
    return SUM(matched.astype(float), period)


def BARSLAST(values: Any) -> np.ndarray:
    """距上次条件为真的根数；从未为真时返回无效值（NaN）。

    ``dtype=bool`` 会把 NaN 变成 True，于是预热段一开头就报 0（"刚刚为真"）；而"从未
    为真"用递增计数表达时，``BARSLAST(x) > 30`` 会对一条从未成立过的条件判真。两者都
    与筛选侧 ``valid &`` 的口径相反，同一根 K 线上公式信号和指标条件会给出不同结论。
    """

    source = np.asarray(values, dtype=float)
    matched = np.isfinite(source) & (source != 0)
    result = np.full(len(source), np.nan)
    last_index: int | None = None
    for index, hit in enumerate(matched):
        if hit:
            last_index = index
        if last_index is not None:
            result[index] = index - last_index
    return result


def CROSS(left: Any, right: Any) -> np.ndarray:
    """上穿：前一根不高于右侧、这一根高于右侧，且两侧的数在两根 bar 上都有效。

    旧写法把"前一根是 NaN"当成"前一根在下方"，于是均线出第一个有效值的当天就报金叉；
    筛选侧 ``cross_up`` 要求前后两根都有效，同一份数据两侧结论相反。
    """

    left_array = np.asarray(left, dtype=float)
    right_array = np.asarray(right, dtype=float)
    crossed = np.zeros(left_array.size, dtype=bool)
    if left_array.size < 2:
        return crossed
    above = left_array > right_array
    valid = np.isfinite(left_array) & np.isfinite(right_array)
    crossed[1:] = ~above[:-1] & above[1:] & valid[:-1] & valid[1:]
    return crossed


class _TdxMath:
    ABS = staticmethod(ABS)
    MAX = staticmethod(MAX)
    MIN = staticmethod(MIN)
    REF = staticmethod(REF)
    SUM = staticmethod(SUM)
    HHV = staticmethod(HHV)
    LLV = staticmethod(LLV)
    MA = staticmethod(MA)
    EMA = staticmethod(EMA)
    SMA = staticmethod(SMA)
    STD = staticmethod(STD)
    AVEDEV = staticmethod(AVEDEV)
    SQRT = staticmethod(SQRT)
    COUNT = staticmethod(COUNT)
    BARSLAST = staticmethod(BARSLAST)
    CROSS = staticmethod(CROSS)


MYTT = _TdxMath()
