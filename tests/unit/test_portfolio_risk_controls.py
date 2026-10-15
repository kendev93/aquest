from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from aquest.adapters.local_day_adapter import StockRef
from aquest.portfolio_backtest.models import PortfolioBacktestConfig
from aquest.portfolio_backtest.service import PortfolioBacktestService
from aquest.screening.models import ScreenCondition
from aquest.screening.rules import RuleGroup


def frame(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=len(rows), freq="D"),
            "open": [row[0] for row in rows],
            "high": [row[1] for row in rows],
            "low": [row[2] for row in rows],
            "close": [row[3] for row in rows],
            "volume": np.full(len(rows), 100_000.0),
            "amount": [row[3] * 100_000.0 for row in rows],
        }
    )


class OneStockAdapter:
    def __init__(self, bars: pd.DataFrame) -> None:
        self.bars = bars

    def list_stock_refs(self, vipdoc_path, universe, universe_file=None):
        return [StockRef(market="SH", code="600000", path=Path(vipdoc_path) / "sh600000.day")]

    def read_stock(self, ref: StockRef) -> pd.DataFrame:
        return self.bars.copy(deep=True)


class TwoStockAdapter:
    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        self.frames = frames

    def list_stock_refs(self, vipdoc_path, universe, universe_file=None):
        return [
            StockRef(market="SH", code=code, path=Path(vipdoc_path) / f"sh{code}.day")
            for code in self.frames
        ]

    def read_stock(self, ref: StockRef) -> pd.DataFrame:
        return self.frames[ref.code].copy(deep=True)


def config(**overrides: object) -> PortfolioBacktestConfig:
    values: dict[str, object] = {
        "vipdoc_path": "/tmp/vipdoc",
        "universe": "all",
        "selected_signals": ("custom.buy",),
        "combine_mode": "any",
        "minimum_matches": None,
        "ranking_value": "custom.rank",
        "rank_order": "desc",
        "max_positions": 1,
        "rebalance_frequency": "daily",
        "formula_text": "BUY:C>0; RANK:C;",
        "start_date": None,
        "end_date": None,
        "initial_cash": 10_000.0,
        "commission": 0.0,
        "min_commission": 0.0,
        "stamp_tax": 0.0,
        "slippage": 0.0,
    }
    values.update(overrides)
    return PortfolioBacktestConfig(**values)


def rule_config(**overrides: object) -> PortfolioBacktestConfig:
    """带显式买入规则的配置。

    一旦给了 ``symbol_strategies``，标的自己没带 buy_rule 时会回落到全局
    ``config.buy_rule``；只靠 selected_signals 的写法在这里不成立，所以每标的
    口径的用例都显式带上规则。
    """

    buy = RuleGroup(
        signals=("custom.buy",),
        conditions=(ScreenCondition(left_value="custom.rank", operator="gt", right_constant=0.0),),
        combine_mode="any",
    )
    return config(buy_rule=buy, **overrides)


def sells_of(report: object) -> list[dict[str, object]]:
    return [dict(trade) for trade in report.trades if trade["direction"] == "SELL"]  # type: ignore[attr-defined]


def holdings_of(report: object) -> list[dict[str, object]]:
    state = getattr(report, "current_state", None) or {}
    return [dict(row) for row in state.get("holdings", [])]


# 建仓口径：第 1 根收盘出信号，第 2 根开盘 10.0 成交，所以风控线以 10.0 为基准。
ENTRY_BARS = [(10.0, 10.5, 9.5, 10.0), (10.0, 10.5, 9.8, 10.0)]


def test_stop_loss_triggers_on_the_breach_bar_and_fills_at_the_next_open() -> None:
    report = PortfolioBacktestService(
        adapter=OneStockAdapter(frame([*ENTRY_BARS, (9.0, 9.5, 8.5, 9.4), (9.2, 9.6, 9.1, 9.5)]))
    ).run(config(stop_loss_pct=0.1, stop_loss_cooldown_days=2))

    sells = sells_of(report)
    assert len(sells) == 1
    # 第 3 根最低 8.5 跌破 9.0 的止损线，但成交落在第 4 根开盘，不再是止损价。
    assert sells[0]["signal_date"] == "2024-01-03"
    assert sells[0]["date"] == "2024-01-04"
    assert sells[0]["price"] == 9.2
    assert sells[0]["risk_trigger"] == "stop_loss"
    assert sells[0]["reason"] == "止损"
    assert sells[0]["cooldown_days"] == 2


def test_next_close_execution_pays_the_following_bar_close() -> None:
    """执行方式选「下一根收盘」时，风控卖单跟着落到次日收盘，仍不是触发当根。"""

    report = PortfolioBacktestService(
        adapter=OneStockAdapter(frame([*ENTRY_BARS, (9.0, 9.5, 8.5, 9.4), (9.2, 9.6, 9.1, 9.5)]))
    ).run(config(stop_loss_pct=0.1, stop_loss_cooldown_days=2, execution="next_close"))

    sells = sells_of(report)
    assert len(sells) == 1
    # 建仓也走次日收盘，两根的收盘价都是 10.0，所以止损线仍是 9.0。
    assert sells[0]["signal_date"] == "2024-01-03"
    assert sells[0]["date"] == "2024-01-04"
    assert sells[0]["price"] == 9.5
    assert sells[0]["risk_trigger"] == "stop_loss"


def test_breach_bar_leaves_the_position_open_with_a_pending_risk_sell() -> None:
    report = PortfolioBacktestService(
        adapter=OneStockAdapter(frame([*ENTRY_BARS, (9.0, 9.5, 8.5, 9.4)]))
    ).run(config(stop_loss_pct=0.1, stop_loss_cooldown_days=2))

    # 回放停在触发当日：没有下一根可成交，所以既不平仓也不留成交记录。
    assert sells_of(report) == []
    held = holdings_of(report)
    assert [row["code"] for row in held] == ["600000"]
    assert held[0]["pending_exit_reason"] == "止损"
    assert held[0]["pending_exit_signal_date"] == "2024-01-03"
    assert held[0]["pending_exit_risk_trigger"] == "stop_loss"

    plan = report.next_plan
    assert [order["code"] for order in plan["sells"]] == ["600000"]
    assert plan["sells"][0]["risk_trigger"] == "stop_loss"
    assert plan["sells"][0]["signal_date"] == "2024-01-03"
    # 明日开盘才卖，所以今天收盘它仍然算在持仓里。
    assert [order["code"] for order in plan["holdings"]] == ["600000"]


def test_take_profit_uses_daily_high_but_pays_the_next_open() -> None:
    report = PortfolioBacktestService(
        adapter=OneStockAdapter(
            frame([*ENTRY_BARS, (10.5, 12.0, 10.2, 10.5), (10.8, 11.0, 10.6, 10.9)])
        )
    ).run(config(take_profit_pct=0.1))

    sells = sells_of(report)
    assert len(sells) == 1
    # 止盈线 11.0 在判定当根被最高价穿过，成交价仍是次日开盘。
    assert sells[0]["signal_date"] == "2024-01-03"
    assert sells[0]["price"] == 10.8
    assert sells[0]["risk_trigger"] == "take_profit"
    assert sells[0]["reason"] == "止盈"


def test_gap_through_the_level_still_pays_the_next_open() -> None:
    stop_report = PortfolioBacktestService(
        adapter=OneStockAdapter(frame([*ENTRY_BARS, (8.0, 8.5, 7.5, 8.0), (7.6, 7.9, 7.4, 7.8)]))
    ).run(config(stop_loss_pct=0.1))
    take_report = PortfolioBacktestService(
        adapter=OneStockAdapter(
            frame([*ENTRY_BARS, (12.0, 12.5, 11.5, 12.0), (11.8, 12.2, 11.6, 12.0)])
        )
    ).run(config(take_profit_pct=0.1))

    # 触发当根的跳空价既不是成交价也不该被当成能拿到的价：只按次日开盘成交。
    assert sells_of(stop_report)[0]["price"] == 7.6
    assert sells_of(stop_report)[0]["signal_date"] == "2024-01-03"
    assert sells_of(take_report)[0]["price"] == 11.8
    assert sells_of(take_report)[0]["signal_date"] == "2024-01-03"


def test_trailing_stop_uses_highest_daily_high_and_pays_the_next_open() -> None:
    report = PortfolioBacktestService(
        adapter=OneStockAdapter(
            frame([*ENTRY_BARS, (11.5, 12.0, 11.3, 11.5), (11.2, 11.4, 11.0, 11.3)])
        )
    ).run(config(trailing_stop_pct=0.05, trailing_stop_cooldown_days=3))

    sells = sells_of(report)
    assert len(sells) == 1
    assert sells[0]["price"] == 11.2
    assert sells[0]["risk_trigger"] == "trailing_stop"
    assert sells[0]["reason"] == "移动止盈"
    assert sells[0]["cooldown_days"] == 3


def test_risk_cooldown_anchors_on_the_fill_bar_and_frees_the_slot_then() -> None:
    dates = pd.date_range("2024-01-01", periods=7, freq="D")
    first = frame(
        [
            (10.0, 10.5, 9.5, 10.0),
            (10.0, 10.5, 9.8, 10.0),
            (9.0, 9.5, 8.5, 9.0),
            (9.0, 9.5, 8.8, 9.0),
            (9.0, 9.5, 8.8, 9.0),
            (9.0, 9.5, 8.8, 9.0),
            (9.0, 9.5, 8.8, 9.0),
        ]
    )
    second = frame([(8.0, 8.5, 7.8, 8.0)] * 7)
    first["date"] = dates
    second["date"] = dates

    report = PortfolioBacktestService(
        adapter=TwoStockAdapter({"600000": first, "600001": second})
    ).run(config(stop_loss_pct=0.1, stop_loss_cooldown_days=2))

    sells = sells_of(report)
    assert [str(trade["date"]) for trade in sells] == ["2024-01-04"]
    buys = [trade for trade in report.trades if trade["direction"] == "BUY"]
    # 空位要等风控卖单成交那天才腾出来，替补与卖出同日、比原口径晚一根。
    assert [(trade["code"], str(trade["date"])) for trade in buys] == [
        ("600000", "2024-01-02"),
        ("600001", "2024-01-04"),
    ]
    # 冷却从成交日（第 4 根）起算 2 根，所以第 5、6 根都不能再买回 600000。
    assert all(
        not (trade["code"] == "600000" and str(trade["date"]) in {"2024-01-05", "2024-01-06"})
        for trade in buys
    )


def test_atr_risk_parameters_require_a_positive_multiplier() -> None:
    try:
        config(stop_loss_mode="atr", stop_loss_atr_multiplier=None)
    except ValueError as exc:
        assert "stop_loss_atr_multiplier" in str(exc)
    else:
        raise AssertionError("ATR 风控缺少倍数时应被拒绝")


def test_atr_stop_loss_uses_wilder_smoothing_from_the_full_history() -> None:
    # 前 14 根真实波幅恒为 2.0，最后两根放大到 4.0：只有一根异常值时 Wilder 与简单
    # 均值恰好相等，必须有第二根把滑窗整体抬起来才分得开。
    rows = [(10.0, 11.0, 9.0, 10.0)] * 14 + [(10.0, 12.0, 8.0, 10.0)] * 2
    wilder = 2.0
    for _ in range(2):
        wilder += (4.0 - wilder) / 14.0
    simple_mean = (12 * 2.0 + 2 * 4.0) / 14.0

    report = PortfolioBacktestService(adapter=OneStockAdapter(frame(rows))).run(
        config(stop_loss_mode="atr", stop_loss_atr_multiplier=1.0)
    )

    held = holdings_of(report)
    assert held[0]["stop_loss_price"] == 10.0 - wilder
    assert held[0]["stop_loss_price"] != 10.0 - simple_mean
    # 线位仍在这根没碰到就不平仓：最低 8.0 高于 7.7245 的止损线。
    assert sells_of(report) == []


def test_per_symbol_atr_period_changes_the_line_the_engine_uses() -> None:
    rows = [(10.0, 11.0, 9.0, 10.0)] * 15 + [(10.0, 12.0, 8.0, 10.0)]
    overrides = {"stop_loss_mode": "atr", "stop_loss_atr_multiplier": 1.0}

    global_period = PortfolioBacktestService(adapter=OneStockAdapter(frame(rows))).run(
        rule_config(**overrides)
    )
    symbol_period = PortfolioBacktestService(adapter=OneStockAdapter(frame(rows))).run(
        rule_config(symbol_strategies={"SH600000": {"atr_period": 3}}, **overrides)
    )

    # 每标的 atr_period 过去被静默丢掉（ATR 只按全局周期算），现在真的生效：
    # 周期 3 的 Wilder 线抬高得更快，止损线更低。
    assert holdings_of(global_period)[0]["stop_loss_price"] == 10.0 - (2.0 + 2.0 / 14.0)
    assert holdings_of(symbol_period)[0]["stop_loss_price"] == 10.0 - (2.0 + 2.0 / 3.0)


def test_per_symbol_max_hold_days_exits_the_portfolio_at_the_next_open() -> None:
    bars = frame([*ENTRY_BARS, (10.2, 10.6, 9.9, 10.4), (10.3, 10.7, 10.1, 10.5)])
    report = PortfolioBacktestService(adapter=OneStockAdapter(bars)).run(
        rule_config(symbol_strategies={"SH600000": {"max_hold_days": 2}})
    )

    sells = sells_of(report)
    assert len(sells) == 1
    # 第 2 根建仓、第 3 根收盘满 2 个交易日，第 4 根开盘卖出。
    assert sells[0]["reason"] == "持有到期"
    assert sells[0]["signal_date"] == "2024-01-03"
    assert sells[0]["date"] == "2024-01-04"
    assert sells[0]["price"] == 10.3
    assert sells[0].get("risk_trigger") is None


def test_holding_expiry_is_reported_with_the_risk_levels() -> None:
    report = PortfolioBacktestService(
        adapter=OneStockAdapter(frame([*ENTRY_BARS, (10.2, 10.6, 9.9, 10.4)]))
    ).run(rule_config(symbol_strategies={"SH600000": {"max_hold_days": 2, "stop_loss_pct": 0.1}}))

    held = holdings_of(report)
    assert held[0]["max_hold_days"] == 2
    assert held[0]["max_hold_days_left"] == 0
    assert held[0]["max_hold_expiry_date"] == "2024-01-03"
    assert held[0]["stop_loss_price"] == 9.0
    assert held[0]["risk_level_as_of"] == "2024-01-03"
    assert held[0]["pending_exit_reason"] == "持有到期"
    # 还没走到最后一根时给不出日期，只报剩余交易日，避免显示一个没依据的日子。
    shorter = PortfolioBacktestService(
        adapter=OneStockAdapter(
            frame([*ENTRY_BARS, (10.2, 10.6, 9.9, 10.4), (10.2, 10.6, 9.9, 10.4)])
        )
    ).run(
        rule_config(
            symbol_strategies={"SH600000": {"max_hold_days": 6, "stop_loss_pct": 0.1}},
        )
    )
    early = holdings_of(shorter)[0]
    assert early["max_hold_days_left"] == 3
    assert early["max_hold_expiry_date"] is None


def test_risk_priority_is_stop_loss_then_trailing_then_take_profit() -> None:
    report = PortfolioBacktestService(
        adapter=OneStockAdapter(
            frame([*ENTRY_BARS, (10.0, 12.0, 8.0, 10.0), (9.8, 10.2, 9.6, 10.0)])
        )
    ).run(config(stop_loss_pct=0.1, trailing_stop_pct=0.05, take_profit_pct=0.1))

    sells = sells_of(report)
    assert len(sells) == 1
    # 同一根既碰到止盈又碰到止损时，先取更坏的那个出口。
    assert sells[0]["risk_trigger"] == "stop_loss"
    assert sells[0]["reason"] == "止损"
    assert sells[0]["signal_date"] == "2024-01-03"
    assert sells[0]["date"] == "2024-01-04"


def test_reusing_a_portfolio_service_does_not_leak_risk_state() -> None:
    bars = frame(
        [
            (10.0, 10.5, 9.5, 10.0),
            (10.0, 10.5, 9.8, 10.0),
            (9.0, 9.5, 8.5, 9.0),
            (9.0, 9.5, 8.8, 9.0),
        ]
    )
    service = PortfolioBacktestService(adapter=OneStockAdapter(bars))
    first = service.run(config(stop_loss_pct=0.1, stop_loss_cooldown_days=2))
    second = service.run(config(stop_loss_pct=0.1, stop_loss_cooldown_days=2))

    assert first.trades == second.trades
    assert first.equity_curve == second.equity_curve
