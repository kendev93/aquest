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
    # 信号/决策发生日期。挖掘本身不看日期（样本是横着比的），但「日均命中多少只股票」
    # 和「排序指标的横截面 IC」都要求把同一天的样本归到一起，所以日期必须随样本走。
    dates: pd.Series
    # 对应的收益率，用于排序推荐的横截面相关度；买入样本是整笔交易收益率，
    # 卖出样本是「按该决策点卖出」的收益率，与基准同一口径（都除以成本）。
    returns: pd.Series
    # 每个决策点**前一个交易日**的同一批特征值（行与 features 一一对齐，取不到就整行 NaN）。
    # 样本本身是横着摆的决策点快照，相邻两行之间没有先后关系，所以「上穿/下穿」这类
    # 需要前一状态的比较只能靠它来算；没有它，挖掘就只能出大小比较。
    previous_features: pd.DataFrame | None = None

    def __len__(self) -> int:
        return len(self.outcomes)


def build_buy_trade_samples(
    trades: Sequence[Mapping[str, object]],
    feature_values: pd.DataFrame,
    *,
    market: str,
    code: str,
    feature_rows: Mapping[str, dict[str, object]] | None = None,
) -> MiningSamples:
    """Pair completed trades and take features visible on each buy signal date.

    ``feature_rows`` lets one research pass reuse the same date index for the
    buy and the early-exit samplers instead of rebuilding it per call.
    """

    feature_rows = feature_rows_by_date(feature_values) if feature_rows is None else feature_rows
    previous_dates = previous_date_by_date(feature_values)
    rows: list[dict[str, object]] = []
    previous_rows: list[dict[str, object] | None] = []
    outcomes: list[bool] = []
    instruments: list[str] = []
    groups: list[str] = []
    dates: list[str] = []
    returns: list[float] = []
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
            previous_rows.append(_previous_row(feature_rows, previous_dates, signal_date))
            outcomes.append(pnl > 0)
            instruments.append(f"{market}{code}")
            groups.append(_trade_group(market, code, pending_entry))
            dates.append(signal_date)
            returns.append(_number(trade.get("return")) or 0.0)
        pending_entry = None

    return _samples(
        feature_values, rows, outcomes, instruments, groups, dates, returns, previous_rows
    )


def build_early_exit_samples(
    trades: Sequence[Mapping[str, object]],
    feature_values: pd.DataFrame,
    bars: pd.DataFrame,
    config: BacktestConfig,
    *,
    market: str,
    code: str,
    max_decision_points_per_trade: int = 8,
    feature_rows: Mapping[str, dict[str, object]] | None = None,
) -> MiningSamples:
    """Label holding-period decisions by whether an earlier exit beats the base exit."""

    if max_decision_points_per_trade < 1:
        raise ValueError("每笔交易的卖出决策样本上限必须大于 0")
    feature_rows = feature_rows_by_date(feature_values) if feature_rows is None else feature_rows
    previous_dates = previous_date_by_date(feature_values)
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
    previous_rows: list[dict[str, object] | None] = []
    outcomes: list[bool] = []
    instruments: list[str] = []
    groups: list[str] = []
    dates: list[str] = []
    returns: list[float] = []
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
                previous_rows.append(_previous_row(feature_rows, previous_dates, decision_date))
                outcomes.append(early_exit_pnl > base_pnl + 1e-9)
                instruments.append(f"{market}{code}")
                groups.append(group)
                dates.append(decision_date)
                returns.append(early_exit_pnl / cost_basis)
        pending_entry = None

    return _samples(
        feature_values, rows, outcomes, instruments, groups, dates, returns, previous_rows
    )


def feature_rows_by_date(feature_values: pd.DataFrame) -> dict[str, dict[str, object]]:
    """Index one stock's feature rows by ``YYYY-MM-DD`` for sample lookups.

    ``to_dict`` already returns a fresh dict per row, so the date column is
    popped in place instead of copying every record again.
    """

    if "date" not in feature_values.columns:
        raise ValueError("指标样本缺少日期列")
    indexed: dict[str, dict[str, object]] = {}
    for record in feature_values.to_dict(orient="records"):
        date_text = _date_text(record.pop("date", None))
        if date_text is not None:
            indexed[date_text] = record
    return indexed


def previous_date_by_date(feature_values: pd.DataFrame) -> dict[str, str]:
    """每个特征日期的**上一个交易日**（按特征表本身的行序）。

    样本是横着摆的决策点快照，相邻两行之间没有先后关系，所以「上一天是哪天」
    只能回特征表里查。这里按行序取前一行，而不是按自然日减一：停牌、周末、
    假期都不是交易日，减一会得到一条根本不存在的行情。
    """

    if "date" not in feature_values.columns:
        raise ValueError("指标样本缺少日期列")
    ordered: list[str] = []
    for value in feature_values["date"]:
        text = _date_text(value)
        if text is not None:
            ordered.append(text)
    return {date_text: ordered[index - 1] for index, date_text in enumerate(ordered) if index > 0}


def _previous_row(
    feature_rows: Mapping[str, dict[str, object]],
    previous_dates: Mapping[str, str],
    date_text: str,
) -> dict[str, object] | None:
    previous_date = previous_dates.get(date_text)
    if previous_date is None:
        return None
    return feature_rows.get(previous_date)


def _samples(
    feature_values: pd.DataFrame,
    rows: list[dict[str, object]],
    outcomes: list[bool],
    instruments: list[str],
    groups: list[str],
    dates: list[str],
    returns: list[float],
    previous_rows: list[dict[str, object] | None] | None = None,
) -> MiningSamples:
    feature_columns = [column for column in feature_values.columns if column != "date"]
    previous_frame: pd.DataFrame | None = None
    if previous_rows is not None:
        if len(previous_rows) != len(rows):
            raise ValueError("前一日特征的行数必须与样本一致")
        filled = [
            row if row is not None else {column: float("nan") for column in feature_columns}
            for row in previous_rows
        ]
        previous_frame = pd.DataFrame(filled, columns=feature_columns)
    return MiningSamples(
        features=pd.DataFrame(rows, columns=feature_columns),
        outcomes=pd.Series(outcomes, dtype=bool),
        instruments=pd.Series(instruments, dtype=str),
        groups=pd.Series(groups, dtype=str),
        dates=pd.Series(dates, dtype=str),
        returns=pd.Series(returns, dtype=float),
        previous_features=previous_frame,
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
