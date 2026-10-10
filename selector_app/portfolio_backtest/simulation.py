"""Portfolio state-machine and order execution logic."""

from __future__ import annotations

import heapq
from bisect import bisect_right
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd

from selector_app.backtest.profiles import TradeProfile, profile_from_config
from selector_app.market_data.adjustment import CorporateAction
from selector_app.market_data.models import MarketCode, StockRef
from selector_app.screening.jobs import TaskCancelledError
from selector_app.screening.models import ScreenCondition
from selector_app.screening.rules import RuleGroup
from selector_app.strategy_fitness.rolling import RollingFitnessDecision, RollingFitnessFilter

from .checkpoint import SimulationCheckpoint
from .context import _StockSeries
from .models import PortfolioBacktestConfig, PortfolioJsonValue
from .report import _date_text
from .risk import _risk_trigger, _risk_trigger_label
from .simulation_models import _PendingBuy, _PendingSell, _Position
from .state import _PortfolioServiceState

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
)


def _action_date_int(value: date) -> int:
    """``date`` -> ``YYYYMMDD``，与回测日期轴用的整数格式对齐。"""

    return value.year * 10000 + value.month * 100 + value.day


class PortfolioSimulationMixin(_PortfolioServiceState):
    """Execute deterministic portfolio state transitions and order fills."""

    @staticmethod
    def _build_ranked_candidate_index(
        contexts: Sequence[_StockSeries],
        dates: Sequence[int],
        config: PortfolioBacktestConfig,
    ) -> dict[int, np.ndarray]:
        """Cache the static buy/rank order for risk-only candidate simulations."""

        ranked_by_date: dict[int, np.ndarray] = {}
        for current_date in dates:
            rows: list[tuple[float, str, int]] = []
            for context_index, context in enumerate(contexts):
                index = context.date_to_index.get(current_date)
                if index is None or not bool(context.buy_mask[index]):
                    continue
                ranking_values = context.values.get(config.ranking_value)
                if ranking_values is None:
                    continue
                score = float(ranking_values[index])
                if not np.isfinite(score):
                    continue
                priority = -score if config.rank_order == "desc" else score
                rows.append((priority, context.symbol, context_index))
            if not rows:
                continue
            rows.sort()
            ranked_by_date[int(current_date)] = np.fromiter(
                (context_index for _priority, _symbol, context_index in rows),
                dtype=np.int32,
                count=len(rows),
            )
        return ranked_by_date

    @staticmethod
    def _no_trade_simulation(
        dates: Sequence[int],
        config: PortfolioBacktestConfig,
        *,
        include_details: bool,
    ) -> dict[str, object]:
        """Build the all-cash result without scanning every context per date."""

        equity: list[dict[str, PortfolioJsonValue]] = []
        states: list[dict[str, PortfolioJsonValue]] = []
        ranking_events: list[dict[str, PortfolioJsonValue]] = []
        date_list = [int(value) for value in dates]
        for index, current_date in enumerate(date_list):
            equity.append(
                {
                    "date": _date_text(current_date),
                    "cash": float(config.initial_cash),
                    "position_value": 0.0,
                    "total": float(config.initial_cash),
                    "positions_count": 0,
                }
            )
            if not include_details:
                continue
            states.append(
                {
                    "date": _date_text(current_date),
                    "cash": float(config.initial_cash),
                    "position_value": 0.0,
                    "total": float(config.initial_cash),
                    "positions_count": 0,
                    "holdings": [],
                }
            )
            if PortfolioSimulationMixin._should_refresh(
                index, date_list, config.rebalance_frequency
            ):
                ranking_events.append(
                    {
                        "date": _date_text(current_date),
                        "slots_available": config.max_positions,
                        "ranking_value": config.ranking_value,
                        "candidates": [],
                    }
                )
        last_state = states[-1] if states else None
        current_state = (
            {
                "as_of": last_state["date"],
                "available": True,
                "equity": float(config.initial_cash),
                "total_asset": float(config.initial_cash),
                "cash": float(config.initial_cash),
                "position_value": 0.0,
                "positions_count": 0,
                "holdings": [],
            }
            if last_state is not None
            else None
        )
        return {
            "cash": float(config.initial_cash),
            "equity": equity,
            "states": states,
            "trades": [],
            "ranking_events": ranking_events,
            "current_state": current_state,
            "today_candidates": {
                "as_of": _date_text(date_list[-1]) if date_list else None,
                "ranking_value": config.ranking_value,
                "total": 0,
                "rows": [],
            },
            "diagnostic": "预检无可交易候选",
        }

    def _simulate(
        self,
        contexts: list[_StockSeries],
        dates: list[int],
        config: PortfolioBacktestConfig,
        fitness_filter: RollingFitnessFilter | None = None,
        *,
        include_details: bool = True,
        checkpoint: SimulationCheckpoint | None = None,
        data_fingerprint: str = "",
        ranked_candidate_index: Mapping[int, np.ndarray] | None = None,
        abort_check: Callable[[], bool] | None = None,
    ) -> dict[str, object]:
        # numba 内核路径：只接发现寻优的形态（指标汇总、无检查点/fitness/中止回调），
        # 与下面的原状态机逐行等价，由差分测试保证；不支持或异常时退回原实现。
        if not (
            not include_details
            and checkpoint is None
            and fitness_filter is None
            and abort_check is None
        ):
            # 记下是哪项形态不满足：内核长期不生效时，靠这个定位而不是靠猜。
            from selector_app.jit import record_outcome

            if include_details:
                record_outcome("portfolio", "details_requested")
            elif checkpoint is not None:
                record_outcome("portfolio", "resume_checkpoint")
            elif fitness_filter is not None:
                record_outcome("portfolio", "fitness_filter")
            else:
                record_outcome("portfolio", "abort_check")

        if (
            not include_details
            and checkpoint is None
            and fitness_filter is None
            and abort_check is None
        ):
            from selector_app.portfolio_backtest.fast_sim import try_fast_portfolio_simulation

            fast_result = try_fast_portfolio_simulation(
                self,
                contexts,
                dates,
                config,
                data_fingerprint=data_fingerprint,
                ranked_candidate_index=ranked_candidate_index,
            )
            if fast_result is not None:
                return fast_result

        by_symbol = {context.symbol: context for context in contexts}
        start_index = 0
        if checkpoint is None:
            positions: dict[str, _Position] = {}
            pending_sells: dict[str, _PendingSell] = {}
            pending_buys: list[_PendingBuy] = []
            cooldown_until: dict[str, int] = {}
            last_prices: dict[str, float] = {}
            peak_value = float(config.initial_cash)
            max_drawdown = 0.0
            cash = config.initial_cash
            trades: list[dict[str, PortfolioJsonValue]] = []
            equity: list[dict[str, PortfolioJsonValue]] = []
            states: list[dict[str, PortfolioJsonValue]] = []
            ranking_events: list[dict[str, PortfolioJsonValue]] = []
        else:
            start_index = self._checkpoint_start_index(
                checkpoint,
                dates,
                data_fingerprint=data_fingerprint,
                include_details=include_details,
            )
            positions = {
                symbol: replace(
                    position,
                    applied_corporate_actions=set(position.applied_corporate_actions),
                )
                for symbol, position in checkpoint.positions.items()
            }
            known_symbols = set(by_symbol)
            if any(symbol not in known_symbols for symbol in positions):
                raise ValueError("模拟检查点持仓标的不在当前行情上下文中")
            pending_sells = dict(checkpoint.pending_sells)
            pending_buys = list(checkpoint.pending_buys)
            if any(symbol not in known_symbols for symbol in pending_sells):
                raise ValueError("模拟检查点卖出订单标的不在当前行情上下文中")
            if any(pending.symbol not in known_symbols for pending in pending_buys):
                raise ValueError("模拟检查点买入订单标的不在当前行情上下文中")
            cooldown_until = dict(checkpoint.cooldown_until)
            last_prices = dict(checkpoint.last_prices)
            peak_value = float(checkpoint.peak_value)
            max_drawdown = float(checkpoint.max_drawdown)
            cash = float(checkpoint.cash)
            trades = [dict(record) for record in checkpoint.trades]
            equity = [dict(record) for record in checkpoint.equity]
            states = [dict(record) for record in checkpoint.states]
            ranking_events = [dict(record) for record in checkpoint.ranking_events]

        actions_by_symbol: dict[str, tuple[CorporateAction, ...]] = {}
        for date_index in range(start_index, len(dates)):
            if date_index % 10 == 0 and abort_check is not None and abort_check():
                raise TaskCancelledError("组合回测已取消")
            current_date = dates[date_index]
            # 除权除息必须赶在当天成交之前处理。若 ex-date 停牌，则在该标的下一根
            # 实际 bar 调整，避免用旧收盘价给已拆股的仓位估值。
            cash = self._apply_corporate_actions(
                current_date,
                positions,
                by_symbol,
                cash,
                actions_by_symbol,
            )
            cash = self._execute_sells(
                current_date,
                positions,
                pending_sells,
                by_symbol,
                last_prices,
                cash,
                config,
                trades,
            )
            cash = self._execute_risk_sells(
                date_index,
                current_date,
                positions,
                pending_sells,
                by_symbol,
                cash,
                config,
                cooldown_until,
                trades,
            )
            cash = self._execute_buys(
                current_date,
                positions,
                pending_sells,
                pending_buys,
                by_symbol,
                cash,
                config,
                cooldown_until,
                date_index,
                trades,
            )
            pending_buys = [
                pending
                for pending in pending_buys
                if pending.symbol not in positions
                and pending.symbol not in pending_sells
                and date_index >= cooldown_until.get(pending.symbol, -1)
                and pending.signal_date >= current_date
            ]
            for symbol in positions:
                context = by_symbol[symbol]
                index = context.date_to_index.get(current_date)
                if index is not None:
                    last_prices[symbol] = float(context.closes[index])
            total_value, position_value = self._portfolio_value(positions, last_prices, cash)
            peak_value = max(peak_value, total_value)
            current_drawdown = (peak_value - total_value) / peak_value if peak_value > 0 else 0.0
            max_drawdown = max(max_drawdown, current_drawdown)
            equity.append(
                {
                    "date": _date_text(current_date),
                    "cash": float(cash),
                    "position_value": float(position_value),
                    "total": float(total_value),
                    "positions_count": len(positions),
                }
            )
            if include_details:
                states.append(
                    self._state_record(
                        current_date, positions, last_prices, cash, total_value, position_value
                    )
                )

            new_exit = self._schedule_exits(
                current_date,
                positions,
                pending_sells,
                by_symbol,
                config,
            )
            if self._should_refresh(date_index, dates, config.rebalance_frequency) or new_exit:
                pending_buys, event = self._select_candidates(
                    current_date,
                    positions,
                    pending_sells,
                    by_symbol,
                    last_prices,
                    cash,
                    total_value,
                    config,
                    fitness_filter,
                    cooldown_until,
                    date_index,
                    include_event=include_details,
                    ranked_contexts=(contexts if ranked_candidate_index is not None else None),
                    ranked_context_indexes=(
                        ranked_candidate_index.get(int(current_date), ())
                        if ranked_candidate_index is not None
                        else None
                    ),
                )
                if include_details:
                    ranking_events.append(event)

        current_state = (
            self._current_state_with_diagnostics(states[-1], by_symbol, config)
            if include_details and states
            else None
        )
        today_candidates = (
            self._today_candidate_scan(
                contexts,
                int(dates[-1]),
                config,
                fitness_filter,
                positions,
            )
            if include_details and dates
            else {"as_of": None, "ranking_value": config.ranking_value, "total": 0, "rows": []}
        )

        return {
            "cash": cash,
            "equity": equity,
            "states": states,
            "trades": trades,
            "ranking_events": ranking_events,
            "current_state": current_state,
            "today_candidates": today_candidates,
            "next_plan": self._next_plan(
                as_of=int(dates[-1]),
                positions=positions,
                pending_sells=pending_sells,
                pending_buys=pending_buys,
                by_symbol=by_symbol,
                last_prices=last_prices,
                cash=cash,
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
                states=tuple(dict(record) for record in states),
                ranking_events=tuple(dict(record) for record in ranking_events),
                include_details=include_details,
                data_fingerprint=data_fingerprint,
            ),
        }

    @staticmethod
    def _value_at(context: _StockSeries, value_id: str, index: int) -> float | None:
        values = context.values.get(value_id)
        if values is None or index < 0 or index >= len(values):
            return None
        value = float(values[index])
        return value if np.isfinite(value) else None

    @classmethod
    def _condition_diagnostic(
        cls,
        condition: ScreenCondition,
        context: _StockSeries,
        index: int,
        *,
        required: bool,
    ) -> dict[str, object]:
        left = cls._value_at(context, condition.left_value, index)
        if condition.right_value is not None:
            right = cls._value_at(context, condition.right_value, index)
        else:
            assert condition.right_constant is not None
            right = float(condition.right_constant)
        previous_left = (
            cls._value_at(context, condition.left_value, index - 1) if index > 0 else None
        )
        if condition.right_value is not None:
            previous_right = (
                cls._value_at(context, condition.right_value, index - 1) if index > 0 else None
            )
        elif index > 0:
            assert condition.right_constant is not None
            previous_right = float(condition.right_constant)
        else:
            previous_right = None
        matched = False
        if left is not None and right is not None:
            if condition.operator == "gt":
                matched = left > right
            elif condition.operator == "gte":
                matched = left >= right
            elif condition.operator == "lt":
                matched = left < right
            elif condition.operator == "lte":
                matched = left <= right
            elif condition.operator == "eq":
                matched = bool(np.isclose(left, right, rtol=1e-9, atol=1e-12))
            elif condition.operator == "neq":
                matched = not bool(np.isclose(left, right, rtol=1e-9, atol=1e-12))
            elif condition.operator == "cross_up":
                matched = (
                    previous_left is not None
                    and previous_right is not None
                    and previous_left <= previous_right
                    and left > right
                )
            elif condition.operator == "cross_down":
                matched = (
                    previous_left is not None
                    and previous_right is not None
                    and previous_left >= previous_right
                    and left < right
                )
        row: dict[str, object] = {
            "left_value": condition.left_value,
            "right_value": condition.right_value,
            "operator": condition.operator,
            "current_left_value": left,
            "current_right_value": right,
            "threshold": right,
            "required": required,
            "matched": matched,
        }
        if condition.operator in {"cross_up", "cross_down"}:
            row["previous_left_value"] = previous_left
            row["previous_right_value"] = previous_right
        return row

    @classmethod
    def _rule_diagnostic(
        cls,
        rule: RuleGroup | None,
        context: _StockSeries,
        index: int,
        *,
        matched: bool,
    ) -> dict[str, object]:
        if rule is None:
            return {"matched": False, "signals": [], "conditions": []}
        signals = [
            {
                "id": signal_id,
                "matched": bool(
                    (values := context.signals.get(signal_id)) is not None
                    and index >= 0
                    and index < len(values)
                    and values[index]
                ),
            }
            for signal_id in rule.signals
        ]
        conditions = [
            cls._condition_diagnostic(condition, context, index, required=True)
            for condition in rule.required_conditions
        ]
        conditions.extend(
            cls._condition_diagnostic(condition, context, index, required=False)
            for condition in rule.conditions
        )
        return {"matched": matched, "signals": signals, "conditions": conditions}

    @classmethod
    def _today_candidate_scan(
        cls,
        contexts: Sequence[_StockSeries],
        as_of: int,
        config: PortfolioBacktestConfig,
        fitness_filter: RollingFitnessFilter | None,
        positions: Mapping[str, _Position],
    ) -> dict[str, object]:
        entries: list[tuple[float, _StockSeries, RollingFitnessDecision | None, int]] = []
        for context in contexts:
            index = context.date_to_index.get(as_of)
            if index is None or not bool(context.buy_mask[index]):
                continue
            score = cls._candidate_score(context, index, config)
            if score is None:
                continue
            if not np.isfinite(score):
                continue
            decision = fitness_filter.decide(context.symbol, as_of) if fitness_filter else None
            entries.append((score, context, decision, index))

        score_entries = [(score, context, decision) for score, context, decision, _ in entries]
        weighted = (
            cls._weighted_scores(score_entries, config)
            if fitness_filter is not None and config.fitness_rank_weight > 0
            else None
        )
        rank_values = [
            weighted[offset]
            if weighted is not None
            else score
            if config.rank_order == "desc"
            else -score
            for offset, (score, _context, _decision, _index) in enumerate(entries)
        ]
        display_rank_scores = (
            weighted
            if weighted is not None
            else [score for score, _context, _decision, _index in entries]
        )
        ranked = sorted(
            zip(entries, rank_values, display_rank_scores, strict=True),
            key=lambda item: (-item[1], item[0][1].symbol),
        )
        rows: list[dict[str, object]] = []
        for rank, ((score, context, decision, index), _sort_score, rank_score) in enumerate(
            ranked, start=1
        ):
            buy = cls._rule_diagnostic(
                context.buy_rule or config.buy_rule,
                context,
                index,
                matched=bool(context.buy_mask[index]),
            )
            row: dict[str, object] = {
                "rank": rank,
                "market": context.market,
                "code": context.code,
                "name": context.name,
                "score": score,
                "rank_score": rank_score,
                "ranking_value": config.ranking_value,
                "already_held": context.symbol in positions,
                "buy_signal": {"matched": buy["matched"], "signals": buy["signals"]},
                "buy_conditions": buy["conditions"],
            }
            if decision is not None:
                row["fitness_score"] = decision.score
                row["fitness_trades"] = decision.trades
                row["fitness_note"] = decision.note
            if rank <= 20:
                rows.append(row)
        return {
            "as_of": _date_text(as_of),
            "ranking_value": config.ranking_value,
            "total": len(entries),
            "rows": rows,
        }

    @classmethod
    def _current_state_with_diagnostics(
        cls,
        state: Mapping[str, PortfolioJsonValue],
        by_symbol: Mapping[str, _StockSeries],
        config: PortfolioBacktestConfig,
    ) -> dict[str, object]:
        as_of = int(str(state.get("date") or "").replace("-", ""))
        total = float(cast(float, state.get("total", 0.0)))
        position_value = float(cast(float, state.get("position_value", 0.0)))
        holdings: list[dict[str, object]] = []
        raw_holdings = state.get("holdings")
        if isinstance(raw_holdings, (list, tuple)):
            for raw in raw_holdings:
                if not isinstance(raw, Mapping):
                    continue
                row = dict(raw)
                symbol = f"{row.get('market', '')}{row.get('code', '')}"
                context = by_symbol.get(symbol)
                if context is not None:
                    index = context.date_to_index.get(as_of)
                    if index is None:
                        index = bisect_right(context.dates, as_of) - 1
                    indicator_as_of = _date_text(int(context.dates[index])) if index >= 0 else None
                    row["sell_signal"] = cls._rule_diagnostic(
                        context.sell_rule or config.sell_rule,
                        context,
                        index if index is not None else -1,
                        matched=(
                            bool(context.sell_mask[index])
                            if index is not None and index >= 0
                            else False
                        ),
                    )
                    row["indicator_as_of"] = indicator_as_of
                else:
                    row["sell_signal"] = {"matched": False, "signals": [], "conditions": []}
                    row["indicator_as_of"] = None
                price = float(cast(float, row.get("close", row.get("entry_price", 0.0))))
                size = float(cast(float, row.get("size", 0.0)))
                market_value = size * price
                entry_price = float(cast(float, row.get("entry_price", 0.0)))
                row.update(
                    {
                        "latest_price": price,
                        "market_value": market_value,
                        "unrealized_pnl_pct": (
                            price / entry_price - 1.0 if entry_price > 0 else 0.0
                        ),
                        "position_weight": market_value / total if total > 0 else 0.0,
                    }
                )
                holdings.append(row)
        return {
            "as_of": state.get("date"),
            "available": True,
            "equity": total,
            "total_asset": total,
            "cash": float(cast(float, state.get("cash", 0.0))),
            "position_value": position_value,
            "positions_count": int(cast(int, state.get("positions_count", len(holdings)))),
            "holdings": holdings,
        }

    @staticmethod
    def _checkpoint_start_index(
        checkpoint: SimulationCheckpoint,
        dates: Sequence[int],
        *,
        data_fingerprint: str,
        include_details: bool,
    ) -> int:
        if checkpoint.data_fingerprint and data_fingerprint:
            if checkpoint.data_fingerprint != data_fingerprint:
                raise ValueError("模拟检查点行情指纹不匹配")
        elif checkpoint.data_fingerprint != data_fingerprint:
            raise ValueError("模拟检查点缺少可验证的行情指纹")
        if checkpoint.include_details != include_details:
            raise ValueError("模拟检查点明细模式不一致")
        prefix = tuple(int(value) for value in dates[: len(checkpoint.processed_dates)])
        if prefix != checkpoint.processed_dates:
            raise ValueError("模拟检查点日期前缀不连续")
        if len(dates) < len(checkpoint.processed_dates):
            raise ValueError("模拟检查点超出当前回测日期范围")
        return bisect_right(dates, checkpoint.last_date)

    def _next_plan(
        self,
        *,
        as_of: int,
        positions: Mapping[str, _Position],
        pending_sells: Mapping[str, _PendingSell],
        pending_buys: Sequence[_PendingBuy],
        by_symbol: Mapping[str, _StockSeries],
        last_prices: Mapping[str, float],
        cash: float,
        config: PortfolioBacktestConfig,
    ) -> dict[str, PortfolioJsonValue]:
        """最后一根 K 线收盘时排定、下一交易日要执行的单。

        引擎本来就是「T 日收盘判定、T+1 开盘撮合」，lag 恒为一根 K（见
        :meth:`_execution_price`），所以循环结束时还挂在 ``pending`` 里的单，正好
        就是明天的动作——不需要再算一遍，也不存在别的口径。

        但有两件事今天不可能知道：明天的开盘价，和明天会不会触发止损 / 止盈。
        所以数量一律按**今日收盘价**估算，字段名一律带 ``estimated_`` 前缀：谁都
        不许把它当成实际成交，界面上也不能省掉"估算"两个字。
        """

        holdings: list[PortfolioJsonValue] = []
        for symbol, position in positions.items():
            price = float(last_prices.get(symbol, position.entry_price))
            holdings.append(
                {
                    "market": position.market,
                    "code": position.code,
                    "name": position.name or "",
                    "size": float(position.size),
                    "reference_price": price,
                    "market_value": float(position.size) * price,
                    "entry_price": float(position.entry_price),
                    "entry_date": _date_text(position.entry_date),
                    "unrealized_pnl_pct": (
                        (price - float(position.entry_price)) / float(position.entry_price)
                        if position.entry_price > 0
                        else 0.0
                    ),
                }
            )

        sells: list[PortfolioJsonValue] = []
        proceeds = 0.0
        for symbol, sell_order in pending_sells.items():
            held = positions.get(symbol)
            if held is None:
                continue
            price = float(last_prices.get(symbol, held.entry_price))
            profile = profile_from_config(
                held.instrument_type,
                commission=config.commission,
                min_commission=config.min_commission,
                stamp_tax=config.stamp_tax,
            )
            size = float(held.size)
            gross = size * price
            commission = max(gross * profile.commission, profile.min_commission)
            stamp_tax = gross * profile.stamp_tax
            net = gross - commission - stamp_tax - size * config.slippage
            proceeds += net
            sells.append(
                {
                    "market": held.market,
                    "code": held.code,
                    "name": held.name or "",
                    "size": size,
                    "reference_price": price,
                    "estimated_proceeds": net,
                    "reason": sell_order.reason or "卖出条件",
                    "signal_date": _date_text(sell_order.signal_date),
                }
            )

        rebalance_due = self._rebalance_due(as_of, config.rebalance_frequency)
        # 最后一天在 :meth:`_should_refresh` 里恒为 True（否则回测收尾那天选不出
        # 票），于是月 / 周频策略在最后一棒也会攒出一批买入候选。那批单不是"明天
        # 该做的"，只是引擎收尾的副产品——这里必须掐掉，否则每天都会给一份假计划。
        buys: list[PortfolioJsonValue] = []
        if rebalance_due:
            available = cash + proceeds
            for order in pending_buys:
                context = by_symbol.get(order.symbol)
                if context is None:
                    continue
                # 买入候选按定义不在持仓里，而 last_prices 只在遍历持仓时更新
                # （也从不清掉旧条目），所以这里必须自己去取末棒收盘：否则从没买过
                # 的标的恒为 0、整笔单从计划里静默消失，买过又卖的则拿到过期价。
                index = context.date_to_index.get(as_of)
                price = float(context.closes[index]) if index is not None else 0.0
                if price <= 0:
                    # 末棒没有收盘价（停牌）：退回最近一次见过的价，再没有就不给。
                    price = float(last_prices.get(order.symbol, 0.0))
                if price <= 0:
                    continue
                profile = profile_from_config(
                    context.instrument_type,
                    commission=config.commission,
                    min_commission=config.min_commission,
                    stamp_tax=config.stamp_tax,
                )
                shares = self._planned_shares(order, price, profile, config)
                shares = self._affordable_shares(shares, price, available, profile, config.slippage)
                if shares <= 0:
                    continue
                gross = shares * price
                # 实际从可用资金扣的是「货款 + 佣金 + 滑点」，估算成本必须同口径，
                # 否则滑点非零时明日计划里的成本被系统性低估。
                cost = (
                    gross
                    + max(gross * profile.commission, profile.min_commission)
                    + shares * config.slippage
                )
                available -= cost
                buys.append(
                    {
                        "market": context.market,
                        "code": context.code,
                        "name": context.name or "",
                        "estimated_size": float(shares),
                        "reference_price": price,
                        "estimated_cost": cost,
                        "signal_date": _date_text(order.signal_date),
                    }
                )

        notes: list[str] = []
        if not rebalance_due:
            label = {"weekly": "每周", "monthly": "每月"}.get(
                config.rebalance_frequency, config.rebalance_frequency
            )
            notes.append(f"这个策略是{label}调仓，最近一个交易日不是调仓日，所以明天没有买入计划。")
        if sells:
            notes.append("卖出数量是持仓实际股数；回款按今日收盘价估，实际以明日开盘价成交。")
        if buys:
            notes.append("买入股数是按今日收盘价与可用现金估算的，明天开盘价变了就会变。")
        if not sells and not buys:
            notes.append("明日无需操作：持仓不动，也没有新的买入标的。")

        return {
            "as_of": _date_text(as_of),
            "cash": float(cash),
            "estimated_cash_after_sells": float(cash) + proceeds,
            "rebalance_due": rebalance_due,
            "holdings": holdings,
            "sells": sells,
            "buys": buys,
            "notes": notes,
        }

    @staticmethod
    def _next_workday(as_of: int) -> int:
        """``as_of`` 之后第一个工作日（周一至周五）的 YYYYMMDD。

        明天的交易日今天还不存在，只能外推。这里**只认周末，不认法定节假日**：
        长假前最后一个交易日会被推到假期结束后第一个工作日，于是可能"多给一份
        计划"。方向选宁可多给也不少给——少给的代价是用户错过真正的调仓日，而周频
        策略一周只有一次机会。
        """

        following = pd.Timestamp(_date_text(as_of)) + pd.Timedelta(1, unit="D")
        while following.dayofweek >= 5:  # 5=周六 6=周日
            following = following + pd.Timedelta(1, unit="D")
        return int(following.strftime("%Y%m%d"))

    @staticmethod
    def _rebalance_due(as_of: int, frequency: str) -> bool:
        """最近一个交易日的**次日**是不是调仓日。

        回测里 :meth:`_should_refresh` 比的是当前交易日与**下一个交易日**；放到"明天"
        这件事上，下一个交易日还没有发生，所以要外推一个工作日再比。

        ⚠️ 不能直接拿 ``as_of + 1 天`` 比：那样周频只在 as_of 是周日时才判跨周，
        而 A 股没有周日交易——结果是周频策略永远拿不到买入计划。月频同理会漏掉
        "月末最后一个交易日不是自然月末"的月份（月末撞周末时，最后一根 bar 的次日
        仍在本月，但真实下一个交易日已经跨月）。
        """

        if frequency == "daily":
            return True
        current = pd.Timestamp(_date_text(as_of))
        following = pd.Timestamp(_date_text(PortfolioSimulationMixin._next_workday(as_of)))
        if frequency == "weekly":
            # 比 weekday 而不是 ISO 周号：跨年那一周 ISO 的 year 会变，比
            # (year, week) 会把同一周判成两周。dayofweek 周一=0，绕回周一即跨周。
            return bool(following.dayofweek <= current.dayofweek)
        return (current.year, current.month) != (following.year, following.month)

    def _execute_sells(
        self,
        current_date: int,
        positions: dict[str, _Position],
        pending_sells: dict[str, _PendingSell],
        by_symbol: Mapping[str, _StockSeries],
        last_prices: Mapping[str, float],
        cash: float,
        config: PortfolioBacktestConfig,
        trades: list[dict[str, PortfolioJsonValue]],
    ) -> float:
        for symbol, pending in list(pending_sells.items()):
            position = positions.get(symbol)
            context = by_symbol[symbol]
            index = context.date_to_index.get(current_date)
            if position is None or index is None:
                continue
            price = self._execution_price(context, index, config)
            size = position.size
            profile = profile_from_config(
                position.instrument_type,
                commission=config.commission,
                min_commission=config.min_commission,
                stamp_tax=config.stamp_tax,
            )
            commission = max(size * price * profile.commission, profile.min_commission)
            stamp_tax = size * price * profile.stamp_tax
            slippage = size * config.slippage
            proceeds = size * price - commission - stamp_tax - slippage
            cash += proceeds
            trades.append(
                {
                    "date": _date_text(current_date),
                    "signal_date": _date_text(pending.signal_date),
                    "market": position.market,
                    "code": position.code,
                    "name": position.name,
                    "instrument_type": position.instrument_type,
                    "board": position.board,
                    "direction": "SELL",
                    "size": float(size),
                    "price": float(price),
                    "commission": float(commission),
                    "stamp_tax": float(stamp_tax),
                    "slippage": float(slippage),
                    "pnl": float(
                        (price - position.entry_price) * size
                        - position.buy_commission
                        - position.buy_slippage
                        - commission
                        - stamp_tax
                        - slippage
                    ),
                    "cost_basis": float(
                        position.entry_price * size
                        + position.buy_commission
                        + position.buy_slippage
                    ),
                    "reason": pending.reason,
                    "rejected": False,
                }
            )
            positions.pop(symbol, None)
            pending_sells.pop(symbol, None)
        return cash

    def _execute_risk_sells(
        self,
        date_index: int,
        current_date: int,
        positions: dict[str, _Position],
        pending_sells: Mapping[str, _PendingSell],
        by_symbol: Mapping[str, _StockSeries],
        cash: float,
        config: PortfolioBacktestConfig,
        cooldown_until: dict[str, int],
        trades: list[dict[str, PortfolioJsonValue]],
    ) -> float:
        """Execute OHLC-based risk exits on the trigger bar itself."""

        for symbol in sorted(list(positions)):
            if symbol in pending_sells:
                continue
            position = positions.get(symbol)
            context = by_symbol[symbol]
            index = context.date_to_index.get(current_date)
            if position is None or index is None:
                continue
            risk_overrides: dict[str, Any] = {
                key: context.strategy_config[key]
                for key in _SYMBOL_RISK_FIELDS
                if key in context.strategy_config
            }
            risk_config = replace(config, **risk_overrides)
            trigger = _risk_trigger(context, index, position, risk_config)
            if trigger is None:
                continue
            trigger_name, trigger_price, cooldown_days = trigger
            price = float(trigger_price)
            size = position.size
            profile = profile_from_config(
                position.instrument_type,
                commission=config.commission,
                min_commission=config.min_commission,
                stamp_tax=config.stamp_tax,
            )
            commission = max(size * price * profile.commission, profile.min_commission)
            stamp_tax = size * price * profile.stamp_tax
            slippage = size * config.slippage
            proceeds = size * price - commission - stamp_tax - slippage
            cash += proceeds
            trades.append(
                {
                    "date": _date_text(current_date),
                    "signal_date": _date_text(current_date),
                    "market": position.market,
                    "code": position.code,
                    "name": position.name,
                    "instrument_type": position.instrument_type,
                    "board": position.board,
                    "direction": "SELL",
                    "size": float(size),
                    "price": price,
                    "commission": float(commission),
                    "stamp_tax": float(stamp_tax),
                    "slippage": float(slippage),
                    "pnl": float(
                        (price - position.entry_price) * size
                        - position.buy_commission
                        - position.buy_slippage
                        - commission
                        - stamp_tax
                        - slippage
                    ),
                    "cost_basis": float(
                        position.entry_price * size
                        + position.buy_commission
                        + position.buy_slippage
                    ),
                    "reason": _risk_trigger_label(trigger_name),
                    "risk_trigger": trigger_name,
                    "cooldown_days": cooldown_days,
                    "rejected": False,
                }
            )
            positions.pop(symbol, None)
            if cooldown_days > 0:
                cooldown_until[symbol] = date_index + cooldown_days + 1
        return cash

    def _execute_buys(
        self,
        current_date: int,
        positions: dict[str, _Position],
        pending_sells: Mapping[str, _PendingSell],
        pending_buys: list[_PendingBuy],
        by_symbol: Mapping[str, _StockSeries],
        cash: float,
        config: PortfolioBacktestConfig,
        cooldown_until: Mapping[str, int],
        date_index: int,
        trades: list[dict[str, PortfolioJsonValue]],
    ) -> float:
        for pending in pending_buys:
            if len(positions) >= config.max_positions:
                break
            if pending.symbol in positions or pending.symbol in pending_sells:
                continue
            if date_index < cooldown_until.get(pending.symbol, -1):
                continue
            context = by_symbol[pending.symbol]
            index = context.date_to_index.get(current_date)
            if index is None:
                continue
            price = self._execution_price(context, index, config)
            profile = profile_from_config(
                context.instrument_type,
                commission=config.commission,
                min_commission=config.min_commission,
                stamp_tax=config.stamp_tax,
            )
            shares = self._planned_shares(pending, price, profile, config)
            shares = self._affordable_shares(shares, price, cash, profile, config.slippage)
            if shares <= 0:
                continue
            commission = max(shares * price * profile.commission, profile.min_commission)
            slippage = shares * config.slippage
            equity_before_buy = cash
            cash -= shares * price + commission + slippage
            positions[pending.symbol] = _Position(
                market=context.market,
                code=context.code,
                name=context.name,
                instrument_type=context.instrument_type,
                board=context.board,
                size=float(shares),
                entry_price=float(price),
                entry_date=current_date,
                highest_price=float(price),
                buy_commission=float(commission),
                buy_slippage=float(slippage),
            )
            trades.append(
                {
                    "date": _date_text(current_date),
                    "signal_date": _date_text(pending.signal_date),
                    "market": context.market,
                    "code": context.code,
                    "name": context.name,
                    "instrument_type": context.instrument_type,
                    "board": context.board,
                    "direction": "BUY",
                    **(
                        {"equity_before": float(equity_before_buy)}
                        if config.single_security_mode
                        else {}
                    ),
                    "size": float(shares),
                    "price": float(price),
                    "commission": float(commission),
                    "stamp_tax": 0.0,
                    "slippage": float(slippage),
                    "pnl": 0.0,
                    "cost_basis": 0.0,
                    "reason": "排名入选",
                    "rejected": False,
                }
            )
        return cash

    @staticmethod
    def _planned_shares(
        pending: _PendingBuy,
        price: float,
        profile: TradeProfile,
        config: PortfolioBacktestConfig,
    ) -> int:
        """Shares to buy before affordability trimming.

        ``fixed`` mode buys a constant share count per slot (the single-stock
        backtest behaviour), ``full`` mode spreads the slot budget evenly.
        """

        if price <= 0:
            return 0
        if config.position_mode == "fixed":
            target = int(config.fixed_size or 0)
            return max(target // profile.lot_size * profile.lot_size, 0)
        return int(max(pending.budget, 0) / price / profile.lot_size) * profile.lot_size

    @staticmethod
    def _affordable_shares(
        shares: int,
        price: float,
        cash: float,
        profile: TradeProfile,
        slippage: float,
    ) -> int:
        while shares >= profile.lot_size:
            commission = max(shares * price * profile.commission, profile.min_commission)
            total_cost = shares * price + commission + shares * slippage
            if total_cost <= cash:
                return shares
            shares -= profile.lot_size
        return 0

    @staticmethod
    def _execution_price(
        context: _StockSeries,
        index: int,
        config: PortfolioBacktestConfig,
    ) -> float:
        return float(
            context.opens[index] if config.execution == "next_open" else context.closes[index]
        )

    def _schedule_exits(
        self,
        current_date: int,
        positions: Mapping[str, _Position],
        pending_sells: dict[str, _PendingSell],
        by_symbol: Mapping[str, _StockSeries],
        config: PortfolioBacktestConfig,
    ) -> bool:
        scheduled = False
        for symbol, position in positions.items():
            if symbol in pending_sells:
                continue
            context = by_symbol[symbol]
            index = context.date_to_index.get(current_date)
            if index is None:
                continue
            reasons = self._sell_reasons(context, index, position, config)
            if reasons:
                pending_sells[symbol] = _PendingSell(current_date, "、".join(reasons))
                scheduled = True
                continue
            if config.max_hold_days and self._held_enough_bars(
                context, position, index, config.max_hold_days
            ):
                pending_sells[symbol] = _PendingSell(current_date, "持有到期")
                scheduled = True
        return scheduled

    def _held_enough_bars(
        self,
        context: _StockSeries,
        position: _Position,
        current_index: int,
        max_hold_days: int,
    ) -> bool:
        """按**交易日**计数：用行情序列自身的 bar 索引，不受停牌和长假影响。

        今天收盘时结算"已持有几个交易日"（买入日和今天都算进去），达到上限就登记
        卖出、由次日开盘成交。这样实际持仓正好等于 max_hold_days 个交易日 ——
        直接用索引差判断会多拿一天，因为卖出只能发生在下一个开盘。
        """

        entry_index = context.date_to_index.get(position.entry_date)
        if entry_index is None:
            return False
        held_bars = current_index - entry_index + 1
        return held_bars >= max_hold_days

    def _sell_reasons(
        self,
        context: _StockSeries,
        index: int,
        position: _Position,
        config: PortfolioBacktestConfig,
    ) -> list[str]:
        reasons: list[str] = []
        if bool(context.sell_mask[index]):
            reasons.append("卖出条件")
        return reasons

    def _select_candidates(
        self,
        current_date: int,
        positions: Mapping[str, _Position],
        pending_sells: Mapping[str, _PendingSell],
        by_symbol: Mapping[str, _StockSeries],
        last_prices: Mapping[str, float],
        cash: float,
        total_value: float,
        config: PortfolioBacktestConfig,
        fitness_filter: RollingFitnessFilter | None,
        cooldown_until: Mapping[str, int],
        date_index: int,
        *,
        include_event: bool = True,
        ranked_contexts: Sequence[_StockSeries] | None = None,
        ranked_context_indexes: Iterable[int] | None = None,
    ) -> tuple[list[_PendingBuy], dict[str, PortfolioJsonValue]]:
        excluded = set(positions) | set(pending_sells)
        slots = max(0, config.max_positions - len(positions) + len(pending_sells))
        entries: list[tuple[float, _StockSeries, RollingFitnessDecision | None]] = []
        if (
            ranked_context_indexes is not None
            and ranked_contexts is not None
            and fitness_filter is None
            and not include_event
        ):
            if slots > 0:
                for context_index in ranked_context_indexes:
                    context = ranked_contexts[int(context_index)]
                    if context.symbol in excluded:
                        continue
                    if date_index < cooldown_until.get(context.symbol, -1):
                        continue
                    index = context.date_to_index.get(current_date)
                    if index is None or not bool(context.buy_mask[index]):
                        continue
                    score = self._candidate_score(context, index, config)
                    if score is None or not np.isfinite(score):
                        continue
                    entries.append((score, context, None))
                    if len(entries) >= slots:
                        break
        else:
            for context in by_symbol.values():
                if context.symbol in excluded:
                    continue
                if date_index < cooldown_until.get(context.symbol, -1):
                    continue
                index = context.date_to_index.get(current_date)
                if index is None:
                    continue
                if not bool(context.buy_mask[index]):
                    continue
                score = self._candidate_score(context, index, config)
                if score is None or not np.isfinite(score):
                    continue
                decision = (
                    fitness_filter.decide(context.symbol, current_date) if fitness_filter else None
                )
                entries.append((score, context, decision))
        weighted = (
            self._weighted_scores(entries, config)
            if fitness_filter is not None and config.fitness_rank_weight > 0
            else None
        )
        sort_values: list[float] = []
        for position, (raw, _context, _decision) in enumerate(entries):
            if weighted is not None:
                sort_values.append(weighted[position])
            elif config.rank_order == "desc":
                sort_values.append(raw)
            else:
                sort_values.append(-raw)
        candidates = [(sort_values[position], entry[1]) for position, entry in enumerate(entries)]
        selected = heapq.nsmallest(slots, candidates, key=_candidate_sort_key)
        slot_budget = total_value / config.max_positions if config.max_positions else cash
        pending = [
            _PendingBuy(context.symbol, current_date, slot_budget) for _, context in selected
        ]
        if not include_event:
            return pending, {
                "date": _date_text(current_date),
                "slots_available": slots,
                "ranking_value": config.ranking_value,
                "candidates": [],
            }
        ranked_candidates = heapq.nsmallest(
            max(slots, 10),
            candidates,
            key=_candidate_sort_key,
        )
        event_candidates: list[dict[str, PortfolioJsonValue]] = []
        selected_symbols = {context.symbol for _, context in selected}
        raw_by_symbol = {context.symbol: raw for raw, context, _ in entries}
        decisions = {context.symbol: decision for _, context, decision in entries}
        weighted_by_symbol = (
            {
                context.symbol: weighted[position]
                for position, (_raw, context, _decision) in enumerate(entries)
            }
            if weighted is not None
            else None
        )
        for rank, (_value, context) in enumerate(ranked_candidates, start=1):
            candidate: dict[str, PortfolioJsonValue] = {
                "rank": rank,
                "market": context.market,
                "code": context.code,
                "name": context.name,
                "score": raw_by_symbol[context.symbol],
                "selected": context.symbol in selected_symbols,
            }
            if weighted_by_symbol is not None:
                candidate["rank_score"] = weighted_by_symbol[context.symbol]
            decision = decisions.get(context.symbol)
            if decision is not None:
                candidate.update(
                    {
                        "fitness_score": decision.score,
                        "fitness_trades": decision.trades,
                        "fitness_note": decision.note,
                    }
                )
            event_candidates.append(candidate)
        return pending, {
            "date": _date_text(current_date),
            "slots_available": slots,
            "ranking_value": config.ranking_value,
            "candidates": event_candidates,
        }

    @staticmethod
    def _candidate_score(
        context: _StockSeries,
        index: int,
        config: PortfolioBacktestConfig,
    ) -> float | None:
        if context.priority_score is not None:
            return context.priority_score
        values = context.values.get(config.ranking_value)
        if values is None:
            return None
        score = float(values[index])
        return score if np.isfinite(score) else None

    @staticmethod
    def _weighted_scores(
        entries: list[tuple[float, _StockSeries, RollingFitnessDecision | None]],
        config: PortfolioBacktestConfig,
    ) -> list[float]:
        """Blend cross-sectional ranking with decayed fitness scores."""
        weight = config.fitness_rank_weight
        percentiles = _percentile_ranks([entry[0] for entry in entries])
        fitness_raw: list[float] = []
        decay: list[float] = []
        for _raw, _context, decision in entries:
            score = float(decision.score) if decision is not None else 0.0
            trades = float(decision.trades) if decision is not None else 0.0
            fitness_raw.append(score / 100.0)
            decay.append(min(1.0, trades / config.fitness_min_trades))
        neutral = _decay_weighted_mean(fitness_raw, decay)
        results: list[float] = []
        for position, percentile in enumerate(percentiles):
            oriented = percentile if config.rank_order == "desc" else 1.0 - percentile
            component = decay[position] * fitness_raw[position] + (1.0 - decay[position]) * neutral
            results.append((1.0 - weight) * oriented + weight * component)
        return results

    @staticmethod
    def _should_refresh(
        index: int,
        dates: list[int],
        frequency: str,
    ) -> bool:
        if index == 0 or frequency == "daily" or index == len(dates) - 1:
            return True
        current = pd.to_datetime(str(dates[index]), format="%Y%m%d")
        following = pd.to_datetime(str(dates[index + 1]), format="%Y%m%d")
        if frequency == "weekly":
            return int(current.isocalendar().week) != int(following.isocalendar().week)
        return (current.year, current.month) != (following.year, following.month)

    def _corporate_actions_for_symbol(self, context: _StockSeries) -> tuple[CorporateAction, ...]:
        """该标的的分红送转事件；适配器不支持时退化成「无事件」。

        批量行情路径已经在 :meth:`_preload_corporate_actions` 里把整批标的的事件写进
        缓存；非批量适配器（以及单股路径）在这里按需查一次并累积进同一份缓存，避免
        同一标的被反复查。这里不复用 `_corporate_actions(ref)`，因为那是 context
        mixin 的方法，跨 mixin 引用会让 mypy 看不见。
        """

        cache: dict[tuple[str, str], tuple[CorporateAction, ...]] | None = getattr(
            self, "_corporate_action_cache", None
        )
        if cache is None:
            cache = {}
            self._corporate_action_cache = cache
        key = (context.market, context.code)
        cached = cache.get(key)
        if cached is not None:
            return cached
        actions: tuple[CorporateAction, ...] = ()
        reader = getattr(self._adapter, "read_corporate_actions", None)
        if callable(reader):
            actions = tuple(
                reader(
                    StockRef(
                        market=cast("MarketCode", context.market),
                        code=context.code,
                        path=Path(),
                        instrument_type=context.instrument_type,
                        board=context.board,
                        name=context.name,
                    )
                )
            )
        cache[key] = actions
        return actions

    def _apply_corporate_actions(
        self,
        current_date: int,
        positions: Mapping[str, _Position],
        by_symbol: Mapping[str, _StockSeries],
        cash: float,
        cache: dict[str, tuple[CorporateAction, ...]],
    ) -> float:
        """把当天或此前到期的分红送转落到当前有 bar 的持仓上。

        成交价是 raw（不复权）价，所以除权日或其后的首根实际 bar 起报价已经按
        ``(前收盘 - 每股派息) / (1 + 每股送转)`` 掉下来了。以前股数与成本价都不动，
        于是 10 送 10 之后市值直接腰斩且再也回不来，现金分红也从没入过账——跨越
        6~7 月分红高峰的多年回测，净值系统性偏低，看起来像是策略不行。

        这里按持有者的真实现金流处理：先把每股派息按**调整前的股数**计入可用资金
        （不计税：股息红利税取决于持股期限，回测不知道账户也没打算建模），再把股数
        乘以 ``1 + share_ratio``，并把成本价与持仓期最高价换算到除权后的价格尺度。
        """

        for symbol, position in positions.items():
            if position.size <= 0:
                continue
            context = by_symbol.get(symbol)
            if context is None or current_date not in context.date_to_index:
                # A halted stock keeps its prior mark until its next real quote. Apply due
                # actions on that bar, before orders can execute against the new price scale.
                continue
            actions = cache.get(symbol)
            if actions is None:
                actions = self._corporate_actions_for_symbol(context)
                cache[symbol] = actions
            for action in sorted(actions, key=lambda item: _action_date_int(item.ex_date)):
                action_date = _action_date_int(action.ex_date)
                action_key = (
                    f"{action_date}:{action.share_ratio:.17g}:{action.cash_per_share:.17g}:"
                    f"{action.factor_ratio:.17g}:{action.action_type}"
                )
                if (
                    action.is_neutral
                    or action_date <= position.entry_date
                    or action_date > current_date
                    or action_key in position.applied_corporate_actions
                ):
                    continue
                ratio = 1.0 + float(action.share_ratio)
                if ratio <= 0:
                    continue
                cash += position.size * float(action.cash_per_share)
                position.size *= ratio
                position.entry_price = (position.entry_price - float(action.cash_per_share)) / ratio
                position.highest_price = (
                    position.highest_price - float(action.cash_per_share)
                ) / ratio
                position.applied_corporate_actions.add(action_key)
        return cash

    @staticmethod
    def _portfolio_value(
        positions: Mapping[str, _Position],
        last_prices: Mapping[str, float],
        cash: float,
    ) -> tuple[float, float]:
        position_value = sum(
            position.size * last_prices.get(symbol, position.entry_price)
            for symbol, position in positions.items()
        )
        return cash + position_value, position_value

    @staticmethod
    def _state_record(
        current_date: int,
        positions: Mapping[str, _Position],
        last_prices: Mapping[str, float],
        cash: float,
        total_value: float,
        position_value: float,
    ) -> dict[str, PortfolioJsonValue]:
        holdings = [
            {
                "market": position.market,
                "code": position.code,
                "name": position.name or "",
                "instrument_type": position.instrument_type,
                "board": position.board,
                "size": float(position.size),
                "entry_price": float(position.entry_price),
                "entry_date": _date_text(position.entry_date),
                "close": float(last_prices.get(symbol, position.entry_price)),
                "unrealized_pnl": float(
                    (last_prices.get(symbol, position.entry_price) - position.entry_price)
                    * position.size
                ),
            }
            for symbol, position in sorted(positions.items())
        ]
        return {
            "date": _date_text(current_date),
            "cash": float(cash),
            "position_value": float(position_value),
            "total": float(total_value),
            "positions_count": len(holdings),
            "holdings": holdings,
        }


def _percentile_ranks(values: list[float]) -> list[float]:
    """Average-rank percentile in [0, 1]; tied values share the mean rank."""
    count = len(values)
    if count <= 1:
        return [0.5] * count
    array = np.asarray(values, dtype=float)
    order = np.argsort(array, kind="stable")
    ordered = array[order]
    ranks = np.empty(count, dtype=float)
    start = 0
    while start < count:
        stop = start
        while stop + 1 < count and ordered[stop + 1] == ordered[start]:
            stop += 1
        ranks[order[start : stop + 1]] = (start + stop) / 2.0
        start = stop + 1
    return [float(value) for value in ranks / (count - 1)]


def _decay_weighted_mean(values: list[float], weights: list[float]) -> float:
    """Neutral fill-in for candidates whose history is too thin to trust."""
    total = sum(weights)
    if total <= 0:
        return 0.0
    return sum(value * weight for value, weight in zip(values, weights)) / total


def _candidate_sort_key(item: tuple[float, _StockSeries]) -> tuple[float, str]:
    return (-item[0], item[1].symbol)


__all__ = ["PortfolioSimulationMixin"]
