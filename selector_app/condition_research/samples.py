"""Build point-in-time feature samples from completed single-stock trades."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

import numpy as np
import pandas as pd

from selector_app.backtest.models import BacktestConfig
from selector_app.backtest.profiles import profile_from_config


@dataclass(frozen=True)
class MiningSamples:
    features: pd.DataFrame
    outcomes: pd.Series
    instruments: pd.Series
    groups: pd.Series

    def __len__(self) -> int:
        return len(self.outcomes)


def build_buy_trade_samples(
    trades: Sequence[Mapping[str, object]],
    feature_values: pd.DataFrame,
    *,
    market: str,
    code: str,
) -> MiningSamples:
    """Pair completed trades and take features visible on each buy signal date."""

    feature_rows = _feature_rows_by_date(feature_values)
    rows: list[dict[str, object]] = []
    outcomes: list[bool] = []
    instruments: list[str] = []
    groups: list[str] = []
    pending_entry: Mapping[str, object] | None = None

    for trade in trades:
        if bool(trade.get("rejected", False)):
            continue
        direction = trade.get("direction")
        if direction == "BUY":
            pending_entry = trade
            continue
        if direction != "SELL" or pending_entry is None:
            continue
        pnl = _number(trade.get("pnl"))
        signal_date = _date_text(pending_entry.get("signal_date"))
        if signal_date is None:
            pending_entry = None
            continue
        feature_row = feature_rows.get(signal_date)
        if pnl is not None and feature_row is not None:
            rows.append(feature_row)
            outcomes.append(pnl > 0)
            instruments.append(f"{market}{code}")
            groups.append(_trade_group(market, code, pending_entry))
        pending_entry = None

    return _samples(feature_values, rows, outcomes, instruments, groups)


def build_early_exit_samples(
    trades: Sequence[Mapping[str, object]],
    feature_values: pd.DataFrame,
    bars: pd.DataFrame,
    config: BacktestConfig,
    *,
    market: str,
    code: str,
    max_decision_points_per_trade: int = 8,
) -> MiningSamples:
    """Label holding-period decisions by whether an earlier exit beats the base exit."""

    if max_decision_points_per_trade < 1:
        raise ValueError("每笔交易的卖出决策样本上限必须大于 0")
    feature_rows = _feature_rows_by_date(feature_values)
    prepared_bars = bars.copy(deep=False)
    prepared_bars["date"] = pd.to_datetime(prepared_bars["date"], errors="coerce")
    date_to_index = {
        timestamp.strftime("%Y-%m-%d"): index
        for index, timestamp in enumerate(prepared_bars["date"])
        if not pd.isna(timestamp)
    }
    profile = profile_from_config(
        config.instrument_type or "stock",
        commission=config.commission,
        min_commission=config.min_commission,
        stamp_tax=config.stamp_tax,
    )

    rows: list[dict[str, object]] = []
    outcomes: list[bool] = []
    instruments: list[str] = []
    groups: list[str] = []
    pending_entry: Mapping[str, object] | None = None

    for trade in trades:
        if bool(trade.get("rejected", False)):
            continue
        direction = trade.get("direction")
        if direction == "BUY":
            pending_entry = trade
            continue
        if direction != "SELL" or pending_entry is None:
            continue

        entry_date = _date_text(pending_entry.get("date"))
        sell_signal_date = _date_text(trade.get("signal_date"))
        sell_execution_date = _date_text(trade.get("date"))
        start_index = date_to_index.get(entry_date)
        signal_index = date_to_index.get(sell_signal_date)
        execution_index = date_to_index.get(sell_execution_date)
        size = _number(trade.get("size"))
        cost_basis = _number(trade.get("cost_basis"))
        base_pnl = _number(trade.get("pnl"))
        if (
            start_index is not None
            and signal_index is not None
            and execution_index is not None
            and size is not None
            and cost_basis is not None
            and base_pnl is not None
            and size > 0
            and cost_basis > 0
        ):
            group = _trade_group(market, code, pending_entry)
            decision_indices = list(range(start_index, signal_index))
            if len(decision_indices) > max_decision_points_per_trade:
                sampled_offsets = np.linspace(
                    0,
                    len(decision_indices) - 1,
                    num=max_decision_points_per_trade,
                    dtype=int,
                )
                decision_indices = [decision_indices[int(offset)] for offset in sampled_offsets]
            for decision_index in decision_indices:
                next_index = decision_index + 1
                if next_index >= len(prepared_bars) or next_index > execution_index:
                    continue
                decision_date = prepared_bars.iloc[decision_index]["date"].strftime("%Y-%m-%d")
                feature_row = feature_rows.get(decision_date)
                if feature_row is None:
                    continue
                execution_row = prepared_bars.iloc[next_index]
                price = float(
                    execution_row["open"]
                    if config.execution == "next_open"
                    else execution_row["close"]
                )
                commission = max(size * price * profile.commission, profile.min_commission)
                stamp_tax = size * price * profile.stamp_tax
                slippage = size * config.slippage
                early_exit_pnl = size * price - commission - stamp_tax - slippage - cost_basis
                rows.append(feature_row)
                outcomes.append(early_exit_pnl > base_pnl + 1e-9)
                instruments.append(f"{market}{code}")
                groups.append(group)
        pending_entry = None

    return _samples(feature_values, rows, outcomes, instruments, groups)


def _feature_rows_by_date(feature_values: pd.DataFrame) -> dict[str, dict[str, object]]:
    if "date" not in feature_values.columns:
        raise ValueError("指标样本缺少日期列")
    indexed: dict[str, dict[str, object]] = {}
    for record in feature_values.to_dict(orient="records"):
        date_text = _date_text(record.get("date"))
        if date_text is not None:
            indexed[date_text] = {key: value for key, value in record.items() if key != "date"}
    return indexed


def _samples(
    feature_values: pd.DataFrame,
    rows: list[dict[str, object]],
    outcomes: list[bool],
    instruments: list[str],
    groups: list[str],
) -> MiningSamples:
    feature_columns = [column for column in feature_values.columns if column != "date"]
    return MiningSamples(
        features=pd.DataFrame(rows, columns=feature_columns),
        outcomes=pd.Series(outcomes, dtype=bool),
        instruments=pd.Series(instruments, dtype=str),
        groups=pd.Series(groups, dtype=str),
    )


def _trade_group(market: str, code: str, entry: Mapping[str, object]) -> str:
    return (
        f"{market}{code}:"
        f"{_date_text(entry.get('signal_date')) or 'unknown'}:"
        f"{_date_text(entry.get('date')) or 'unknown'}"
    )


def _date_text(value: object) -> str | None:
    if value is None:
        return None
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(timestamp) else str(timestamp.strftime("%Y-%m-%d"))


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, np.number)):
        return None
    resolved = float(cast(float, value))
    return resolved if math.isfinite(resolved) else None
