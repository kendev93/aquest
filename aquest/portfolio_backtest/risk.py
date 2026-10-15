"""Risk-control calculations for portfolio simulation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from .models import PortfolioBacktestConfig

if TYPE_CHECKING:
    from .context import _StockSeries
    from .simulation_models import _Position

# 每标的策略可以覆盖的风控参数。判定、到期与展示三档风控价都必须走
# :func:`_risk_config_for_symbol` 取同一份生效配置，否则显示出来的线会和
# 引擎真正用的那条脱钩。``fast_sim._risk_override`` 是同一张表的第三份拷贝
# （njit 里拿不到 getattr），改动这里时同步它。
_SYMBOL_RISK_FIELDS = (
    "stop_loss_pct",
    "stop_loss_mode",
    "stop_loss_atr_multiplier",
    "stop_loss_cooldown_days",
    "take_profit_pct",
    "take_profit_mode",
    "take_profit_atr_multiplier",
    "take_profit_cooldown_days",
    "trailing_stop_pct",
    "trailing_stop_mode",
    "trailing_stop_atr_multiplier",
    "trailing_stop_cooldown_days",
    "atr_period",
    "max_hold_days",
)


def _risk_config_for_symbol(
    config: PortfolioBacktestConfig,
    strategy_config: Mapping[str, object],
) -> PortfolioBacktestConfig:
    """按标的覆盖风控参数；策略里没有的字段保持全局配置。"""

    overrides: dict[str, Any] = {
        key: strategy_config[key] for key in _SYMBOL_RISK_FIELDS if key in strategy_config
    }
    return replace(config, **overrides) if overrides else config


def _atr_series(frame: pd.DataFrame, period: int) -> np.ndarray:
    """Causal ATR with Wilder smoothing, the same recipe the indicator catalog uses.

    风控线过去用的是简单移动平均，而目录里给用户看的 ``atr_pct`` 是 Wilder
    （``formulas/technical.py``，``swing_research/segments.py`` 把 Wilder 定为权威），
    同一个名字两套算法：界面显示的 ATR 和引擎用的止损线永远对不上。这里统一到 Wilder，
    两个引擎共用这一个函数，所以搜索与回放一起变。
    """

    high = pd.to_numeric(frame["high"], errors="coerce")
    low = pd.to_numeric(frame["low"], errors="coerce")
    close = pd.to_numeric(frame["close"], errors="coerce")
    previous_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()],
        axis=1,
    ).max(axis=1)
    return np.asarray(
        true_range.ewm(alpha=1 / period, adjust=False, min_periods=period)
        .mean()
        .to_numpy(dtype=float),
        dtype=float,
    )


def _risk_level_set(
    position: _Position,
    config: PortfolioBacktestConfig,
    atr: float | None,
) -> dict[str, float | None]:
    """当前仓位生效的三档风控价；没有启用的规则返回 None（不得当 0 用）。"""

    return {
        "stop_loss": _risk_level(
            position.entry_price,
            config.stop_loss_mode,
            config.stop_loss_pct,
            config.stop_loss_atr_multiplier,
            atr,
            direction="down",
        ),
        "trailing_stop": _risk_level(
            position.highest_price,
            config.trailing_stop_mode,
            config.trailing_stop_pct,
            config.trailing_stop_atr_multiplier,
            atr,
            direction="down",
        ),
        "take_profit": _risk_level(
            position.entry_price,
            config.take_profit_mode,
            config.take_profit_pct,
            config.take_profit_atr_multiplier,
            atr,
            direction="up",
        ),
    }


def _risk_trigger(
    context: _StockSeries,
    index: int,
    position: _Position,
    config: PortfolioBacktestConfig,
) -> tuple[str, int] | None:
    """Return the first OHLC risk trigger using deterministic priority.

    只判定「哪条线被碰到」，不决定成交价：风控卖单和规则卖单一样登记成挂单，由
    下一交易日开盘撮合。本系统只在收盘后批处理，盘中那一下触碰当天既不知道也来不及
    挂单，成交价因此不属于这里。
    """

    high = float(context.highs[index])
    low = float(context.lows[index])
    open_price = float(context.opens[index])
    if not (np.isfinite(high) and np.isfinite(low) and np.isfinite(open_price)):
        return None
    position.highest_price = max(position.highest_price, high)
    atr = float(context.atrs[index]) if np.isfinite(context.atrs[index]) else None

    levels = _risk_level_set(position, config, atr)
    stop_level = levels["stop_loss"]
    trailing_level = levels["trailing_stop"]
    take_level = levels["take_profit"]
    if stop_level is not None and low <= stop_level:
        return "stop_loss", int(config.stop_loss_cooldown_days)
    if trailing_level is not None and low <= trailing_level:
        return "trailing_stop", int(config.trailing_stop_cooldown_days)
    if take_level is not None and high >= take_level:
        return "take_profit", int(config.take_profit_cooldown_days)
    return None


def _risk_level(
    base: float,
    mode: str,
    percentage: float | None,
    atr_multiplier: float | None,
    atr: float | None,
    *,
    direction: str,
) -> float | None:
    if mode == "atr":
        if atr_multiplier is None or atr is None:
            return None
        distance = atr * atr_multiplier
    else:
        if percentage is None:
            return None
        distance = base * percentage
    return base - distance if direction == "down" else base + distance


def _risk_trigger_label(trigger: str) -> str:
    return {
        "stop_loss": "止损",
        "take_profit": "止盈",
        "trailing_stop": "移动止盈",
    }.get(trigger, "风险卖出")


def _risk_controls(config: PortfolioBacktestConfig) -> dict[str, object]:
    return {
        "take_profit_mode": config.take_profit_mode,
        "take_profit_pct": config.take_profit_pct,
        "take_profit_atr_multiplier": config.take_profit_atr_multiplier,
        "take_profit_cooldown_days": config.take_profit_cooldown_days,
        "stop_loss_mode": config.stop_loss_mode,
        "stop_loss_pct": config.stop_loss_pct,
        "stop_loss_atr_multiplier": config.stop_loss_atr_multiplier,
        "stop_loss_cooldown_days": config.stop_loss_cooldown_days,
        "trailing_stop_mode": config.trailing_stop_mode,
        "trailing_stop_pct": config.trailing_stop_pct,
        "trailing_stop_atr_multiplier": config.trailing_stop_atr_multiplier,
        "trailing_stop_cooldown_days": config.trailing_stop_cooldown_days,
        "atr_period": config.atr_period,
    }
