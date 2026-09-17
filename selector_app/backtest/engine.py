"""Deterministic daily-bar transaction simulator owned by the application."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date
from typing import Literal

import numpy as np
import pandas as pd

from selector_app.formulas.common import validate_market_data

from .performance import PerformanceAnalyzer
from .profiles import InstrumentType, TradeProfile, profile_from_config

ExecutionMode = Literal["next_open", "next_close"]
PositionMode = Literal["full", "fixed"]


@dataclass(frozen=True)
class SignalBacktestConfig:
    initial_cash: float = 100_000.0
    commission: float = 0.0001
    min_commission: float = 0.1
    stamp_tax: float = 0.001
    slippage: float = 0.0
    execution: ExecutionMode = "next_open"
    position_mode: PositionMode = "full"
    fixed_size: int | None = None
    lot_size: int = 100
    start_date: date | None = None
    end_date: date | None = None
    instrument_type: InstrumentType = "stock"
    # 收官时把还没卖掉的仓位按最后一根收盘价平掉。只有「不设卖出规则」的场景需要它，
    # 默认关闭以免改变既有回测的交易笔数。
    close_at_end: bool = False

    def __post_init__(self) -> None:
        if not math.isfinite(self.initial_cash) or self.initial_cash <= 0:
            raise ValueError("初始资金必须是大于 0 的有限数字")
        for name, value in {
            "commission": self.commission,
            "min_commission": self.min_commission,
            "stamp_tax": self.stamp_tax,
            "slippage": self.slippage,
        }.items():
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} 必须是大于等于 0 的有限数字")
        if self.execution not in {"next_open", "next_close"}:
            raise ValueError("不支持的成交方式")
        if self.position_mode not in {"full", "fixed"}:
            raise ValueError("不支持的仓位方式")
        if isinstance(self.lot_size, bool) or self.lot_size <= 0:
            raise ValueError("成交单位必须是正整数")
        if self.position_mode == "fixed":
            if (
                self.fixed_size is None
                or isinstance(self.fixed_size, bool)
                or self.fixed_size < self.lot_size
                or self.fixed_size % self.lot_size != 0
            ):
                raise ValueError("固定数量必须是成交单位的整数倍")
        elif self.fixed_size is not None:
            raise ValueError("全仓模式不应设置固定数量")


@dataclass(frozen=True)
class SignalBacktestResult:
    performance: dict[str, float]
    equity_curve: pd.DataFrame
    trades: pd.DataFrame
    positions: pd.DataFrame
    diagnostic: str | None = None


_TRADE_COLUMNS = [
    "datetime",
    "market",
    "code",
    "name",
    "direction",
    "size",
    "price",
    "commission",
    "stamp_tax",
    "slippage",
    "pnl",
    "cost_basis",
    "rejected",
    # 买卖条件研究按信号日的可见特征抽取样本；缺这一列会让样本恒为空，
    # 因此 _trade 写入的 signal_date 必须保留在这个投影里。
    "signal_date",
]
_POSITION_COLUMNS = [
    "datetime",
    "size",
    "avg_price",
    "market_value",
    "unrealized_pnl",
]


def run_signal_backtest(
    frame: pd.DataFrame,
    buy_mask: np.ndarray,
    sell_mask: np.ndarray,
    config: SignalBacktestConfig,
    *,
    market: str = "",
    code: str = "",
    name: str | None = None,
    summary_only: bool = False,
) -> SignalBacktestResult:
    """Run one long-only signal stream with next-bar execution semantics."""

    prepared = frame.copy(deep=True)
    if prepared.empty:
        empty_equity = pd.DataFrame(
            columns=["date", "cash", "position_value", "total", "drawdown", "drawdown_pct"]
        )
        empty_trades = pd.DataFrame(columns=_TRADE_COLUMNS)
        empty_positions = pd.DataFrame(columns=_POSITION_COLUMNS)
        analyzer = PerformanceAnalyzer(empty_equity, empty_trades)
        performance = analyzer.compute()
        return SignalBacktestResult(
            performance, empty_equity, empty_trades, empty_positions, analyzer.diagnostic
        )
    validate_market_data(prepared)
    prepared["date"] = pd.to_datetime(prepared["date"], errors="coerce")
    prepared = prepared.sort_values("date", kind="stable").reset_index(drop=True)
    buys = np.asarray(buy_mask, dtype=bool)
    sells = np.asarray(sell_mask, dtype=bool)
    if len(buys) != len(frame) or len(sells) != len(frame):
        raise ValueError("交易信号长度必须与行情数据一致")
    # The service normally slices signals before calling us. This second guard
    # keeps the standalone engine safe when callers provide a date window.
    if config.start_date is not None or config.end_date is not None:
        dates = prepared["date"].dt.date
        selected = np.ones(len(prepared), dtype=bool)
        if config.start_date is not None:
            selected &= dates.to_numpy() >= config.start_date
        if config.end_date is not None:
            selected &= dates.to_numpy() <= config.end_date
        prepared = prepared.loc[selected].reset_index(drop=True)
        buys = buys[selected]
        sells = sells[selected]

    profile = replace(
        profile_from_config(
            config.instrument_type,
            commission=config.commission,
            min_commission=config.min_commission,
            stamp_tax=config.stamp_tax,
        ),
        lot_size=config.lot_size,
    )
    cash = float(config.initial_cash)
    position = 0.0
    average_cost = 0.0
    pending: tuple[str, int] | None = None
    trades: list[dict[str, object]] = []
    equity: list[dict[str, object]] = []
    positions: list[dict[str, object]] = []
    totals: list[float] = []
    dates = prepared["date"].to_numpy()
    opens = prepared["open"].to_numpy(dtype=float, copy=False)
    closes = prepared["close"].to_numpy(dtype=float, copy=False)

    for index in range(len(prepared)):
        current_date = pd.Timestamp(dates[index])
        if pending is not None:
            direction, signal_index = pending
            price = float(opens[index] if config.execution == "next_open" else closes[index])
            if direction == "BUY":
                size = _buy_size(cash, price, config, profile)
                if size > 0:
                    commission = max(size * price * profile.commission, profile.min_commission)
                    slippage = size * config.slippage
                    cash -= size * price + commission + slippage
                    average_cost = (
                        (position * average_cost + size * price + commission + slippage)
                        / (position + size)
                        if position + size > 0
                        else 0.0
                    )
                    position += size
                    trades.append(
                        _trade(
                            current_date,
                            direction,
                            size,
                            price,
                            commission,
                            0.0,
                            slippage,
                            0.0,
                            0.0,
                            False,
                            market,
                            code,
                            name,
                            signal_index,
                            dates[signal_index],
                        )
                    )
            else:
                size = position if config.position_mode in {"full", "fixed"} else position
                if size > 0:
                    commission = max(size * price * profile.commission, profile.min_commission)
                    stamp_tax = size * price * profile.stamp_tax
                    slippage = size * config.slippage
                    cost_basis = average_cost * size
                    pnl = size * price - commission - stamp_tax - slippage - cost_basis
                    cash += size * price - commission - stamp_tax - slippage
                    trades.append(
                        _trade(
                            current_date,
                            direction,
                            size,
                            price,
                            commission,
                            stamp_tax,
                            slippage,
                            pnl,
                            cost_basis,
                            False,
                            market,
                            code,
                            name,
                            signal_index,
                            dates[signal_index],
                        )
                    )
                    position = 0.0
                    average_cost = 0.0
            pending = None

        close = float(closes[index])
        position_value = position * close
        total = cash + position_value
        if summary_only:
            totals.append(total)
        else:
            equity.append(
                {
                    "date": current_date.strftime("%Y-%m-%d"),
                    "datetime": current_date,
                    "cash": cash,
                    "position_value": position_value,
                    "total": total,
                }
            )
            positions.append(
                {
                    "datetime": current_date,
                    "size": position,
                    "avg_price": average_cost,
                    "market_value": position_value,
                    "unrealized_pnl": (close - average_cost) * position,
                }
            )

        if index < len(prepared) - 1:
            if position > 0 and sells[index]:
                pending = ("SELL", index)
            elif position <= 0 and buys[index]:
                pending = ("BUY", index)

    if config.close_at_end and position > 0:
        # 没有卖出规则时的收官平仓：按最后一根收盘价成交，signal 与成交同日。
        # 权益曲线此前已经按收盘价计过市值，这里只补一笔交易记录，不再改曲线。
        last_index = len(prepared) - 1
        current_date = pd.Timestamp(dates[last_index])
        price = float(closes[last_index])
        size = position
        commission = max(size * price * profile.commission, profile.min_commission)
        stamp_tax = size * price * profile.stamp_tax
        slippage = size * config.slippage
        cost_basis = average_cost * size
        pnl = size * price - commission - stamp_tax - slippage - cost_basis
        cash += size * price - commission - stamp_tax - slippage
        trades.append(
            _trade(
                current_date,
                "SELL",
                size,
                price,
                commission,
                stamp_tax,
                slippage,
                pnl,
                cost_basis,
                False,
                market,
                code,
                name,
                last_index,
                dates[last_index],
            )
        )
        position = 0.0
        average_cost = 0.0

    equity_frame = pd.DataFrame({"total": totals}) if summary_only else pd.DataFrame(equity)
    if not summary_only:
        peak = equity_frame["total"].cummax()
        equity_frame["drawdown"] = peak - equity_frame["total"]
        equity_frame["drawdown_pct"] = np.divide(
            equity_frame["drawdown"],
            peak,
            out=np.zeros(len(equity_frame), dtype=float),
            where=peak.to_numpy() != 0,
        )
    trade_frame = pd.DataFrame(trades, columns=_TRADE_COLUMNS)
    position_frame = pd.DataFrame(positions, columns=_POSITION_COLUMNS)
    analyzer = PerformanceAnalyzer(equity_frame, trade_frame)
    return SignalBacktestResult(
        performance=analyzer.compute(),
        equity_curve=equity_frame,
        trades=trade_frame,
        positions=position_frame,
        diagnostic=analyzer.diagnostic,
    )


def _buy_size(
    cash: float,
    price: float,
    config: SignalBacktestConfig,
    profile: TradeProfile,
) -> float:
    if config.position_mode == "fixed":
        requested = config.fixed_size or 0
        return float(
            requested
            if _total_cost(requested, price, profile, config.slippage) <= cash
            else _affordable_size(cash, price, profile, config.slippage)
        )
    return float(_affordable_size(cash, price, profile, config.slippage))


def _total_cost(size: float, price: float, profile: TradeProfile, slippage: float) -> float:
    commission = max(size * price * profile.commission, profile.min_commission)
    return size * price + commission + size * slippage


def _affordable_size(
    cash: float,
    price: float,
    profile: TradeProfile,
    slippage: float,
) -> int:
    size = int(max(cash, 0) / max(price, 1e-12) / profile.lot_size) * profile.lot_size
    while size >= profile.lot_size and _total_cost(size, price, profile, slippage) > cash:
        size -= profile.lot_size
    return size


def _trade(
    execution_date: pd.Timestamp,
    direction: str,
    size: float,
    price: float,
    commission: float,
    stamp_tax: float,
    slippage: float,
    pnl: float,
    cost_basis: float,
    rejected: bool,
    market: str,
    code: str,
    name: str | None,
    signal_index: int,
    signal_date: object,
) -> dict[str, object]:
    return {
        "datetime": execution_date,
        "date": execution_date.strftime("%Y-%m-%d"),
        "signal_date": pd.Timestamp(signal_date).strftime("%Y-%m-%d"),
        "signal_index": signal_index,
        "market": market,
        "code": code,
        "name": name,
        "direction": direction,
        "size": float(size),
        "price": float(price),
        "commission": float(commission),
        "stamp_tax": float(stamp_tax),
        "slippage": float(slippage),
        "pnl": float(pnl),
        "cost_basis": float(cost_basis),
        "rejected": rejected,
    }
