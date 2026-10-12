"""Signal backtest 主循环的 Numba 内核（单标的、单持仓状态机）。

语义与 :func:`aquest.backtest.engine.run_signal_backtest` 的逐 bar 循环
（含 ``close_at_end`` 收官平仓）逐行对齐：同运算顺序、同分支、同记账字段，
由 ``tests/unit/test_fast_signal_engine.py`` 的新旧对拍保证。numba 缺失时
同一个函数按纯 Python 执行——两条路径共用这份实现，不会漂移。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from aquest.jit import njit

if TYPE_CHECKING:  # pragma: no cover - 仅为类型检查
    from collections.abc import Callable

    from aquest.backtest.engine import SignalBacktestConfig
    from aquest.backtest.profiles import TradeProfile

__all__ = ["fast_signal_loop_outputs"]


def _signal_backtest_kernel_impl(
    opens: np.ndarray,
    closes: np.ndarray,
    buys: np.ndarray,
    sells: np.ndarray,
    exec_next_open: bool,
    position_fixed: bool,
    fixed_size: int,
    lot_size: int,
    commission_rate: float,
    min_commission: float,
    stamp_tax_rate: float,
    slippage: float,
    initial_cash: float,
    close_at_end: bool,
) -> tuple[Any, ...]:
    n_bars = opens.shape[0]
    cap = n_bars + 1
    t_bar = np.empty(cap, dtype=np.int64)
    t_dir = np.empty(cap, dtype=np.int8)
    t_size = np.empty(cap, dtype=np.float64)
    t_price = np.empty(cap, dtype=np.float64)
    t_comm = np.empty(cap, dtype=np.float64)
    t_stamp = np.empty(cap, dtype=np.float64)
    t_slip = np.empty(cap, dtype=np.float64)
    t_pnl = np.empty(cap, dtype=np.float64)
    t_cb = np.empty(cap, dtype=np.float64)
    t_sig = np.empty(cap, dtype=np.int64)
    n_trades = 0
    end_closed = 0

    cash = initial_cash
    position = 0.0
    average_cost = 0.0
    # pending_dir: -1 无挂单，0 BUY，1 SELL —— 对应原循环的 pending 元组。
    pending_dir = -1
    pending_sig = -1

    cash_arr = np.empty(n_bars, dtype=np.float64)
    posval_arr = np.empty(n_bars, dtype=np.float64)
    total_arr = np.empty(n_bars, dtype=np.float64)
    pos_arr = np.empty(n_bars, dtype=np.float64)
    avg_arr = np.empty(n_bars, dtype=np.float64)

    for index in range(n_bars):
        if pending_dir >= 0:
            if exec_next_open:
                price = opens[index]
            else:
                price = closes[index]
            if pending_dir == 0:
                # 内联原 _buy_size / _affordable_size：fixed 模式按固定股数，
                # full 模式按可用资金；整手数量从上限往下减。
                cand = int(max(cash, 0.0) / max(price, 1e-12) / lot_size) * lot_size
                while cand >= lot_size:
                    cand_comm = max(cand * price * commission_rate, min_commission)
                    cand_cost = cand * price + cand_comm + cand * slippage
                    if cand_cost <= cash:
                        break
                    cand -= lot_size
                if position_fixed:
                    requested = fixed_size
                    req_comm = max(requested * price * commission_rate, min_commission)
                    req_cost = requested * price + req_comm + requested * slippage
                    if req_cost <= cash:
                        size = float(requested)
                    else:
                        size = float(cand)
                else:
                    size = float(cand)
                if size > 0.0:
                    commission = max(size * price * commission_rate, min_commission)
                    slippage_amt = size * slippage
                    cash -= size * price + commission + slippage_amt
                    if position + size > 0.0:
                        average_cost = (
                            position * average_cost + size * price + commission + slippage_amt
                        ) / (position + size)
                    else:
                        average_cost = 0.0
                    position += size
                    t_bar[n_trades] = index
                    t_dir[n_trades] = 0
                    t_size[n_trades] = size
                    t_price[n_trades] = price
                    t_comm[n_trades] = commission
                    t_stamp[n_trades] = 0.0
                    t_slip[n_trades] = slippage_amt
                    t_pnl[n_trades] = 0.0
                    t_cb[n_trades] = 0.0
                    t_sig[n_trades] = pending_sig
                    n_trades += 1
            else:
                size = position
                if size > 0.0:
                    commission = max(size * price * commission_rate, min_commission)
                    stamp_tax = size * price * stamp_tax_rate
                    slippage_amt = size * slippage
                    cost_basis = average_cost * size
                    pnl = size * price - commission - stamp_tax - slippage_amt - cost_basis
                    cash += size * price - commission - stamp_tax - slippage_amt
                    t_bar[n_trades] = index
                    t_dir[n_trades] = 1
                    t_size[n_trades] = size
                    t_price[n_trades] = price
                    t_comm[n_trades] = commission
                    t_stamp[n_trades] = stamp_tax
                    t_slip[n_trades] = slippage_amt
                    t_pnl[n_trades] = pnl
                    t_cb[n_trades] = cost_basis
                    t_sig[n_trades] = pending_sig
                    n_trades += 1
                    position = 0.0
                    average_cost = 0.0
            pending_dir = -1
            pending_sig = -1

        close = closes[index]
        position_value = position * close
        total = cash + position_value
        cash_arr[index] = cash
        posval_arr[index] = position_value
        total_arr[index] = total
        pos_arr[index] = position
        avg_arr[index] = average_cost

        if index < n_bars - 1:
            if position > 0.0 and sells[index]:
                pending_dir = 1
                pending_sig = index
            elif position <= 0.0 and buys[index]:
                pending_dir = 0
                pending_sig = index

    if close_at_end and position > 0.0:
        # 没有卖出规则时的收官平仓：按最后一根收盘价成交，signal 与成交同日。
        last_index = n_bars - 1
        price = closes[last_index]
        size = position
        commission = max(size * price * commission_rate, min_commission)
        stamp_tax = size * price * stamp_tax_rate
        slippage_amt = size * slippage
        cost_basis = average_cost * size
        pnl = size * price - commission - stamp_tax - slippage_amt - cost_basis
        cash += size * price - commission - stamp_tax - slippage_amt
        t_bar[n_trades] = last_index
        t_dir[n_trades] = 1
        t_size[n_trades] = size
        t_price[n_trades] = price
        t_comm[n_trades] = commission
        t_stamp[n_trades] = stamp_tax
        t_slip[n_trades] = slippage_amt
        t_pnl[n_trades] = pnl
        t_cb[n_trades] = cost_basis
        t_sig[n_trades] = last_index
        n_trades += 1
        position = 0.0
        average_cost = 0.0
        # 最后一帧回到「已平仓」的真实状态：市值归零、现金是扣费后的现金。
        # 注意只调净值（cash/total），持仓明细行保持平仓前的市值，与原实现一致。
        cash_arr[last_index] = cash
        total_arr[last_index] = cash
        end_closed = 1

    return (
        cash,
        position,
        average_cost,
        n_trades,
        end_closed,
        t_bar,
        t_dir,
        t_size,
        t_price,
        t_comm,
        t_stamp,
        t_slip,
        t_pnl,
        t_cb,
        t_sig,
        cash_arr,
        posval_arr,
        total_arr,
        pos_arr,
        avg_arr,
    )


#: 编译后的内核；测试可以把它替换成未包装的纯函数来验证移植逻辑本身。
_SIGNAL_KERNEL: Callable[..., tuple[Any, ...]] | None = None


def _signal_kernel() -> Callable[..., tuple[Any, ...]]:
    global _SIGNAL_KERNEL
    if _SIGNAL_KERNEL is None:
        _SIGNAL_KERNEL = njit(cache=True)(_signal_backtest_kernel_impl)
    return _SIGNAL_KERNEL


def fast_signal_loop_outputs(
    opens: np.ndarray,
    closes: np.ndarray,
    buys: np.ndarray,
    sells: np.ndarray,
    config: SignalBacktestConfig,
    profile: TradeProfile,
    dates: np.ndarray,
    *,
    market: str,
    code: str,
    name: str | None,
    summary_only: bool,
) -> (
    tuple[
        float,
        float,
        float,
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[float],
    ]
    | None
):
    """用内核重跑主循环，返回与原循环相同的四份记账结构。

    返回 ``(cash, position, average_cost, trades, equity, positions, totals)``；
    ``position``/``average_cost`` 供引擎尾部 ``close_at_end`` 分支判断使用。
    参数超出内核支持范围时返回 ``None``，由调用方退回原循环。
    """

    from aquest.backtest.engine import _trade  # 局部导入避免循环依赖
    from aquest.jit import record_outcome

    n_bars = int(opens.shape[0])
    if n_bars == 0 or int(buys.shape[0]) != n_bars or int(sells.shape[0]) != n_bars:
        record_outcome("signal", "length_mismatch")
        return None
    (
        cash,
        position,
        average_cost,
        n_trades,
        end_closed,
        t_bar,
        t_dir,
        t_size,
        t_price,
        t_comm,
        t_stamp,
        t_slip,
        t_pnl,
        t_cb,
        t_sig,
        cash_arr,
        posval_arr,
        total_arr,
        pos_arr,
        avg_arr,
    ) = _signal_kernel()(
        np.ascontiguousarray(opens, dtype=np.float64),
        np.ascontiguousarray(closes, dtype=np.float64),
        np.ascontiguousarray(buys, dtype=np.bool_),
        np.ascontiguousarray(sells, dtype=np.bool_),
        bool(config.execution == "next_open"),
        bool(config.position_mode == "fixed"),
        int(config.fixed_size or 0),
        int(profile.lot_size),
        float(profile.commission),
        float(profile.min_commission),
        float(profile.stamp_tax),
        float(config.slippage),
        float(config.initial_cash),
        bool(config.close_at_end),
    )

    # 日期文本与 Timestamp 一次性向量化生成；逐行 pd.Timestamp+strftime 会把
    # 内核省下的时间全吃回去。结果与逐行构造逐字段一致。
    stamps = pd.to_datetime(dates)
    date_texts = stamps.strftime("%Y-%m-%d")
    stamp_list = list(stamps)

    trades: list[dict[str, Any]] = []
    for i in range(int(n_trades)):
        signal_index = int(t_sig[i])
        execution_date = stamp_list[int(t_bar[i])]
        direction = "BUY" if t_dir[i] == 0 else "SELL"
        trades.append(
            _trade(
                execution_date,
                direction,
                float(t_size[i]),
                float(t_price[i]),
                float(t_comm[i]),
                float(t_stamp[i]),
                float(t_slip[i]),
                float(t_pnl[i]),
                float(t_cb[i]),
                False,
                market,
                code,
                name,
                signal_index,
                dates[signal_index],
            )
        )

    equity: list[dict[str, Any]] = []
    position_rows: list[dict[str, Any]] = []
    totals: list[float] = []
    last_index = n_bars - 1
    for i in range(n_bars):
        # 收官平仓只改净值行的现金与市值；持仓明细行保持平仓前状态。
        closed_here = bool(end_closed) and i == last_index
        equity_position_value = 0.0 if closed_here else float(posval_arr[i])
        if summary_only:
            totals.append(float(total_arr[i]))
        else:
            equity.append(
                {
                    "date": date_texts[i],
                    "datetime": stamp_list[i],
                    "cash": float(cash_arr[i]),
                    "position_value": equity_position_value,
                    "total": float(total_arr[i]),
                }
            )
            position_rows.append(
                {
                    "datetime": stamp_list[i],
                    "size": float(pos_arr[i]),
                    "avg_price": float(avg_arr[i]),
                    "market_value": float(posval_arr[i]),
                    "unrealized_pnl": (float(closes[i]) - float(avg_arr[i])) * float(pos_arr[i]),
                }
            )
    record_outcome("signal", "hit")
    return (
        float(cash),
        float(position),
        float(average_cost),
        trades,
        equity,
        position_rows,
        totals,
    )
