"""Immutable configuration and serializable result models for formula backtests."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from selector_app.market_data.day_format import classify_board, classify_instrument
from selector_app.market_data.models import InstrumentBoard, InstrumentType, MarketCode
from selector_app.screening.rules import RuleGroup

ExecutionMode = Literal["next_open", "next_close"]
PositionMode = Literal["full", "fixed"]
BacktestValue = float | int | bool | str | None

_CODE_PATTERN = re.compile(r"^\d{6}$")


@dataclass(frozen=True)
class BacktestConfig:
    """Configuration for one single-stock daily formula backtest."""

    market: MarketCode
    code: str
    vipdoc_path: str | Path
    buy_signal: str | None = None
    sell_signal: str | None = None
    formula_text: str | None = None
    formula_parameters: Mapping[str, float] = field(default_factory=dict)
    start_date: date | None = None
    end_date: date | None = None
    initial_cash: float = 100_000.0
    commission: float = 0.0001
    min_commission: float = 0.1
    stamp_tax: float = 0.001
    slippage: float = 0.0
    execution: ExecutionMode = "next_open"
    position_mode: PositionMode = "full"
    fixed_size: int | None = None
    instrument_type: InstrumentType | None = None
    board: InstrumentBoard | None = None
    buy_rule: RuleGroup | None = None
    sell_rule: RuleGroup | None = None
    # 「持有到期末」：没有卖出规则时也不报错，持仓一直留到最后一根 K 线按收盘价平掉。
    # 买卖条件研究靠它表达「我只给买入条件，卖出时机让研究去找」。
    close_at_end: bool = False
    # 「持续满仓」：close_at_end 的对偶。没有买入规则时也不报错，每根 K 线都是候选买点
    # （实际表现是卖出后下一个交易日立即再买入）。买卖条件研究靠它表达
    # 「我只给卖出条件，买入时机让研究去找」。
    always_in_market: bool = False

    def __post_init__(self) -> None:
        code = self.code.strip()
        buy_signal = self.buy_signal.strip() if self.buy_signal is not None else None
        sell_signal = self.sell_signal.strip() if self.sell_signal is not None else None
        if self.market not in {"SH", "SZ"} or not _CODE_PATTERN.fullmatch(code):
            raise ValueError("股票市场和代码格式无效")
        detected_type = classify_instrument(self.market, code)
        detected_board = classify_board(self.market, code)
        if detected_type is None or detected_board is None:
            raise ValueError(f"不支持回测的行情品种: {self.market} {code}")
        if self.instrument_type is not None and self.instrument_type != detected_type:
            raise ValueError("回测品种类型与市场代码不一致")
        if self.board is not None and self.board != detected_board:
            raise ValueError("回测板块与市场代码不一致")
        explicit_buy_rule = self.buy_rule is not None
        explicit_sell_rule = self.sell_rule is not None
        if self.buy_rule is None and buy_signal:
            object.__setattr__(self, "buy_rule", RuleGroup(signals=(buy_signal,)))
        if self.sell_rule is None and sell_signal:
            object.__setattr__(self, "sell_rule", RuleGroup(signals=(sell_signal,)))
        if self.buy_rule is None:
            if not self.always_in_market:
                raise ValueError("买入规则不能为空，或者开启「持续满仓」由系统在每个交易日尝试入场")
        if self.sell_rule is None and not self.close_at_end:
            raise ValueError("卖出规则不能为空，或者开启「持有到期末」由系统在最后一根 K 线平仓")
        if not explicit_buy_rule and not explicit_sell_rule and buy_signal == sell_signal:
            raise ValueError("买入信号和卖出信号不能相同")
        if self.execution not in {"next_open", "next_close"}:
            raise ValueError("不支持的成交方式")
        if self.position_mode not in {"full", "fixed"}:
            raise ValueError("不支持的仓位方式")
        if self.start_date is not None and self.end_date is not None:
            if self.start_date > self.end_date:
                raise ValueError("回测开始日期不能晚于结束日期")
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
        if self.position_mode == "fixed":
            if (
                self.fixed_size is None
                or isinstance(self.fixed_size, bool)
                or self.fixed_size < 100
                or self.fixed_size % 100 != 0
            ):
                raise ValueError("固定股数必须是 100 的整数倍且不小于 100")
        elif self.fixed_size is not None:
            raise ValueError("全仓模式不应设置固定股数")
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "instrument_type", detected_type)
        object.__setattr__(self, "board", detected_board)
        object.__setattr__(self, "buy_signal", buy_signal)
        object.__setattr__(self, "sell_signal", sell_signal)
        formula_text = self.formula_text.strip() if self.formula_text else None
        object.__setattr__(self, "formula_text", formula_text or None)
        object.__setattr__(
            self, "formula_parameters", MappingProxyType(dict(self.formula_parameters))
        )


@dataclass(frozen=True)
class BacktestReport:
    """JSON-ready historical backtest output."""

    market: MarketCode
    code: str
    bars: int
    start_date: str
    end_date: str
    buy_signal: str | None
    sell_signal: str | None
    performance: Mapping[str, float | None]
    equity_curve: tuple[Mapping[str, BacktestValue], ...]
    trades: tuple[Mapping[str, BacktestValue], ...]
    positions: tuple[Mapping[str, BacktestValue], ...]
    configuration: Mapping[str, BacktestValue]
    diagnostic: str | None = None
    name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "performance", MappingProxyType(dict(self.performance)))
        object.__setattr__(self, "configuration", MappingProxyType(dict(self.configuration)))

    def to_dict(self) -> dict[str, object]:
        return {
            "market": self.market,
            "code": self.code,
            "name": self.name,
            "bars": self.bars,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "buy_signal": self.buy_signal,
            "sell_signal": self.sell_signal,
            "performance": dict(self.performance),
            "equity_curve": [dict(row) for row in self.equity_curve],
            "trades": [dict(row) for row in self.trades],
            "positions": [dict(row) for row in self.positions],
            "configuration": dict(self.configuration),
            "diagnostic": self.diagnostic,
        }
