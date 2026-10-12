"""Serialization and validation for resumable portfolio simulations."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from aquest.market_data.models import InstrumentBoard, InstrumentType

from .models import PortfolioJsonValue
from .simulation_models import _PendingBuy, _PendingSell, _Position


@dataclass(frozen=True)
class SimulationCheckpoint:
    """Serializable state captured after a portfolio simulation prefix.

    The checkpoint intentionally contains the accumulated output rows as well
    as the mutable trading state. A resumed simulation can therefore return
    the same report as a from-scratch run instead of only matching the final
    cash balance.
    """

    last_date: int
    cash: float
    positions: Mapping[str, _Position]
    pending_sells: Mapping[str, _PendingSell]
    pending_buys: tuple[_PendingBuy, ...]
    cooldown_until: Mapping[str, int]
    last_prices: Mapping[str, float]
    peak_value: float
    max_drawdown: float
    processed_dates: tuple[int, ...]
    equity: tuple[Mapping[str, PortfolioJsonValue], ...]
    trades: tuple[Mapping[str, PortfolioJsonValue], ...]
    states: tuple[Mapping[str, PortfolioJsonValue], ...]
    ranking_events: tuple[Mapping[str, PortfolioJsonValue], ...]
    include_details: bool
    data_fingerprint: str = ""

    def __post_init__(self) -> None:
        if not self.processed_dates or self.last_date != self.processed_dates[-1]:
            raise ValueError("模拟检查点日期状态无效")
        if not isinstance(self.include_details, bool):
            raise ValueError("模拟检查点明细开关无效")
        if len(self.equity) != len(self.processed_dates):
            raise ValueError("模拟检查点净值状态长度无效")
        if self.include_details and len(self.states) != len(self.processed_dates):
            raise ValueError("模拟检查点持仓状态长度无效")
        if not self.include_details and (self.states or self.ranking_events):
            raise ValueError("无明细模拟检查点包含明细状态")
        object.__setattr__(self, "positions", dict(self.positions))
        object.__setattr__(self, "pending_sells", dict(self.pending_sells))
        object.__setattr__(self, "pending_buys", tuple(self.pending_buys))
        object.__setattr__(self, "cooldown_until", dict(self.cooldown_until))
        object.__setattr__(self, "last_prices", dict(self.last_prices))
        for field_name in ("equity", "trades", "states", "ranking_events"):
            object.__setattr__(
                self,
                field_name,
                tuple(dict(row) for row in getattr(self, field_name)),
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "version": 2,
            "last_date": self.last_date,
            "cash": self.cash,
            "positions": {
                symbol: _position_to_dict(position) for symbol, position in self.positions.items()
            },
            "pending_sells": {
                symbol: {
                    "signal_date": pending.signal_date,
                    "reason": pending.reason,
                }
                for symbol, pending in self.pending_sells.items()
            },
            "pending_buys": [
                {
                    "symbol": pending.symbol,
                    "signal_date": pending.signal_date,
                    "budget": pending.budget,
                }
                for pending in self.pending_buys
            ],
            "cooldown_until": dict(self.cooldown_until),
            "last_prices": dict(self.last_prices),
            "peak_value": self.peak_value,
            "max_drawdown": self.max_drawdown,
            "processed_dates": list(self.processed_dates),
            "equity": [dict(row) for row in self.equity],
            "trades": [dict(row) for row in self.trades],
            "states": [dict(row) for row in self.states],
            "ranking_events": [dict(row) for row in self.ranking_events],
            "include_details": self.include_details,
            "data_fingerprint": self.data_fingerprint,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> SimulationCheckpoint:
        """Restore a checkpoint and reject malformed or unsafe state."""

        if payload.get("version") != 2:
            raise ValueError("模拟检查点版本不受支持")
        positions_payload = _checkpoint_mapping(payload.get("positions"), "positions")
        pending_sells_payload = _checkpoint_mapping(payload.get("pending_sells"), "pending_sells")
        pending_buys_payload = _checkpoint_list(payload.get("pending_buys"), "pending_buys")
        processed_dates = tuple(
            _checkpoint_int(item, "processed_dates")
            for item in _checkpoint_list(payload.get("processed_dates"), "processed_dates")
        )
        positions = {
            str(symbol): _position_from_dict(str(symbol), value)
            for symbol, value in positions_payload.items()
        }
        pending_sells = {
            str(symbol): _PendingSell(
                signal_date=_checkpoint_int(
                    _checkpoint_mapping(value, "pending_sells"), "signal_date"
                ),
                reason=str(_checkpoint_mapping(value, "pending_sells").get("reason") or ""),
            )
            for symbol, value in pending_sells_payload.items()
        }
        pending_buys = tuple(
            _PendingBuy(
                symbol=str(_checkpoint_mapping(value, "pending_buys").get("symbol") or ""),
                signal_date=_checkpoint_int(
                    _checkpoint_mapping(value, "pending_buys"), "signal_date"
                ),
                budget=_checkpoint_float(_checkpoint_mapping(value, "pending_buys"), "budget"),
            )
            for value in pending_buys_payload
        )
        cooldown_until = {
            str(symbol): _checkpoint_int(value, "cooldown_until")
            for symbol, value in _checkpoint_mapping(
                payload.get("cooldown_until"), "cooldown_until"
            ).items()
        }
        last_prices = {
            str(symbol): _checkpoint_float(value, "last_prices")
            for symbol, value in _checkpoint_mapping(
                payload.get("last_prices"), "last_prices"
            ).items()
        }

        include_details = payload.get("include_details")
        if not isinstance(include_details, bool):
            raise ValueError("模拟检查点字段 include_details 必须是布尔值")
        return cls(
            last_date=_checkpoint_int(payload, "last_date"),
            cash=_checkpoint_float(payload, "cash"),
            positions=positions,
            pending_sells=pending_sells,
            pending_buys=pending_buys,
            cooldown_until=cooldown_until,
            last_prices=last_prices,
            peak_value=_checkpoint_float(payload, "peak_value"),
            max_drawdown=_checkpoint_float(payload, "max_drawdown"),
            processed_dates=processed_dates,
            equity=_checkpoint_records(payload, "equity"),
            trades=_checkpoint_records(payload, "trades"),
            states=_checkpoint_records(payload, "states"),
            ranking_events=_checkpoint_records(payload, "ranking_events"),
            include_details=include_details,
            data_fingerprint=str(payload.get("data_fingerprint") or ""),
        )


def _checkpoint_mapping(payload: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(payload, Mapping):
        raise ValueError(f"模拟检查点字段 {field_name} 必须是对象")
    return payload


def _checkpoint_list(payload: object, field_name: str) -> list[object]:
    if not isinstance(payload, list):
        raise ValueError(f"模拟检查点字段 {field_name} 必须是数组")
    return payload


def _checkpoint_int(payload: object, field_name: str) -> int:
    value = payload.get(field_name) if isinstance(payload, Mapping) else payload
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"模拟检查点字段 {field_name} 必须是整数")
    return int(value)


def _checkpoint_float(payload: object, field_name: str) -> float:
    value = payload.get(field_name) if isinstance(payload, Mapping) else payload
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"模拟检查点字段 {field_name} 必须是数字")
    numeric_value = float(value)
    if not math.isfinite(numeric_value):
        raise ValueError(f"模拟检查点字段 {field_name} 必须是有限数字")
    return numeric_value


def _checkpoint_records(
    payload: Mapping[str, object], field_name: str
) -> tuple[Mapping[str, PortfolioJsonValue], ...]:
    return tuple(
        dict(_checkpoint_mapping(row, field_name))
        for row in _checkpoint_list(payload.get(field_name), field_name)
    )


def _position_to_dict(position: _Position) -> dict[str, object]:
    return {
        "market": position.market,
        "code": position.code,
        "instrument_type": position.instrument_type,
        "board": position.board,
        "size": position.size,
        "entry_price": position.entry_price,
        "entry_date": position.entry_date,
        "highest_price": position.highest_price,
        "buy_commission": position.buy_commission,
        "buy_slippage": position.buy_slippage,
        "name": position.name,
        "applied_corporate_actions": sorted(position.applied_corporate_actions),
    }


def _position_from_dict(symbol: str, payload: object) -> _Position:
    values = _checkpoint_mapping(payload, f"positions[{symbol}]")
    market = str(values.get("market") or "")
    code = str(values.get("code") or "")
    instrument_type = str(values.get("instrument_type") or "")
    board = str(values.get("board") or "")
    if not market or not code or f"{market}{code}" != symbol:
        raise ValueError(f"模拟检查点持仓标识无效: {symbol}")
    if instrument_type != "stock":
        raise ValueError(f"模拟检查点持仓类型无效: {symbol}")
    if board not in {"main", "star", "chinext"}:
        raise ValueError(f"模拟检查点持仓板块无效: {symbol}")
    size = _checkpoint_float(values.get("size"), f"positions[{symbol}].size")
    entry_price = _checkpoint_float(values.get("entry_price"), f"positions[{symbol}].entry_price")
    highest_price = _checkpoint_float(
        values.get("highest_price"), f"positions[{symbol}].highest_price"
    )
    if size <= 0 or entry_price <= 0 or highest_price <= 0:
        raise ValueError(f"模拟检查点持仓数值无效: {symbol}")
    raw_actions = values.get("applied_corporate_actions")
    if not isinstance(raw_actions, list) or any(
        not isinstance(value, str) for value in raw_actions
    ):
        raise ValueError(f"模拟检查点字段 positions[{symbol}].applied_corporate_actions 无效")
    return _Position(
        market=market,
        code=code,
        instrument_type=cast(InstrumentType, instrument_type),
        board=cast(InstrumentBoard, board),
        size=size,
        entry_price=entry_price,
        entry_date=_checkpoint_int(values.get("entry_date"), f"positions[{symbol}].entry_date"),
        highest_price=highest_price,
        buy_commission=_checkpoint_float(
            values.get("buy_commission"), f"positions[{symbol}].buy_commission"
        ),
        buy_slippage=_checkpoint_float(
            values.get("buy_slippage"), f"positions[{symbol}].buy_slippage"
        ),
        name=(None if values.get("name") is None else str(values.get("name"))),
        applied_corporate_actions=set(raw_actions),
    )


__all__ = ["SimulationCheckpoint"]
