"""Risk-control calculations for portfolio simulation."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from .models import PortfolioBacktestConfig

if TYPE_CHECKING:
    from .context import _StockSeries
    from .simulation_models import _Position


def _atr_series(frame: pd.DataFrame, period: int) -> np.ndarray:
    """Calculate a causal ATR series from daily OHLC data."""

    high = pd.to_numeric(frame["high"], errors="coerce")
    low = pd.to_numeric(frame["low"], errors="coerce")
    close = pd.to_numeric(frame["close"], errors="coerce")
    previous_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()],
        axis=1,
    ).max(axis=1)
    return np.asarray(
        true_range.rolling(period, min_periods=period).mean().to_numpy(dtype=float),
        dtype=float,
    )


def _risk_trigger(
    context: _StockSeries,
    index: int,
    position: _Position,
    config: PortfolioBacktestConfig,
) -> tuple[str, float, int] | None:
    """Return the first OHLC risk trigger using deterministic priority."""

    high = float(context.highs[index])
    low = float(context.lows[index])
    open_price = float(context.opens[index])
    if not (np.isfinite(high) and np.isfinite(low) and np.isfinite(open_price)):
        return None
    position.highest_price = max(position.highest_price, high)
    atr = float(context.atrs[index]) if np.isfinite(context.atrs[index]) else None

    stop_level = _risk_level(
        position.entry_price,
        config.stop_loss_mode,
        config.stop_loss_pct,
        config.stop_loss_atr_multiplier,
        atr,
        direction="down",
    )
    if stop_level is not None and low <= stop_level:
        return (
            "stop_loss",
            _risk_fill_price(open_price, stop_level, direction="down"),
            config.stop_loss_cooldown_days,
        )

    moving_levels = (
        (
            "moving_stop_loss",
            _risk_level(
                position.highest_price,
                config.moving_stop_loss_mode,
                config.moving_stop_loss_pct,
                config.moving_stop_loss_atr_multiplier,
                atr,
                direction="down",
            ),
            config.moving_stop_loss_cooldown_days,
            0,
        ),
        (
            "trailing_stop",
            _risk_level(
                position.highest_price,
                config.trailing_stop_mode,
                config.trailing_stop_pct,
                config.trailing_stop_atr_multiplier,
                atr,
                direction="down",
            ),
            config.trailing_stop_cooldown_days,
            1,
        ),
    )
    triggered_moving_levels = [
        (trigger, level, cooldown, tie_breaker)
        for trigger, level, cooldown, tie_breaker in moving_levels
        if level is not None and low <= level
    ]
    if triggered_moving_levels:
        trigger, level, cooldown, _tie_breaker = max(
            triggered_moving_levels,
            key=lambda item: (float(item[1]), -item[3]),
        )
        assert level is not None
        return (
            trigger,
            _risk_fill_price(open_price, level, direction="down"),
            cooldown,
        )

    take_level = _risk_level(
        position.entry_price,
        config.take_profit_mode,
        config.take_profit_pct,
        config.take_profit_atr_multiplier,
        atr,
        direction="up",
    )
    if take_level is not None and high >= take_level:
        return (
            "take_profit",
            _risk_fill_price(open_price, take_level, direction="up"),
            config.take_profit_cooldown_days,
        )
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


def _risk_fill_price(open_price: float, trigger_price: float, *, direction: str) -> float:
    if direction == "down":
        return open_price if open_price <= trigger_price else trigger_price
    return open_price if open_price >= trigger_price else trigger_price


def _risk_trigger_label(trigger: str) -> str:
    return {
        "stop_loss": "止损",
        "take_profit": "止盈",
        "moving_stop_loss": "移动止损",
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
        "moving_stop_loss_mode": config.moving_stop_loss_mode,
        "moving_stop_loss_pct": config.moving_stop_loss_pct,
        "moving_stop_loss_atr_multiplier": config.moving_stop_loss_atr_multiplier,
        "moving_stop_loss_cooldown_days": config.moving_stop_loss_cooldown_days,
        "trailing_stop_mode": config.trailing_stop_mode,
        "trailing_stop_pct": config.trailing_stop_pct,
        "trailing_stop_atr_multiplier": config.trailing_stop_atr_multiplier,
        "trailing_stop_cooldown_days": config.trailing_stop_cooldown_days,
        "atr_period": config.atr_period,
    }
