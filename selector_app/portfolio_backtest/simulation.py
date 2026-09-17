"""Portfolio state-machine and order execution logic."""

from __future__ import annotations

import heapq
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import replace

import numpy as np
import pandas as pd

from selector_app.backtest.profiles import TradeProfile, profile_from_config
from selector_app.strategy_fitness.rolling import RollingFitnessDecision, RollingFitnessFilter

from .checkpoint import SimulationCheckpoint
from .context import _StockSeries
from .models import PortfolioBacktestConfig, PortfolioJsonValue
from .report import _date_text
from .risk import _risk_trigger, _risk_trigger_label
from .simulation_models import _PendingBuy, _PendingSell, _Position
from .state import _PortfolioServiceState


class PortfolioSimulationMixin(_PortfolioServiceState):
    """Execute deterministic portfolio state transitions and order fills."""

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
        return {
            "cash": float(config.initial_cash),
            "equity": equity,
            "states": states,
            "trades": [],
            "ranking_events": ranking_events,
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
    ) -> dict[str, object]:
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
                symbol: replace(position) for symbol, position in checkpoint.positions.items()
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

        for date_index in range(start_index, len(dates)):
            current_date = dates[date_index]
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
                )
                if include_details:
                    ranking_events.append(event)

        return {
            "cash": cash,
            "equity": equity,
            "states": states,
            "trades": trades,
            "ranking_events": ranking_events,
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
            trigger = _risk_trigger(context, index, position, config)
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
            shares = (
                int(max(pending.budget, 0) / price / profile.lot_size) * profile.lot_size
                if price > 0
                else 0
            )
            shares = self._affordable_shares(shares, price, cash, profile, config.slippage)
            if shares <= 0:
                continue
            commission = max(shares * price * profile.commission, profile.min_commission)
            slippage = shares * config.slippage
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
        return scheduled

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
    ) -> tuple[list[_PendingBuy], dict[str, PortfolioJsonValue]]:
        excluded = set(positions) | set(pending_sells)
        entries: list[tuple[float, _StockSeries, RollingFitnessDecision | None]] = []
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
            score = float(context.values[config.ranking_value][index])
            if not np.isfinite(score):
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
        slots = max(0, config.max_positions - len(positions) + len(pending_sells))
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
                "name": position.name,
                "instrument_type": position.instrument_type,
                "board": position.board,
                "size": float(position.size),
                "entry_price": float(position.entry_price),
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
