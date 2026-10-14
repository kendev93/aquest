"""组合模拟状态机（``PortfolioSimulationMixin._simulate``）的 Numba 内核。

只服务发现寻优的热路径（``run_prepared_metrics`` → ``include_details=False``、
无检查点、无 fitness 过滤、无中止回调）；其余形态一律退回原 Python 实现。
内核是原逐日循环的逐行移植：同运算顺序、同分支、同记账字段，由
``tests/unit/test_fast_portfolio_sim.py`` 的新旧对拍保证 bit-exact。

numba 缺失时同一份实现按纯 Python 执行（结果一致、只是不提速）。
"""

from __future__ import annotations

import logging
import math
from bisect import bisect_right
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

import numpy as np
import pandas as pd

from aquest.backtest.profiles import profile_from_config
from aquest.jit import NUMBA_AVAILABLE, fast_sim_enabled, njit, record_outcome
from aquest.portfolio_backtest.checkpoint import SimulationCheckpoint
from aquest.portfolio_backtest.risk import _risk_trigger_label
from aquest.portfolio_backtest.simulation_models import (
    _PendingBuy,
    _PendingSell,
    _Position,
)

if TYPE_CHECKING:  # pragma: no cover - 仅为类型检查
    from collections.abc import Callable, Sequence

    from aquest.portfolio_backtest.context import _StockSeries
    from aquest.portfolio_backtest.models import PortfolioBacktestConfig

__all__ = ["try_fast_portfolio_simulation"]

_LOGGER = logging.getLogger(__name__)
_WARNED = False


def _fast_date_text(value: int) -> str:
    """``_date_text`` 的纯字符串等价（合法 8 位 YYYYMMDD 逐字符一致）。"""

    text = str(value)
    return f"{text[:4]}-{text[4:6]}-{text[6:]}"


# 内核输入是 n_symbols × n_days 的稠密矩阵，超限退回 Python 实现。
_MAX_CELLS = 2_000_000
_MAX_TRADE_ROWS = 4_000_000

# 原因编码：0 规则卖出、1 持有到期、2/3/4 风险退出（止损/止盈/移动止盈）、5 排名买入。
_REASON_LABELS = ("卖出条件", "持有到期", "止损", "止盈", "移动止盈", "排名入选")
_REASON_LABEL_SET = frozenset(_REASON_LABELS)
_TRIGGER_NAMES = ("", "", "stop_loss", "take_profit", "trailing_stop", "")


@njit(cache=True)
def _ps_contains(ps_slot: np.ndarray, n_ps: int, slot: int) -> bool:
    for index in range(n_ps):
        if ps_slot[index] == slot:
            return True
    return False


@njit(cache=True)
def _ps_remove_at(
    ps_slot: np.ndarray, ps_sig: np.ndarray, ps_reason: np.ndarray, index: int, n_ps: int
) -> int:
    last = n_ps - 1
    for cursor in range(index, last):
        ps_slot[cursor] = ps_slot[cursor + 1]
        ps_sig[cursor] = ps_sig[cursor + 1]
        ps_reason[cursor] = ps_reason[cursor + 1]
    return last


@njit(cache=True)
def _pos_remove(pos_order: np.ndarray, n_pos: int, slot: int) -> int:
    for index in range(n_pos):
        if pos_order[index] == slot:
            last = n_pos - 1
            for cursor in range(index, last):
                pos_order[cursor] = pos_order[cursor + 1]
            return last
    return n_pos


@njit(cache=True)
def _risk_level_impl(
    base: float,
    is_atr: bool,
    pct: float,
    multiplier: float,
    atr: float,
    atr_ok: bool,
    down: bool,
) -> float:
    """移植 ``risk._risk_level``；NaN 表示「无此风险线」。"""

    if is_atr:
        if math.isnan(multiplier) or not atr_ok:
            return math.nan
        distance = atr * multiplier
    else:
        if math.isnan(pct):
            return math.nan
        distance = base * pct
    if down:
        return base - distance
    return base + distance


@njit(cache=True)
def _risk_check(
    highs_m: np.ndarray,
    lows_m: np.ndarray,
    opens_m: np.ndarray,
    atrs_m: np.ndarray,
    slot: int,
    day: int,
    pos_entry_price: np.ndarray,
    pos_highest: np.ndarray,
    sl_is_atr: np.ndarray,
    sl_pct: np.ndarray,
    sl_mult: np.ndarray,
    sl_cd: np.ndarray,
    tp_is_atr: np.ndarray,
    tp_pct: np.ndarray,
    tp_mult: np.ndarray,
    tp_cd: np.ndarray,
    tr_is_atr: np.ndarray,
    tr_pct: np.ndarray,
    tr_mult: np.ndarray,
    tr_cd: np.ndarray,
) -> tuple[int, float, int]:
    """移植 ``risk._risk_trigger``；返回 (trigger_id, fill_price, cooldown_days)。

    trigger_id：-1 无触发、2 止损、3 止盈、4 移动止盈（判定优先级与原实现一致）。
    与原实现相同，最高价在判定前就地更新（无论是否触发）。
    """

    high = highs_m[slot, day]
    low = lows_m[slot, day]
    open_price = opens_m[slot, day]
    if not (math.isfinite(high) and math.isfinite(low) and math.isfinite(open_price)):
        return -1, 0.0, 0
    if pos_highest[slot] < high:
        pos_highest[slot] = high
    atr = math.nan
    atr_ok = math.isfinite(atrs_m[slot, day])
    if atr_ok:
        atr = atrs_m[slot, day]
    entry = pos_entry_price[slot]
    level = _risk_level_impl(entry, sl_is_atr[slot], sl_pct[slot], sl_mult[slot], atr, atr_ok, True)
    if not math.isnan(level) and low <= level:
        fill = open_price if open_price <= level else level
        return 2, fill, sl_cd[slot]
    level = _risk_level_impl(
        pos_highest[slot], tr_is_atr[slot], tr_pct[slot], tr_mult[slot], atr, atr_ok, True
    )
    if not math.isnan(level) and low <= level:
        fill = open_price if open_price <= level else level
        return 4, fill, tr_cd[slot]
    level = _risk_level_impl(
        entry, tp_is_atr[slot], tp_pct[slot], tp_mult[slot], atr, atr_ok, False
    )
    if not math.isnan(level) and high >= level:
        fill = open_price if open_price >= level else level
        return 3, fill, tp_cd[slot]
    return -1, 0.0, 0


@njit(cache=True)
def _collect_candidate(
    slot: int,
    day: int,
    pos_active: np.ndarray,
    n_ps: int,
    ps_slot: np.ndarray,
    cooldown: np.ndarray,
    g2l: np.ndarray,
    buy_m: np.ndarray,
    rank_values_m: np.ndarray,
    has_priority: np.ndarray,
    priority: np.ndarray,
    e_score: np.ndarray,
    e_slot: np.ndarray,
    n_e: int,
    cap: int,
) -> int:
    """原 ``_select_candidates`` 的单候选过滤；cap<0 表示不设上限。"""

    if cap >= 0 and n_e >= cap:
        return n_e
    if pos_active[slot]:
        return n_e
    if _ps_contains(ps_slot, n_ps, slot):
        return n_e
    if day < cooldown[slot]:
        return n_e
    local = g2l[slot, day]
    if local < 0:
        return n_e
    if not buy_m[slot, day]:
        return n_e
    if has_priority[slot]:
        score = priority[slot]
    else:
        score = rank_values_m[slot, day]
    if not math.isfinite(score):
        return n_e
    e_score[n_e] = score
    e_slot[n_e] = slot
    return n_e + 1


@njit(cache=True)
def _select_candidates_impl(
    current_date: int,
    day: int,
    pos_active: np.ndarray,
    n_pos: int,
    n_ps: int,
    ps_slot: np.ndarray,
    cooldown: np.ndarray,
    g2l: np.ndarray,
    buy_m: np.ndarray,
    rank_values_m: np.ndarray,
    has_priority: np.ndarray,
    priority: np.ndarray,
    rank_desc: bool,
    slots: int,
    slot_budget: float,
    has_ranked: bool,
    ranked_offsets: np.ndarray,
    ranked_flat: np.ndarray,
    sym_sort_rank: np.ndarray,
    e_score: np.ndarray,
    e_slot: np.ndarray,
    taken: np.ndarray,
    pb_slot: np.ndarray,
    pb_sig: np.ndarray,
    pb_budget: np.ndarray,
) -> int:
    """移植 ``_select_candidates``（include_event=False），整体替换 pending_buys。

    选中集合按 key=(-sort_value, symbol) 升序取前 slots 个，与
    ``heapq.nsmallest(slots, candidates, key=_candidate_sort_key)`` 等价
    （symbol 唯一，key 无并列，稳定性不参与）。
    """

    n_symbols = g2l.shape[0]
    n_e = 0
    if has_ranked:
        if slots > 0:
            start = ranked_offsets[day]
            stop = ranked_offsets[day + 1]
            for ri in range(start, stop):
                if n_e >= slots:
                    break
                slot = int(ranked_flat[ri])
                n_e = _collect_candidate(
                    slot,
                    day,
                    pos_active,
                    n_ps,
                    ps_slot,
                    cooldown,
                    g2l,
                    buy_m,
                    rank_values_m,
                    has_priority,
                    priority,
                    e_score,
                    e_slot,
                    n_e,
                    slots,
                )
    else:
        for slot in range(n_symbols):
            n_e = _collect_candidate(
                slot,
                day,
                pos_active,
                n_ps,
                ps_slot,
                cooldown,
                g2l,
                buy_m,
                rank_values_m,
                has_priority,
                priority,
                e_score,
                e_slot,
                n_e,
                -1,
            )
    picks = slots if slots < n_e else n_e
    for j in range(n_e):
        taken[j] = False
    n_pb = 0
    for _pick in range(picks):
        best_j = -1
        best1 = 0.0
        best2 = 0
        for j in range(n_e):
            if taken[j]:
                continue
            score = e_score[j]
            key1 = -score if rank_desc else score
            key2 = sym_sort_rank[e_slot[j]]
            if best_j < 0 or key1 < best1 or (key1 == best1 and key2 < best2):
                best_j = j
                best1 = key1
                best2 = key2
        if best_j < 0:
            break
        taken[best_j] = True
        pb_slot[n_pb] = e_slot[best_j]
        pb_sig[n_pb] = current_date
        pb_budget[n_pb] = slot_budget
        n_pb += 1
    return n_pb


@njit(cache=True)
def _record_trade(  # noqa: PLR0913 - 内核参数即状态
    t_day: np.ndarray,
    t_sig: np.ndarray,
    t_slot: np.ndarray,
    t_dir: np.ndarray,
    t_size: np.ndarray,
    t_price: np.ndarray,
    t_comm: np.ndarray,
    t_stamp: np.ndarray,
    t_slip: np.ndarray,
    t_pnl: np.ndarray,
    t_cb: np.ndarray,
    t_reason: np.ndarray,
    t_risk: np.ndarray,
    t_cd: np.ndarray,
    t_eqb: np.ndarray,
    n_trades: int,
    day: int,
    sig_day: int,
    slot: int,
    direction: int,
    size: float,
    price: float,
    comm: float,
    stamp: float,
    slip: float,
    pnl: float,
    cost_basis: float,
    reason_id: int,
    risk_id: int,
    cooldown_days: int,
    equity_before: float,
) -> int:
    t_day[n_trades] = day
    t_sig[n_trades] = sig_day
    t_slot[n_trades] = slot
    t_dir[n_trades] = direction
    t_size[n_trades] = size
    t_price[n_trades] = price
    t_comm[n_trades] = comm
    t_stamp[n_trades] = stamp
    t_slip[n_trades] = slip
    t_pnl[n_trades] = pnl
    t_cb[n_trades] = cost_basis
    t_reason[n_trades] = reason_id
    t_risk[n_trades] = risk_id
    t_cd[n_trades] = cooldown_days
    t_eqb[n_trades] = equity_before
    return n_trades + 1


def _portfolio_kernel_impl(  # noqa: PLR0913 - 内核参数即预处理后的市场数据
    dates: np.ndarray,
    refresh_due: np.ndarray,
    g2l: np.ndarray,
    opens_m: np.ndarray,
    highs_m: np.ndarray,
    lows_m: np.ndarray,
    closes_m: np.ndarray,
    atrs_m: np.ndarray,
    buy_m: np.ndarray,
    sell_m: np.ndarray,
    rank_values_m: np.ndarray,
    has_priority: np.ndarray,
    priority: np.ndarray,
    rank_desc: bool,
    exec_next_open: bool,
    position_fixed: bool,
    fixed_size: int,
    slippage: float,
    initial_cash: float,
    max_positions: int,
    max_hold_days: int,
    single_security: bool,
    prof_comm: np.ndarray,
    prof_min_comm: np.ndarray,
    prof_stamp: np.ndarray,
    prof_lot: np.ndarray,
    slot_by_rank: np.ndarray,
    sym_sort_rank: np.ndarray,
    sl_is_atr: np.ndarray,
    sl_pct: np.ndarray,
    sl_mult: np.ndarray,
    sl_cd: np.ndarray,
    tp_is_atr: np.ndarray,
    tp_pct: np.ndarray,
    tp_mult: np.ndarray,
    tp_cd: np.ndarray,
    tr_is_atr: np.ndarray,
    tr_pct: np.ndarray,
    tr_mult: np.ndarray,
    tr_cd: np.ndarray,
    act_offsets: np.ndarray,
    act_date: np.ndarray,
    act_cash: np.ndarray,
    act_ratio: np.ndarray,
    act_neutral: np.ndarray,
    has_ranked: bool,
    ranked_offsets: np.ndarray,
    ranked_flat: np.ndarray,
    resume_start: int,
    init_pos_active: np.ndarray,
    init_pos_size: np.ndarray,
    init_pos_entry_price: np.ndarray,
    init_pos_entry_date: np.ndarray,
    init_pos_highest: np.ndarray,
    init_pos_buy_comm: np.ndarray,
    init_pos_buy_slip: np.ndarray,
    init_pos_entry_local: np.ndarray,
    init_pos_order: np.ndarray,
    init_n_pos: int,
    init_ps_slot: np.ndarray,
    init_ps_sig: np.ndarray,
    init_ps_reason: np.ndarray,
    init_n_ps: int,
    init_pb_slot: np.ndarray,
    init_pb_sig: np.ndarray,
    init_pb_budget: np.ndarray,
    init_n_pb: int,
    init_cooldown: np.ndarray,
    init_last_price: np.ndarray,
    init_last_seen: np.ndarray,
    init_applied: np.ndarray,
    init_cash: float,
    init_peak: float,
    init_drawdown: float,
) -> tuple[Any, ...]:
    n_days = dates.shape[0]
    n_symbols = g2l.shape[0]

    # ---- 持仓状态（dict 语义：pos_order 保持插入序，pop 后其余元素次序不变）。
    # resume_start > 0 时由包装层从检查点恢复，字段语义与原 `_simulate` 的
    # checkpoint 分支逐项一致；resume_start == 0 时初始数组全空，等价从头跑。
    if resume_start > 0:
        pos_active = init_pos_active
        pos_size = init_pos_size
        pos_entry_price = init_pos_entry_price
        pos_entry_date = init_pos_entry_date
        pos_highest = init_pos_highest
        pos_buy_comm = init_pos_buy_comm
        pos_buy_slip = init_pos_buy_slip
        pos_entry_local = init_pos_entry_local
        pos_order = init_pos_order
        n_pos = init_n_pos
        ps_slot = init_ps_slot
        ps_sig = init_ps_sig
        ps_reason = init_ps_reason
        n_ps = init_n_ps
        pb_slot = init_pb_slot
        pb_sig = init_pb_sig
        pb_budget = init_pb_budget
        n_pb = init_n_pb
        cooldown = init_cooldown
        last_price = init_last_price
        last_seen = init_last_seen
        applied = init_applied
        peak_value = init_peak
        max_drawdown = init_drawdown
    else:
        pos_active = np.zeros(n_symbols, dtype=np.bool_)
        pos_size = np.zeros(n_symbols, dtype=np.float64)
        pos_entry_price = np.zeros(n_symbols, dtype=np.float64)
        pos_entry_date = np.zeros(n_symbols, dtype=np.int64)
        pos_highest = np.zeros(n_symbols, dtype=np.float64)
        pos_buy_comm = np.zeros(n_symbols, dtype=np.float64)
        pos_buy_slip = np.zeros(n_symbols, dtype=np.float64)
        pos_entry_local = np.zeros(n_symbols, dtype=np.int64)
        pos_order = np.zeros(max_positions, dtype=np.int64)
        n_pos = 0

        # ---- 挂单与冷却（均保持插入序）
        ps_slot = np.zeros(max_positions, dtype=np.int64)
        ps_sig = np.zeros(max_positions, dtype=np.int64)
        ps_reason = np.zeros(max_positions, dtype=np.int8)
        n_ps = 0
        pb_slot = np.zeros(max_positions, dtype=np.int64)
        pb_sig = np.zeros(max_positions, dtype=np.int64)
        pb_budget = np.zeros(max_positions, dtype=np.float64)
        n_pb = 0
        cooldown = np.full(n_symbols, -1, dtype=np.int64)
        last_price = np.full(n_symbols, math.nan)
        last_seen = np.zeros(n_symbols, dtype=np.bool_)
        applied = np.zeros(act_offsets[n_symbols], dtype=np.bool_)

        peak_value = initial_cash
        max_drawdown = 0.0

    # ---- 输出缓冲
    eq_cash = np.zeros(n_days, dtype=np.float64)
    eq_posval = np.zeros(n_days, dtype=np.float64)
    eq_total = np.zeros(n_days, dtype=np.float64)
    eq_count = np.zeros(n_days, dtype=np.int64)
    trade_cap = n_days * (2 * max_positions + 1) + 8
    t_day = np.zeros(trade_cap, dtype=np.int64)
    t_sig = np.zeros(trade_cap, dtype=np.int64)
    t_slot = np.zeros(trade_cap, dtype=np.int64)
    t_dir = np.zeros(trade_cap, dtype=np.int8)
    t_size = np.zeros(trade_cap, dtype=np.float64)
    t_price = np.zeros(trade_cap, dtype=np.float64)
    t_comm = np.zeros(trade_cap, dtype=np.float64)
    t_stamp = np.zeros(trade_cap, dtype=np.float64)
    t_slip = np.zeros(trade_cap, dtype=np.float64)
    t_pnl = np.zeros(trade_cap, dtype=np.float64)
    t_cb = np.zeros(trade_cap, dtype=np.float64)
    t_reason = np.zeros(trade_cap, dtype=np.int8)
    t_risk = np.zeros(trade_cap, dtype=np.int8)
    t_cd = np.zeros(trade_cap, dtype=np.int64)
    t_eqb = np.zeros(trade_cap, dtype=np.float64)
    n_trades = 0
    e_score = np.zeros(n_symbols, dtype=np.float64)
    e_slot = np.zeros(n_symbols, dtype=np.int64)
    taken = np.zeros(n_symbols, dtype=np.bool_)

    nan = math.nan
    cash = init_cash if resume_start > 0 else initial_cash

    for day in range(resume_start, n_days):
        current_date = dates[day]

        # ---- 除权除息（原 _apply_corporate_actions：按持仓插入序，事件按日期序）
        for oi in range(n_pos):
            slot = pos_order[oi]
            if pos_size[slot] <= 0.0:
                continue
            local = g2l[slot, day]
            if local < 0:
                continue
            for ai in range(act_offsets[slot], act_offsets[slot + 1]):
                if act_neutral[ai]:
                    continue
                action_date = act_date[ai]
                if action_date <= pos_entry_date[slot] or action_date > current_date:
                    continue
                if applied[ai]:
                    continue
                ratio = 1.0 + act_ratio[ai]
                if ratio <= 0.0:
                    continue
                cash += pos_size[slot] * act_cash[ai]
                pos_size[slot] *= ratio
                pos_entry_price[slot] = (pos_entry_price[slot] - act_cash[ai]) / ratio
                pos_highest[slot] = (pos_highest[slot] - act_cash[ai]) / ratio
                applied[ai] = True

        # ---- 规则卖出（原 _execute_sells：按 pending_sells 插入序逐个成交）
        k = 0
        while k < n_ps:
            slot = ps_slot[k]
            sig_day = ps_sig[k]
            reason_id = ps_reason[k]
            if not pos_active[slot]:
                k += 1
                continue
            local = g2l[slot, day]
            if local < 0:
                k += 1
                continue
            if exec_next_open:
                price = opens_m[slot, day]
            else:
                price = closes_m[slot, day]
            size = pos_size[slot]
            comm = max(size * price * prof_comm[slot], prof_min_comm[slot])
            stamp = size * price * prof_stamp[slot]
            slip = size * slippage
            proceeds = size * price - comm - stamp - slip
            cash += proceeds
            pnl = (
                (price - pos_entry_price[slot]) * size
                - pos_buy_comm[slot]
                - pos_buy_slip[slot]
                - comm
                - stamp
                - slip
            )
            cost_basis = pos_entry_price[slot] * size + pos_buy_comm[slot] + pos_buy_slip[slot]
            n_trades = _record_trade(
                t_day,
                t_sig,
                t_slot,
                t_dir,
                t_size,
                t_price,
                t_comm,
                t_stamp,
                t_slip,
                t_pnl,
                t_cb,
                t_reason,
                t_risk,
                t_cd,
                t_eqb,
                n_trades,
                day,
                sig_day,
                slot,
                1,
                size,
                price,
                comm,
                stamp,
                slip,
                pnl,
                cost_basis,
                reason_id,
                -1,
                -1,
                nan,
            )
            pos_active[slot] = False
            n_pos = _pos_remove(pos_order, n_pos, slot)
            n_ps = _ps_remove_at(ps_slot, ps_sig, ps_reason, k, n_ps)

        # ---- 风险卖出（原 _execute_risk_sells：按 symbol 字典序，当日 OHLC 触发）
        for rank_i in range(n_symbols):
            slot = slot_by_rank[rank_i]
            if not pos_active[slot]:
                continue
            if _ps_contains(ps_slot, n_ps, slot):
                continue
            local = g2l[slot, day]
            if local < 0:
                continue
            trigger_id, fill, cooldown_days = _risk_check(
                highs_m,
                lows_m,
                opens_m,
                atrs_m,
                slot,
                day,
                pos_entry_price,
                pos_highest,
                sl_is_atr,
                sl_pct,
                sl_mult,
                sl_cd,
                tp_is_atr,
                tp_pct,
                tp_mult,
                tp_cd,
                tr_is_atr,
                tr_pct,
                tr_mult,
                tr_cd,
            )
            if trigger_id < 0:
                continue
            price = fill
            size = pos_size[slot]
            comm = max(size * price * prof_comm[slot], prof_min_comm[slot])
            stamp = size * price * prof_stamp[slot]
            slip = size * slippage
            proceeds = size * price - comm - stamp - slip
            cash += proceeds
            pnl = (
                (price - pos_entry_price[slot]) * size
                - pos_buy_comm[slot]
                - pos_buy_slip[slot]
                - comm
                - stamp
                - slip
            )
            cost_basis = pos_entry_price[slot] * size + pos_buy_comm[slot] + pos_buy_slip[slot]
            n_trades = _record_trade(
                t_day,
                t_sig,
                t_slot,
                t_dir,
                t_size,
                t_price,
                t_comm,
                t_stamp,
                t_slip,
                t_pnl,
                t_cb,
                t_reason,
                t_risk,
                t_cd,
                t_eqb,
                n_trades,
                day,
                current_date,
                slot,
                1,
                size,
                price,
                comm,
                stamp,
                slip,
                pnl,
                cost_basis,
                trigger_id,
                trigger_id,
                cooldown_days,
                nan,
            )
            pos_active[slot] = False
            n_pos = _pos_remove(pos_order, n_pos, slot)
            if cooldown_days > 0:
                cooldown[slot] = day + cooldown_days + 1

        # ---- 买入（原 _execute_buys：按 pending_buys 顺序，槽位满即停）
        for k in range(n_pb):
            if n_pos >= max_positions:
                break
            slot = pb_slot[k]
            if pos_active[slot]:
                continue
            if _ps_contains(ps_slot, n_ps, slot):
                continue
            if day < cooldown[slot]:
                continue
            local = g2l[slot, day]
            if local < 0:
                continue
            if exec_next_open:
                price = opens_m[slot, day]
            else:
                price = closes_m[slot, day]
            if price <= 0.0:
                continue
            lot = prof_lot[slot]
            # 原 _planned_shares：fixed 按固定股数，full 按 slot 预算；再按可负担下调。
            if position_fixed:
                shares = fixed_size // lot * lot
                if shares < 0:
                    shares = 0
            else:
                budget = pb_budget[k]
                if budget < 0.0:
                    budget = 0.0
                shares = int(budget / price / lot) * lot
            while shares >= lot:
                s_comm = max(shares * price * prof_comm[slot], prof_min_comm[slot])
                s_cost = shares * price + s_comm + shares * slippage
                if s_cost <= cash:
                    break
                shares -= lot
            if shares <= 0:
                continue
            comm = max(shares * price * prof_comm[slot], prof_min_comm[slot])
            slip = shares * slippage
            equity_before = cash
            cash -= shares * price + comm + slip
            pos_active[slot] = True
            pos_size[slot] = float(shares)
            pos_entry_price[slot] = price
            pos_entry_date[slot] = current_date
            pos_highest[slot] = price
            pos_buy_comm[slot] = comm
            pos_buy_slip[slot] = slip
            pos_entry_local[slot] = local
            for ai in range(act_offsets[slot], act_offsets[slot + 1]):
                applied[ai] = False
            pos_order[n_pos] = slot
            n_pos += 1
            if single_security:
                equity_before_out = equity_before
            else:
                equity_before_out = nan
            n_trades = _record_trade(
                t_day,
                t_sig,
                t_slot,
                t_dir,
                t_size,
                t_price,
                t_comm,
                t_stamp,
                t_slip,
                t_pnl,
                t_cb,
                t_reason,
                t_risk,
                t_cd,
                t_eqb,
                n_trades,
                day,
                pb_sig[k],
                slot,
                0,
                float(shares),
                price,
                comm,
                0.0,
                slip,
                0.0,
                0.0,
                5,
                -1,
                -1,
                equity_before_out,
            )

        # ---- 清理失效买入单（原 pending_buys 列表推导，保序压缩）
        w = 0
        for k in range(n_pb):
            slot = pb_slot[k]
            if pos_active[slot]:
                continue
            if _ps_contains(ps_slot, n_ps, slot):
                continue
            if day < cooldown[slot]:
                continue
            if pb_sig[k] < current_date:
                continue
            pb_slot[w] = pb_slot[k]
            pb_sig[w] = pb_sig[k]
            pb_budget[w] = pb_budget[k]
            w += 1
        n_pb = w

        # ---- 收盘价更新（原 last_prices 循环）
        for oi in range(n_pos):
            slot = pos_order[oi]
            local = g2l[slot, day]
            if local >= 0:
                last_price[slot] = closes_m[slot, day]
                last_seen[slot] = True

        # ---- 组合估值与回撤（原 _portfolio_value：按持仓插入序累加）
        position_value = 0.0
        for oi in range(n_pos):
            slot = pos_order[oi]
            if last_seen[slot]:
                price_ref = last_price[slot]
            else:
                price_ref = pos_entry_price[slot]
            position_value += pos_size[slot] * price_ref
        total_value = cash + position_value
        if total_value > peak_value:
            peak_value = total_value
        if peak_value > 0.0:
            current_drawdown = (peak_value - total_value) / peak_value
        else:
            current_drawdown = 0.0
        if current_drawdown > max_drawdown:
            max_drawdown = current_drawdown
        eq_cash[day] = cash
        eq_posval[day] = position_value
        eq_total[day] = total_value
        eq_count[day] = n_pos

        # ---- 排卖出单（原 _schedule_exits：按持仓插入序）
        new_exit = False
        for oi in range(n_pos):
            slot = pos_order[oi]
            if _ps_contains(ps_slot, n_ps, slot):
                continue
            local = g2l[slot, day]
            if local < 0:
                continue
            if sell_m[slot, day]:
                ps_slot[n_ps] = slot
                ps_sig[n_ps] = current_date
                ps_reason[n_ps] = 0
                n_ps += 1
                new_exit = True
                continue
            if max_hold_days > 0 and local - pos_entry_local[slot] + 1 >= max_hold_days:
                ps_slot[n_ps] = slot
                ps_sig[n_ps] = current_date
                ps_reason[n_ps] = 1
                n_ps += 1
                new_exit = True

        # ---- 调仓/新卖出触发选股（原 _select_candidates，整体替换 pending_buys）
        if refresh_due[day] or new_exit:
            slots = max_positions - n_pos + n_ps
            if slots < 0:
                slots = 0
            if max_positions > 0:
                slot_budget = total_value / max_positions
            else:
                slot_budget = cash
            n_pb = _select_candidates_impl(
                current_date,
                day,
                pos_active,
                n_pos,
                n_ps,
                ps_slot,
                cooldown,
                g2l,
                buy_m,
                rank_values_m,
                has_priority,
                priority,
                rank_desc,
                slots,
                slot_budget,
                has_ranked,
                ranked_offsets,
                ranked_flat,
                sym_sort_rank,
                e_score,
                e_slot,
                taken,
                pb_slot,
                pb_sig,
                pb_budget,
            )

    return (
        cash,
        peak_value,
        max_drawdown,
        n_pos,
        pos_order,
        pos_active,
        pos_size,
        pos_entry_price,
        pos_entry_date,
        pos_highest,
        pos_buy_comm,
        pos_buy_slip,
        pos_entry_local,
        applied,
        n_ps,
        ps_slot,
        ps_sig,
        ps_reason,
        n_pb,
        pb_slot,
        pb_sig,
        pb_budget,
        cooldown,
        last_price,
        last_seen,
        n_trades,
        t_day,
        t_sig,
        t_slot,
        t_dir,
        t_size,
        t_price,
        t_comm,
        t_stamp,
        t_slip,
        t_pnl,
        t_cb,
        t_reason,
        t_risk,
        t_cd,
        t_eqb,
        eq_cash,
        eq_posval,
        eq_total,
        eq_count,
    )


#: 编译后的内核；测试可以替换成未包装的纯函数来验证移植逻辑本身。
_PORTFOLIO_KERNEL: Callable[..., tuple[Any, ...]] | None = None


def _portfolio_kernel() -> Callable[..., tuple[Any, ...]]:
    global _PORTFOLIO_KERNEL
    if _PORTFOLIO_KERNEL is None:
        _PORTFOLIO_KERNEL = njit(cache=True)(_portfolio_kernel_impl)
    return _PORTFOLIO_KERNEL


def _action_date_int(value: Any) -> int:
    return int(value.year) * 10000 + int(value.month) * 100 + int(value.day)


def _action_key(action: Any) -> str:
    """与原 ``_apply_corporate_actions`` 完全相同的幂等键格式。"""

    return (
        f"{_action_date_int(action.ex_date)}:{action.share_ratio:.17g}:"
        f"{action.cash_per_share:.17g}:{action.factor_ratio:.17g}:{action.action_type}"
    )


def _risk_override(context: _StockSeries, config: PortfolioBacktestConfig, name: str) -> Any:
    strategy = context.strategy_config
    if name in strategy:
        return strategy[name]
    return getattr(config, name)


def _optional_float(value: Any) -> float:
    return math.nan if value is None else float(value)


def _refresh_due_array(dates_arr: np.ndarray, frequency: str) -> np.ndarray:
    """移植 ``_should_refresh``：首日/末日恒真，daily 恒真，周/月按日历比较。"""

    n = dates_arr.shape[0]
    refresh = np.zeros(n, dtype=np.bool_)
    if n == 0 or frequency == "daily":
        refresh[:] = True
        return refresh
    refresh[0] = True
    refresh[n - 1] = True
    stamps = pd.to_datetime(pd.Series([str(int(d)) for d in dates_arr]), format="%Y%m%d")
    if frequency == "weekly":
        weeks = stamps.dt.isocalendar().week.to_numpy()
        refresh[:-1] |= weeks[:-1] != weeks[1:]
    else:
        years = stamps.dt.year.to_numpy()
        months = stamps.dt.month.to_numpy()
        refresh[:-1] |= (years[:-1] != years[1:]) | (months[:-1] != months[1:])
    return refresh


def _ranked_csr(
    ranked_candidate_index: Any, dates_arr: np.ndarray, cache: dict[str, Any]
) -> tuple[np.ndarray, np.ndarray]:
    cached = cache.get("ranked")
    if cached is not None and cached[0] is ranked_candidate_index:
        return cached[1], cached[2]
    n_days = dates_arr.shape[0]
    offsets = np.zeros(n_days + 1, dtype=np.int64)
    flat: list[int] = []
    for day in range(n_days):
        entries = ranked_candidate_index.get(int(dates_arr[day]), ())
        flat.extend(int(value) for value in entries)
        offsets[day + 1] = len(flat)
    if len(cache) > 8:
        cache.clear()
    flat_arr = np.asarray(flat, dtype=np.int32)
    cache["ranked"] = (ranked_candidate_index, offsets, flat_arr)
    return offsets, flat_arr


def _build_inputs(
    service: Any,
    contexts: Sequence[_StockSeries],
    dates: list[int],
    config: PortfolioBacktestConfig,
    data_fingerprint: str,
    ranked_candidate_index: Any,
    checkpoint: SimulationCheckpoint | None = None,
    resume_start: int = 0,
) -> tuple[tuple[Any, ...], list[list[str]], np.ndarray] | None:
    """把上下文摊平成内核矩阵；返回 None 表示形态超出内核支持范围。

    ``checkpoint`` 非 None 时按原 ``_simulate`` 的恢复分支构造初始状态数组；
    任何无法无损还原的字段（如拼接的卖出原因、未知标的）都返回 None 交回原实现，
    由原实现抛出同样的校验错误或按 Python 语义继续。
    """

    n_symbols = len(contexts)
    n_days = len(dates)
    symbols = [context.symbol for context in contexts]
    if len(set(symbols)) != n_symbols:
        return None

    dates_arr = np.asarray(dates, dtype=np.int64)
    if n_days > 1 and not bool(np.all(np.diff(dates_arr) > 0)):
        return None

    # 调仓日数组按 (行情指纹, 频率, 日期轴) 缓存——同一份快照、同一份日历才复用。
    # 日期轴必须进键：train/validation/test 用的是同一个整帧指纹，切的却是各自的日期
    # 区间，而 weekly/monthly 的调仓日取决于真实星期与月界。只按天数缓存时，两段天数
    # 相等就会让后跑的那段直接复用另一段日历的掩码，报出来的分段收益根本不是那一段的。
    cache = getattr(service, "_fast_sim_cache", None)
    if cache is None:
        cache = {}
        service._fast_sim_cache = cache
    refresh_key = ("refresh", data_fingerprint, config.rebalance_frequency, dates_arr.tobytes())
    refresh_due = cache.get(refresh_key)
    if not isinstance(refresh_due, np.ndarray):
        refresh_due = _refresh_due_array(dates_arr, config.rebalance_frequency)
        if len(cache) > 8:
            cache.clear()
        cache[refresh_key] = refresh_due

    g2l = np.full((n_symbols, n_days), -1, dtype=np.int32)
    opens_m = np.full((n_symbols, n_days), math.nan)
    highs_m = np.full((n_symbols, n_days), math.nan)
    lows_m = np.full((n_symbols, n_days), math.nan)
    closes_m = np.full((n_symbols, n_days), math.nan)
    atrs_m = np.full((n_symbols, n_days), math.nan)
    buy_m = np.zeros((n_symbols, n_days), dtype=np.bool_)
    sell_m = np.zeros((n_symbols, n_days), dtype=np.bool_)
    rank_values_m = np.full((n_symbols, n_days), math.nan)

    for s, context in enumerate(contexts):
        ctx_dates = np.asarray(context.dates, dtype=np.int64)
        pos = np.searchsorted(ctx_dates, dates_arr)
        clipped = np.minimum(pos, len(ctx_dates) - 1)
        valid = (pos < len(ctx_dates)) & (ctx_dates[clipped] == dates_arr)
        idx = clipped[valid]
        opens_m[s, valid] = np.asarray(context.opens, dtype=np.float64)[idx]
        highs_m[s, valid] = np.asarray(context.highs, dtype=np.float64)[idx]
        lows_m[s, valid] = np.asarray(context.lows, dtype=np.float64)[idx]
        closes_m[s, valid] = np.asarray(context.closes, dtype=np.float64)[idx]
        atrs_m[s, valid] = np.asarray(context.atrs, dtype=np.float64)[idx]
        buy_m[s, valid] = np.asarray(context.buy_mask, dtype=np.bool_)[idx]
        sell_m[s, valid] = np.asarray(context.sell_mask, dtype=np.bool_)[idx]
        ranking = context.values.get(config.ranking_value)
        if ranking is not None:
            rank_values_m[s, valid] = np.asarray(ranking, dtype=float)[idx]
        g2l[s, valid] = idx

    prof_comm = np.zeros(n_symbols, dtype=np.float64)
    prof_min_comm = np.zeros(n_symbols, dtype=np.float64)
    prof_stamp = np.zeros(n_symbols, dtype=np.float64)
    prof_lot = np.zeros(n_symbols, dtype=np.int64)
    has_priority = np.zeros(n_symbols, dtype=np.bool_)
    priority = np.zeros(n_symbols, dtype=np.float64)
    sl_is_atr = np.zeros(n_symbols, dtype=np.bool_)
    sl_pct = np.full(n_symbols, math.nan)
    sl_mult = np.full(n_symbols, math.nan)
    sl_cd = np.zeros(n_symbols, dtype=np.int64)
    tp_is_atr = np.zeros(n_symbols, dtype=np.bool_)
    tp_pct = np.full(n_symbols, math.nan)
    tp_mult = np.full(n_symbols, math.nan)
    tp_cd = np.zeros(n_symbols, dtype=np.int64)
    tr_is_atr = np.zeros(n_symbols, dtype=np.bool_)
    tr_pct = np.full(n_symbols, math.nan)
    tr_mult = np.full(n_symbols, math.nan)
    tr_cd = np.zeros(n_symbols, dtype=np.int64)
    for s, context in enumerate(contexts):
        profile = profile_from_config(
            context.instrument_type,
            commission=config.commission,
            min_commission=config.min_commission,
            stamp_tax=config.stamp_tax,
        )
        prof_comm[s] = profile.commission
        prof_min_comm[s] = profile.min_commission
        prof_stamp[s] = profile.stamp_tax
        prof_lot[s] = profile.lot_size
        score = context.priority_score
        if score is not None:
            has_priority[s] = True
            priority[s] = float(score)
        sl_is_atr[s] = _risk_override(context, config, "stop_loss_mode") == "atr"
        sl_pct[s] = _optional_float(_risk_override(context, config, "stop_loss_pct"))
        sl_mult[s] = _optional_float(_risk_override(context, config, "stop_loss_atr_multiplier"))
        sl_cd[s] = int(_risk_override(context, config, "stop_loss_cooldown_days") or 0)
        tp_is_atr[s] = _risk_override(context, config, "take_profit_mode") == "atr"
        tp_pct[s] = _optional_float(_risk_override(context, config, "take_profit_pct"))
        tp_mult[s] = _optional_float(_risk_override(context, config, "take_profit_atr_multiplier"))
        tp_cd[s] = int(_risk_override(context, config, "take_profit_cooldown_days") or 0)
        tr_is_atr[s] = _risk_override(context, config, "trailing_stop_mode") == "atr"
        tr_pct[s] = _optional_float(_risk_override(context, config, "trailing_stop_pct"))
        tr_mult[s] = _optional_float(
            _risk_override(context, config, "trailing_stop_atr_multiplier")
        )
        tr_cd[s] = int(_risk_override(context, config, "trailing_stop_cooldown_days") or 0)

    order = sorted(range(n_symbols), key=lambda s: symbols[s])
    slot_by_rank = np.asarray(order, dtype=np.int64)
    sym_sort_rank = np.zeros(n_symbols, dtype=np.int64)
    for rank, slot in enumerate(order):
        sym_sort_rank[slot] = rank

    # 除权事件 CSR：按日期稳定排序 + 幂等键去重（与原实现的 applied 集合语义一致）。
    act_offsets = np.zeros(n_symbols + 1, dtype=np.int64)
    act_date_rows: list[int] = []
    act_cash_rows: list[float] = []
    act_ratio_rows: list[float] = []
    act_neutral_rows: list[bool] = []
    action_keys: list[list[str]] = []
    for s, context in enumerate(contexts):
        actions = service._corporate_actions_for_symbol(context)
        # 排序键必须是 ex_date（原实现 simulation.py 同款），传整个事件会取不到 .year。
        ordered = sorted(actions, key=lambda item: _action_date_int(item.ex_date))
        keys: list[str] = []
        seen: set[str] = set()
        for action in ordered:
            key = _action_key(action)
            if key in seen:
                continue
            seen.add(key)
            act_date_rows.append(_action_date_int(action.ex_date))
            act_cash_rows.append(float(action.cash_per_share))
            act_ratio_rows.append(float(action.share_ratio))
            act_neutral_rows.append(bool(action.is_neutral))
            keys.append(key)
        action_keys.append(keys)
        act_offsets[s + 1] = len(act_date_rows)
    act_date = np.asarray(act_date_rows, dtype=np.int64)
    act_cash = np.asarray(act_cash_rows, dtype=np.float64)
    act_ratio = np.asarray(act_ratio_rows, dtype=np.float64)
    act_neutral = np.asarray(act_neutral_rows, dtype=np.bool_)

    has_ranked = ranked_candidate_index is not None
    if has_ranked:
        ranked_offsets, ranked_flat = _ranked_csr(ranked_candidate_index, dates_arr, cache)
    else:
        ranked_offsets = np.zeros(n_days + 1, dtype=np.int64)
        ranked_flat = np.zeros(0, dtype=np.int32)

    # ---- 检查点续跑初始状态（resume_start == 0 时全空，内核走从头分支）。
    # 字段与原 `_simulate` 的 checkpoint 恢复分支一一对应：持仓插入序 =
    # checkpoint.positions 的键序，挂单/冷却/最新价同理。
    symbol_slots = {symbol: s for s, symbol in enumerate(symbols)}
    max_pos = int(config.max_positions)
    init_pos_active = np.zeros(n_symbols, dtype=np.bool_)
    init_pos_size = np.zeros(n_symbols, dtype=np.float64)
    init_pos_entry_price = np.zeros(n_symbols, dtype=np.float64)
    init_pos_entry_date = np.zeros(n_symbols, dtype=np.int64)
    init_pos_highest = np.zeros(n_symbols, dtype=np.float64)
    init_pos_buy_comm = np.zeros(n_symbols, dtype=np.float64)
    init_pos_buy_slip = np.zeros(n_symbols, dtype=np.float64)
    init_pos_entry_local = np.zeros(n_symbols, dtype=np.int64)
    init_pos_order = np.full(max_pos, -1, dtype=np.int64)
    init_ps_slot = np.zeros(max_pos, dtype=np.int64)
    init_ps_sig = np.zeros(max_pos, dtype=np.int64)
    init_ps_reason = np.zeros(max_pos, dtype=np.int8)
    init_pb_slot = np.zeros(max_pos, dtype=np.int64)
    init_pb_sig = np.zeros(max_pos, dtype=np.int64)
    init_pb_budget = np.zeros(max_pos, dtype=np.float64)
    init_cooldown = np.full(n_symbols, -1, dtype=np.int64)
    init_last_price = np.full(n_symbols, math.nan)
    init_last_seen = np.zeros(n_symbols, dtype=np.bool_)
    init_n_pos = 0
    init_n_ps = 0
    init_n_pb = 0
    resuming = checkpoint is not None and resume_start > 0
    if resuming:
        assert checkpoint is not None  # for type checkers; resuming 已保证
        known = set(symbols)
        if any(symbol not in known for symbol in checkpoint.positions):
            return None
        if any(symbol not in known for symbol in checkpoint.pending_sells):
            return None
        if any(pending.symbol not in known for pending in checkpoint.pending_buys):
            return None
        for symbol, position in checkpoint.positions.items():
            s = symbol_slots[symbol]
            day_idx = int(np.searchsorted(dates_arr, position.entry_date))
            if day_idx >= n_days or int(dates_arr[day_idx]) != int(position.entry_date):
                return None
            init_pos_active[s] = True
            init_pos_size[s] = float(position.size)
            init_pos_entry_price[s] = float(position.entry_price)
            init_pos_entry_date[s] = int(position.entry_date)
            init_pos_highest[s] = float(position.highest_price)
            init_pos_buy_comm[s] = float(position.buy_commission)
            init_pos_buy_slip[s] = float(position.buy_slippage)
            init_pos_entry_local[s] = int(g2l[s, day_idx])
            init_pos_order[init_n_pos] = s
            init_n_pos += 1
        for symbol, pending in checkpoint.pending_sells.items():
            # 内核用 int8 编码原因；拼接原因（如「止损、移动止盈」）无法无损还原，
            # 交回原实现按字符串语义继续。
            if pending.reason not in _REASON_LABEL_SET:
                return None
            init_ps_slot[init_n_ps] = symbol_slots[symbol]
            init_ps_sig[init_n_ps] = int(pending.signal_date)
            init_ps_reason[init_n_ps] = _REASON_LABELS.index(pending.reason)
            init_n_ps += 1
        for buy in checkpoint.pending_buys:
            init_pb_slot[init_n_pb] = symbol_slots[buy.symbol]
            init_pb_sig[init_n_pb] = int(buy.signal_date)
            init_pb_budget[init_n_pb] = float(buy.budget)
            init_n_pb += 1
        for s, context in enumerate(contexts):
            if context.symbol in checkpoint.cooldown_until:
                init_cooldown[s] = int(checkpoint.cooldown_until[context.symbol])
            if context.symbol in checkpoint.last_prices:
                init_last_seen[s] = True
                init_last_price[s] = float(checkpoint.last_prices[context.symbol])
    init_applied = np.zeros(int(act_offsets[n_symbols]), dtype=np.bool_)
    if resuming:
        assert checkpoint is not None
        # 已应用除权必须逐键还原：否则续跑会对前缀里已处理过的事件重复除权。
        for s, context in enumerate(contexts):
            resumed_position = checkpoint.positions.get(context.symbol)
            if resumed_position is None or not resumed_position.applied_corporate_actions:
                continue
            keys = action_keys[s]
            if not resumed_position.applied_corporate_actions.issubset(keys):
                return None
            for ai, key in enumerate(keys):
                if key in resumed_position.applied_corporate_actions:
                    init_applied[int(act_offsets[s]) + ai] = True

    # mypy 无法经由 resuming 布尔量推出 checkpoint 非 None；先取一次窄化别名。
    resumed_checkpoint = checkpoint if resuming else None
    inputs = (
        dates_arr,
        refresh_due,
        g2l,
        opens_m,
        highs_m,
        lows_m,
        closes_m,
        atrs_m,
        buy_m,
        sell_m,
        rank_values_m,
        has_priority,
        priority,
        config.rank_order == "desc",
        config.execution == "next_open",
        config.position_mode == "fixed",
        int(config.fixed_size or 0),
        float(config.slippage),
        float(config.initial_cash),
        int(config.max_positions),
        int(config.max_hold_days or 0),
        bool(config.single_security_mode),
        prof_comm,
        prof_min_comm,
        prof_stamp,
        prof_lot,
        slot_by_rank,
        sym_sort_rank,
        sl_is_atr,
        sl_pct,
        sl_mult,
        sl_cd,
        tp_is_atr,
        tp_pct,
        tp_mult,
        tp_cd,
        tr_is_atr,
        tr_pct,
        tr_mult,
        tr_cd,
        act_offsets,
        act_date,
        act_cash,
        act_ratio,
        act_neutral,
        has_ranked,
        ranked_offsets,
        ranked_flat,
        resume_start,
        init_pos_active,
        init_pos_size,
        init_pos_entry_price,
        init_pos_entry_date,
        init_pos_highest,
        init_pos_buy_comm,
        init_pos_buy_slip,
        init_pos_entry_local,
        init_pos_order,
        init_n_pos,
        init_ps_slot,
        init_ps_sig,
        init_ps_reason,
        init_n_ps,
        init_pb_slot,
        init_pb_sig,
        init_pb_budget,
        init_n_pb,
        init_cooldown,
        init_last_price,
        init_last_seen,
        init_applied,
        float(resumed_checkpoint.cash) if resumed_checkpoint else 0.0,
        float(resumed_checkpoint.peak_value) if resumed_checkpoint else 0.0,
        float(resumed_checkpoint.max_drawdown) if resumed_checkpoint else 0.0,
    )
    return inputs, action_keys, act_offsets


def _checkpoint_resume_start(
    checkpoint: SimulationCheckpoint, dates: list[int], data_fingerprint: str
) -> int:
    """与原 ``_checkpoint_start_index`` 同一套校验；返回续跑起点，-1 = 内核不支持。

    不支持的形态交回原实现处理：无效检查点由原实现抛同样的 ValueError，
    语义不会因为走了内核路径而改变。
    """

    if checkpoint.include_details is not False:
        return -1
    if checkpoint.data_fingerprint and data_fingerprint:
        if checkpoint.data_fingerprint != data_fingerprint:
            return -1
    elif checkpoint.data_fingerprint != data_fingerprint:
        return -1
    processed = checkpoint.processed_dates
    if tuple(int(value) for value in dates[: len(processed)]) != processed:
        return -1
    if len(dates) < len(processed):
        return -1
    resume_start = bisect_right(dates, checkpoint.last_date)
    if resume_start >= len(dates):
        return -1
    return resume_start


def try_fast_portfolio_simulation(
    service: Any,
    contexts: Sequence[_StockSeries],
    dates: list[int],
    config: PortfolioBacktestConfig,
    *,
    data_fingerprint: str,
    ranked_candidate_index: Any,
    checkpoint: SimulationCheckpoint | None = None,
) -> dict[str, object] | None:
    """在内核支持范围内用 Numba 跑 ``_simulate``；否则返回 None 走原实现。

    ``checkpoint`` 非 None 时从该状态续跑（多保真度寻优的 medium/full 阶段），
    输出与前缀结果拼接，语义与原实现的检查点续跑一致。
    """

    global _WARNED
    if not fast_sim_enabled():
        record_outcome("portfolio", "disabled")
        return None
    if not NUMBA_AVAILABLE:
        record_outcome("portfolio", "no_numba")
        return None
    n_symbols = len(contexts)
    n_days = len(dates)
    if n_symbols == 0 or n_days == 0:
        record_outcome("portfolio", "empty")
        return None
    if config.max_positions < 1:
        record_outcome("portfolio", "no_positions")
        return None
    if n_symbols * n_days > _MAX_CELLS:
        # 最容易被忽略的静默回退：全市场标的多、窗口一长，一次模拟就顶到上限。
        record_outcome("portfolio", "too_many_cells")
        return None
    if n_days * (2 * config.max_positions + 1) + 8 > _MAX_TRADE_ROWS:
        record_outcome("portfolio", "too_many_trade_rows")
        return None
    resume_start = 0
    if checkpoint is not None:
        resume_start = _checkpoint_resume_start(checkpoint, dates, data_fingerprint)
        if resume_start < 0:
            record_outcome("portfolio", "checkpoint_unsupported")
            return None
    try:
        built = _build_inputs(
            service,
            contexts,
            dates,
            config,
            data_fingerprint,
            ranked_candidate_index,
            checkpoint,
            resume_start,
        )
        if built is None:
            record_outcome("portfolio", "unsupported_inputs")
            return None
        inputs, action_keys, act_offsets = built
        outputs = _portfolio_kernel()(*inputs)
    except Exception:  # noqa: BLE001 - 内核异常一律退回 Python 实现
        record_outcome("portfolio", "kernel_error")
        if not _WARNED:
            _WARNED = True
            _LOGGER.warning("组合回测内核执行失败，回退到 Python 实现", exc_info=True)
        return None
    record_outcome("portfolio", "hit")
    return _build_result(
        service,
        contexts,
        dates,
        config,
        data_fingerprint,
        action_keys,
        act_offsets,
        outputs,
        resume_start=resume_start,
        prefix=checkpoint if resume_start > 0 else None,
    )


def _build_result(
    service: Any,
    contexts: Sequence[_StockSeries],
    dates: list[int],
    config: PortfolioBacktestConfig,
    data_fingerprint: str,
    action_keys: list[list[str]],
    act_offsets: np.ndarray,
    outputs: tuple[Any, ...],
    resume_start: int = 0,
    prefix: SimulationCheckpoint | None = None,
) -> dict[str, object]:
    (
        cash,
        peak_value,
        max_drawdown,
        n_pos,
        pos_order,
        pos_active,
        pos_size,
        pos_entry_price,
        pos_entry_date,
        pos_highest,
        pos_buy_comm,
        pos_buy_slip,
        _pos_entry_local,
        applied,
        n_ps,
        ps_slot,
        ps_sig,
        ps_reason,
        n_pb,
        pb_slot,
        pb_sig,
        pb_budget,
        cooldown,
        last_price,
        last_seen,
        n_trades,
        t_day,
        t_sig,
        t_slot,
        t_dir,
        t_size,
        t_price,
        t_comm,
        t_stamp,
        t_slip,
        t_pnl,
        t_cb,
        t_reason,
        t_risk,
        t_cd,
        t_eqb,
        eq_cash,
        eq_posval,
        eq_total,
        eq_count,
    ) = outputs
    n_days = len(dates)

    # 日期文本一次生成：原 _date_text 每次调用要走两趟 pandas，占掉重建的大头。
    # 对合法的 8 位 YYYYMMDD，纯切片与 pd.to_datetime().strftime() 逐字符等价。
    date_texts = [_fast_date_text(int(value)) for value in dates]

    # 续跑时净值/交易前缀直接取自检查点（与原实现的拼接语义一致），
    # 内核输出只覆盖 [resume_start, n_days) 区间。
    equity: list[dict[str, object]] = (
        cast(list[dict[str, object]], list(prefix.equity)) if prefix is not None else []
    )
    equity.extend(
        {
            "date": date_texts[day],
            "cash": float(eq_cash[day]),
            "position_value": float(eq_posval[day]),
            "total": float(eq_total[day]),
            "positions_count": int(eq_count[day]),
        }
        for day in range(resume_start, n_days)
    )

    trades: list[dict[str, object]] = (
        cast(list[dict[str, object]], list(prefix.trades)) if prefix is not None else []
    )
    for i in range(int(n_trades)):
        slot = int(t_slot[i])
        context = contexts[slot]
        direction = "BUY" if t_dir[i] == 0 else "SELL"
        reason_id = int(t_reason[i])
        row: dict[str, object] = {
            "date": date_texts[int(t_day[i])],
            "signal_date": _fast_date_text(int(t_sig[i])),
            "market": context.market,
            "code": context.code,
            "name": context.name,
            "instrument_type": context.instrument_type,
            "board": context.board,
            "direction": direction,
            "size": float(t_size[i]),
            "price": float(t_price[i]),
            "commission": float(t_comm[i]),
            "stamp_tax": float(t_stamp[i]),
            "slippage": float(t_slip[i]),
            "pnl": float(t_pnl[i]),
            "cost_basis": float(t_cb[i]),
            "reason": (
                _REASON_LABELS[reason_id]
                if reason_id in (0, 1, 5)
                else _risk_trigger_label(_TRIGGER_NAMES[reason_id])
            ),
            "rejected": False,
        }
        if reason_id in (2, 3, 4):
            row["risk_trigger"] = _TRIGGER_NAMES[reason_id]
            row["cooldown_days"] = int(t_cd[i])
        if direction == "BUY" and config.single_security_mode:
            row["equity_before"] = float(t_eqb[i])
        trades.append(row)

    positions: dict[str, _Position] = {}
    for oi in range(int(n_pos)):
        slot = int(pos_order[oi])
        context = contexts[slot]
        applied_keys = {
            action_keys[slot][ai]
            for ai in range(len(action_keys[slot]))
            if bool(applied[int(act_offsets[slot]) + ai])
        }
        positions[context.symbol] = _Position(
            market=context.market,
            code=context.code,
            instrument_type=context.instrument_type,
            board=context.board,
            size=float(pos_size[slot]),
            entry_price=float(pos_entry_price[slot]),
            entry_date=int(pos_entry_date[slot]),
            highest_price=float(pos_highest[slot]),
            buy_commission=float(pos_buy_comm[slot]),
            buy_slippage=float(pos_buy_slip[slot]),
            name=context.name,
            applied_corporate_actions=applied_keys,
        )

    pending_sells: dict[str, _PendingSell] = {}
    for i in range(int(n_ps)):
        slot = int(ps_slot[i])
        context = contexts[slot]
        pending_sells[context.symbol] = _PendingSell(
            int(ps_sig[i]), _REASON_LABELS[int(ps_reason[i])]
        )
    pending_buys = [
        _PendingBuy(contexts[int(pb_slot[i])].symbol, int(pb_sig[i]), float(pb_budget[i]))
        for i in range(int(n_pb))
    ]
    cooldown_until = {
        contexts[slot].symbol: int(cooldown[slot])
        for slot in range(len(contexts))
        if cooldown[slot] != -1
    }
    last_prices = {
        contexts[slot].symbol: float(last_price[slot])
        for slot in range(len(contexts))
        if last_seen[slot]
    }

    by_symbol = {context.symbol: context for context in contexts}
    return {
        "cash": float(cash),
        "equity": equity,
        "states": [],
        "trades": trades,
        "ranking_events": [],
        "current_state": None,
        "today_candidates": {
            "as_of": None,
            "ranking_value": config.ranking_value,
            "total": 0,
            "rows": [],
        },
        "next_plan": service._next_plan(
            as_of=int(dates[-1]),
            positions=positions,
            pending_sells=pending_sells,
            pending_buys=pending_buys,
            by_symbol=by_symbol,
            last_prices=last_prices,
            cash=float(cash),
            config=config,
        ),
        "checkpoint": SimulationCheckpoint(
            last_date=int(dates[-1]),
            cash=float(cash),
            positions={symbol: replace(position) for symbol, position in positions.items()},
            pending_sells=dict(pending_sells),
            pending_buys=tuple(pending_buys),
            cooldown_until=dict(cooldown_until),
            last_prices=dict(last_prices),
            peak_value=float(peak_value),
            max_drawdown=float(max_drawdown),
            processed_dates=tuple(int(value) for value in dates),
            equity=tuple(dict(record) for record in equity),
            trades=tuple(dict(record) for record in trades),
            states=(),
            ranking_events=(),
            include_details=False,
            data_fingerprint=data_fingerprint,
        ),
    }
