from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import numpy as np
import pandas as pd
import pytest

from selector_app.adapters.local_day_adapter import StockRef
from selector_app.formulas.custom import parse_formula
from selector_app.individual_research.service import _report_metrics
from selector_app.jit import fast_sim_stats
from selector_app.market_data.adjustment import CorporateAction, compute_factor_ratio
from selector_app.portfolio_backtest.context import SortedDateIndex
from selector_app.portfolio_backtest.models import (
    PortfolioBacktestConfig,
    PortfolioBacktestReport,
)
from selector_app.portfolio_backtest.service import (
    PortfolioBacktestService,
    SimulationCheckpoint,
    _StockSeries,
)
from selector_app.portfolio_backtest.simulation import PortfolioSimulationMixin
from selector_app.screening.models import ScreenCondition
from selector_app.screening.rules import RuleGroup
from selector_app.strategy_fitness.rolling import RollingFitnessDecision


def stock_frame(close: list[float]) -> pd.DataFrame:
    values = np.asarray(close, dtype=float)
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=len(values), freq="D"),
            "open": values,
            "high": values + 0.5,
            "low": values - 0.5,
            "close": values,
            "volume": np.full(len(values), 100_000.0),
            "amount": values * 100_000.0,
        }
    )


class FakeAdapter:
    def __init__(self) -> None:
        self.frames = {
            "600000": stock_frame([10, 11, 12, 13, 14, 15]),
            "600001": stock_frame([20, 19, 18, 17, 16, 15]),
            "600002": stock_frame([5, 6, 7, 8, 9, 10]),
        }

    def list_stock_refs(self, vipdoc_path, universe, universe_file=None):
        return [
            StockRef(
                market="SH",
                code=code,
                name=f"测试{code}",
                path=Path(vipdoc_path) / f"sh{code}.day",
            )
            for code in self.frames
        ]

    def stock_ref(self, vipdoc_path, market, code):
        normalized = code.strip()
        if normalized not in self.frames or market != "SH":
            raise ValueError(f"找不到已导入的行情: {market} {normalized}")
        return StockRef(
            market=market,
            code=normalized,
            name=f"测试{normalized}",
            path=Path(vipdoc_path) / f"sh{normalized}.day",
        )

    def read_stock(self, ref: StockRef) -> pd.DataFrame:
        return self.frames[ref.code].copy(deep=True)


class BrokenAdapter:
    def list_stock_refs(self, vipdoc_path, universe, universe_file=None):
        return [StockRef(market="SH", code="600999", path=Path(vipdoc_path) / "sh600999.day")]

    def read_stock(self, ref: StockRef) -> pd.DataFrame:
        raise ValueError("读取失败")


class SparseAdapter:
    def __init__(self) -> None:
        sparse = stock_frame([10, 10])
        sparse["date"] = pd.to_datetime(["2024-01-01", "2024-01-03"])
        self.frames = {
            "600000": stock_frame([1, 1, 1]),
            "600001": sparse,
        }

    def list_stock_refs(self, vipdoc_path, universe, universe_file=None):
        return [
            StockRef(market="SH", code=code, path=Path(vipdoc_path) / f"sh{code}.day")
            for code in self.frames
        ]

    def read_stock(self, ref: StockRef) -> pd.DataFrame:
        return self.frames[ref.code].copy(deep=True)


class StaggeredAdapter:
    def __init__(self) -> None:
        first = stock_frame([20, 20, 20, 20])
        second = stock_frame([10, 8, 8])
        second["date"] = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
        self.frames = {"600000": first, "600001": second}

    def list_stock_refs(self, vipdoc_path, universe, universe_file=None):
        return [
            StockRef(market="SH", code=code, path=Path(vipdoc_path) / f"sh{code}.day")
            for code in self.frames
        ]

    def read_stock(self, ref: StockRef) -> pd.DataFrame:
        return self.frames[ref.code].copy(deep=True)


class StAdapter:
    """候选池里混一只戴帽股：``600002`` 是 ``*ST``，``600000`` 正常。"""

    def __init__(self) -> None:
        self.frames = {
            "600000": stock_frame([10, 11, 12, 13, 14, 15]),
            "600002": stock_frame([5, 6, 7, 8, 9, 10]),
        }
        self.names = {"600000": "浦发银行", "600002": "*ST测试"}

    def _refs(self, vipdoc_path: str, codes: list[str]) -> list[StockRef]:
        return [
            StockRef(
                market="SH",
                code=code,
                name=self.names[code],
                path=Path(vipdoc_path) / f"sh{code}.day",
            )
            for code in codes
        ]

    def list_stock_refs(self, vipdoc_path, universe, universe_file=None):
        return self._refs(vipdoc_path, list(self.frames))

    def stock_ref(self, vipdoc_path, market, code):
        normalized = code.strip()
        if normalized not in self.frames or market != "SH":
            raise ValueError(f"找不到已导入的行情: {market} {normalized}")
        return self._refs(vipdoc_path, [normalized])[0]

    def read_stock(self, ref: StockRef) -> pd.DataFrame:
        return self.frames[ref.code].copy(deep=True)


class SpyIndicatorCache:
    def __init__(self) -> None:
        self.calls: list[tuple[int, tuple[str, ...]]] = []

    def preload(self, frames, formula_ids):
        self.calls.append((len(frames), tuple(formula_ids)))
        return len(frames)


def config(**overrides: object) -> PortfolioBacktestConfig:
    values: dict[str, object] = {
        "vipdoc_path": "/tmp/vipdoc",
        "universe": "all",
        "selected_signals": ("custom.buy",),
        "combine_mode": "any",
        "minimum_matches": None,
        "ranking_value": "custom.rank",
        "rank_order": "desc",
        "max_positions": 2,
        "rebalance_frequency": "daily",
        "formula_text": "BUY:C>0; RANK:C;",
        "start_date": date(2024, 1, 1),
        "end_date": date(2024, 1, 6),
        "stop_loss_pct": 0.05,
        "initial_cash": 10_000.0,
        "commission": 0.0,
        "min_commission": 0.0,
        "stamp_tax": 0.0,
        "slippage": 0.0,
    }
    values.update(overrides)
    return PortfolioBacktestConfig(**values)


def test_indicators_use_adjusted_prices_while_fills_use_raw() -> None:
    """除权校正只作用于指标口径，成交价必须是真实价。

    构造一段行情：除权前后真实收盘价都低于 16，只有按除权校正后的价格才跨过
    阈值。于是

    * 指标若用 raw，买入条件永不成立，回测一笔都不做；
    * 指标用复权价，除权后第一天就产生信号；
    * 而成交价必须仍然是 raw 开盘价 —— 用复权价算股数的话，100 万能买到的手数
      完全是另一个数，涨跌停与整手也全都不再对应现实。
    """

    dates = pd.date_range("2024-01-02", periods=60, freq="B")
    # 除权前 41 天收在 11.x，除权后收在 12.6~13.2：真实价全程低于 16。
    close = np.where(
        np.arange(60) < 41, 11.0 + np.arange(60) % 9 * 0.1, 12.6 + np.arange(60) % 7 * 0.1
    )
    frame = pd.DataFrame(
        {
            "date": dates,
            "open": close - 0.07,
            "high": close + 0.03,
            "low": close - 0.11,
            "close": close,
            "volume": np.full(60, 100_000.0),
            "amount": close * 100_000.0,
        }
    )
    ex_index = 40
    actions = (
        CorporateAction(
            market="SH",
            code="600000",
            ex_date=dates[ex_index].date(),
            share_ratio=0.3,
            cash_per_share=0.2,
            factor_ratio=compute_factor_ratio(
                prev_close=float(close[ex_index - 1]),
                share_ratio=0.3,
                cash_per_share=0.2,
            ),
            action_type="mixed",
        ),
    )

    class ExDividendAdapter(FakeAdapter):
        def list_stock_refs(self, vipdoc_path, universe, universe_file=None):
            return [
                StockRef(
                    market="SH",
                    code="600000",
                    name="测试600000",
                    path=Path(vipdoc_path) / "sh600000.day",
                )
            ]

        def read_stock(self, ref: StockRef) -> pd.DataFrame:
            return frame.copy(deep=True)

        def read_corporate_actions(self, ref: StockRef):
            return actions

    settings = config(
        formula_text="BUY:C>16; RANK:C;",
        max_positions=1,
        initial_cash=100_000.0,
        start_date=dates[0].date(),
        end_date=dates[-1].date(),
        stop_loss_pct=None,
        # 出口只用来满足"至少一个卖出规则"的校验，10 倍止盈在这段行情里不会触发。
        take_profit_pct=10.0,
    )

    report = PortfolioBacktestService(adapter=ExDividendAdapter()).run(settings)

    assert [trade["direction"] for trade in report.trades] == ["BUY"]
    # 复权后除权次日（index 41）的收盘 = 12.6 x 1.317 > 16 产生信号，
    # next_open 成交落在再下一根 bar。
    assert report.trades[0]["date"] == dates[ex_index + 2].strftime("%Y-%m-%d")
    # 成交价是 raw 开盘价，不是复权价。
    assert report.trades[0]["price"] == pytest.approx(float(frame["open"].iloc[ex_index + 2]))


def test_positions_are_adjusted_on_ex_dividend_day() -> None:
    """跨除权日必须同步股数与成本价、现金分红要入账。

    不调整的话：raw 价按 (20-0.2)/1.3 掉到 15.23，股数与成本价却还停在除权前，
    市值当天凭空少掉 23%，而且现金分红永远到不了账——跨越 6~7 月分红高峰的多年
    回测，净值会系统性偏低。
    """

    share_ratio, cash_per_share = 0.3, 0.2
    before = 20.0
    after = (before - cash_per_share) / (1.0 + share_ratio)
    ex_index = 40
    dates = pd.date_range("2024-01-02", periods=60, freq="B")
    close = np.where(np.arange(60) < ex_index, before, after)
    frame = pd.DataFrame(
        {
            "date": dates,
            "open": close,
            "high": close + 0.03,
            "low": close - 0.03,
            "close": close,
            "volume": np.full(60, 100_000.0),
            "amount": close * 100_000.0,
        }
    )
    actions = (
        CorporateAction(
            market="SH",
            code="600000",
            ex_date=dates[ex_index].date(),
            share_ratio=share_ratio,
            cash_per_share=cash_per_share,
            factor_ratio=compute_factor_ratio(
                prev_close=before,
                share_ratio=share_ratio,
                cash_per_share=cash_per_share,
            ),
            action_type="mixed",
        ),
    )

    class ExDividendAdapter(FakeAdapter):
        def list_stock_refs(self, vipdoc_path, universe, universe_file=None):
            return [
                StockRef(
                    market="SH",
                    code="600000",
                    name="测试600000",
                    path=Path(vipdoc_path) / "sh600000.day",
                )
            ]

        def read_stock(self, ref: StockRef) -> pd.DataFrame:
            return frame.copy(deep=True)

        def read_corporate_actions(self, ref: StockRef):
            return actions

    settings = config(
        formula_text="BUY:C>19; RANK:C;",
        max_positions=1,
        initial_cash=100_000.0,
        start_date=dates[0].date(),
        end_date=dates[-1].date(),
        stop_loss_pct=None,
        # 只用来满足"至少一个卖出规则"的校验，这段行情里不会触发。
        take_profit_pct=10.0,
    )

    report = PortfolioBacktestService(adapter=ExDividendAdapter()).run(settings)

    # 全程只买不卖，方便只看除权日那一天的效应。
    assert [trade["direction"] for trade in report.trades] == ["BUY"]
    # 1) 总资产在除权日应当连续：市值 + 到账的现金红利 = 原有市值。
    totals = [float(row["total"]) for row in report.equity_curve]
    index = len(totals) - (len(dates) - ex_index)
    assert totals[index] == pytest.approx(totals[index - 1], rel=1e-6)
    # 2) 股数照 1+share_ratio 放大。
    state = report.current_state
    assert state is not None
    holdings = list(cast("list[dict[str, object]]", state["holdings"]))
    assert holdings
    assert float(cast("float", holdings[0]["size"])) == pytest.approx(
        float(cast("float", report.trades[0]["size"])) * (1.0 + share_ratio)
    )


def test_run_stock_refreshes_corporate_actions_between_top_level_runs() -> None:
    dates = pd.date_range("2024-01-01", periods=5, freq="D")
    frame = pd.DataFrame(
        {
            "date": dates,
            "open": [20.0, 20.0, 10.0, 10.0, 10.0],
            "high": [20.0] * 2 + [10.0] * 3,
            "low": [20.0] * 2 + [10.0] * 3,
            "close": [20.0] * 2 + [10.0] * 3,
            "volume": np.full(5, 100_000.0),
            "amount": [2_000_000.0] * 2 + [1_000_000.0] * 3,
        }
    )

    class MutableActionAdapter(FakeAdapter):
        actions: tuple[CorporateAction, ...] = ()

        def list_stock_refs(self, vipdoc_path, universe, universe_file=None):
            return [self.stock_ref(vipdoc_path, "SH", "600000")]

        def read_stock(self, ref: StockRef) -> pd.DataFrame:
            return frame.copy(deep=True)

        def read_corporate_actions(self, ref: StockRef):
            return self.actions

    adapter = MutableActionAdapter()
    service = PortfolioBacktestService(adapter=adapter)
    ref = adapter.stock_ref("/tmp/vipdoc", "SH", "600000")
    settings = config(
        formula_text="BUY:C>0; RANK:C;",
        max_positions=1,
        initial_cash=100_000.0,
        start_date=dates[0].date(),
        end_date=dates[-1].date(),
        stop_loss_pct=None,
        take_profit_pct=10.0,
    )

    before = service.run_stock(ref, settings)
    adapter.actions = (
        CorporateAction(
            market="SH",
            code="600000",
            ex_date=dates[2].date(),
            share_ratio=1.0,
            cash_per_share=0.0,
            factor_ratio=compute_factor_ratio(
                prev_close=20.0,
                share_ratio=1.0,
                cash_per_share=0.0,
            ),
            action_type="stock_dividend",
        ),
    )

    refreshed = service.run_stock(ref, settings)
    fresh_service = PortfolioBacktestService(adapter=adapter).run_stock(ref, settings)

    assert float(before.equity_curve[-1]["total"]) == pytest.approx(50_000.0)
    assert float(refreshed.equity_curve[-1]["total"]) == pytest.approx(100_000.0)
    assert before.data_fingerprint != refreshed.data_fingerprint
    assert refreshed.equity_curve == fresh_service.equity_curve


def _trade_directions(report: PortfolioBacktestReport) -> list[tuple[str, bool]]:
    return [
        (str(trade["direction"]), bool(trade.get("rejected", False))) for trade in report.trades
    ]


def test_run_stock_metrics_only_matches_full_report_performance() -> None:
    closes = [10.0, 12.0, 9.0, 11.0] * 20
    adapter = FakeAdapter()
    adapter.frames["600000"] = stock_frame(closes)
    service = PortfolioBacktestService(adapter=adapter)
    ref = adapter.stock_ref("/tmp/vipdoc", "SH", "600000")
    settings = config(
        max_positions=1,
        initial_cash=100_000.0,
        take_profit_pct=0.10,
        stop_loss_pct=0.05,
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 1) + timedelta(days=len(closes) - 1),
    )

    full = service.run_stock(ref, settings)
    fast_sim_stats(reset=True)
    try:
        metrics = service.run_stock(ref, settings, metrics_only=True)
    finally:
        stats = fast_sim_stats(reset=True)
    # 内核回退时对拍照样「通过」——必须显式断言 metrics_only 真的走了内核。
    if os.environ.get("SELECTOR_FAST_SIM", "1") != "0":
        assert stats.get("portfolio:hit") == 1, f"内核未命中（静默回退）：{stats}"

    assert metrics.data_fingerprint == full.data_fingerprint
    assert metrics.bars == full.bars
    assert metrics.start_date == full.start_date
    assert metrics.end_date == full.end_date
    # 内核与状态机的成交序列一致（方向 + 被拒标记是 _report_metrics 的读取面）。
    assert _trade_directions(metrics) == _trade_directions(full)
    assert len(_trade_directions(full)) > 4
    assert set(metrics.performance) >= {
        "total_return",
        "annual_return",
        "sharpe",
        "max_drawdown",
        "win_rate",
        "payoff_ratio",
        "profit_factor",
        "total_trades",
        "avg_holding_days",
        "turnover",
    }
    for key, value in metrics.performance.items():
        if value is None:
            assert full.performance[key] is None
        else:
            assert value == pytest.approx(cast("float", full.performance[key]))
    assert metrics.equity_curve == ()
    assert _report_metrics(metrics) == _report_metrics(full)


def test_run_stock_metrics_only_preflight_no_trade_matches_full_metrics() -> None:
    closes = [10.0 + index * 0.1 for index in range(30)]
    adapter = FakeAdapter()
    adapter.frames["600000"] = stock_frame(closes)
    service = PortfolioBacktestService(adapter=adapter)
    ref = adapter.stock_ref("/tmp/vipdoc", "SH", "600000")
    settings = config(
        formula_text="BUY:C<0; RANK:C;",
        max_positions=1,
        initial_cash=100_000.0,
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 30),
    )

    full = service.run_stock(ref, settings)
    metrics = service.run_stock(ref, settings, metrics_only=True)

    assert metrics.data_fingerprint == full.data_fingerprint
    assert _trade_directions(metrics) == _trade_directions(full) == []
    assert int(cast("float", metrics.performance["total_trades"])) == 0
    assert metrics.diagnostic == "预检无可交易候选"
    assert _report_metrics(metrics) == _report_metrics(full)


class _CountingReadAdapter(FakeAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.read_calls = 0

    def read_stock(self, ref: StockRef) -> pd.DataFrame:
        self.read_calls += 1
        return super().read_stock(ref)


def test_context_base_cache_hits_within_same_window() -> None:
    """同股同窗口的逐候选评估复用底座：行情只读一次，结果逐字段一致。"""

    adapter = _CountingReadAdapter()
    service = PortfolioBacktestService(adapter=adapter)
    ref = adapter.stock_ref("/tmp/vipdoc", "SH", "600000")
    settings = config(max_positions=1, initial_cash=100_000.0)

    first = service.run_stock(ref, settings)
    assert adapter.read_calls == 1
    second = service.run_stock(ref, settings)
    assert adapter.read_calls == 1
    assert second.data_fingerprint == first.data_fingerprint
    assert _trade_directions(second) == _trade_directions(first)
    assert second.performance["total_return"] == first.performance["total_return"]


def test_context_base_cache_keeps_both_research_windows() -> None:
    """规则对初筛在训练/验证两个窗口间交替，两个底座都应保持全热。"""

    adapter = _CountingReadAdapter()
    service = PortfolioBacktestService(adapter=adapter)
    ref = adapter.stock_ref("/tmp/vipdoc", "SH", "600000")
    train = config(max_positions=1, start_date=date(2024, 1, 1), end_date=date(2024, 1, 4))
    validation = config(max_positions=1, start_date=date(2024, 1, 5), end_date=date(2024, 1, 6))

    for _ in range(3):
        service.run_stock(ref, train)
        service.run_stock(ref, validation)

    assert adapter.read_calls == 2
    assert len(service._context_base_cache) == 2  # type: ignore[attr-defined]


def test_context_base_cache_evicts_oldest_entries() -> None:
    adapter = FakeAdapter()
    service = PortfolioBacktestService(adapter=adapter)
    ref = adapter.stock_ref("/tmp/vipdoc", "SH", "600000")

    for offset in range(5):
        service.run_stock(
            ref,
            config(
                max_positions=1,
                start_date=date(2024, 1, 1),
                end_date=date(2024, 1, 2 + offset),
            ),
        )

    assert len(service._context_base_cache) == 4  # type: ignore[attr-defined]


def test_context_base_cache_bypassed_for_explicit_frames() -> None:
    """批量路径自带窗口帧（含 warmup），语义与全历史帧不同，绝不能进同一缓存。"""

    adapter = FakeAdapter()
    service = PortfolioBacktestService(adapter=adapter)
    ref = adapter.stock_ref("/tmp/vipdoc", "SH", "600000")
    settings = config(max_positions=1)
    frame = adapter.read_stock(ref)
    parsed = parse_formula(settings.formula_text) if settings.formula_text else None

    context = service._build_context(ref, settings, parsed, frame)

    assert context is not None
    assert getattr(service, "_context_base_cache", None) is None


def test_prepared_context_action_preload_caches_missing_events_as_empty() -> None:
    class BatchActionAdapter:
        batch_calls = 0
        single_calls = 0

        def load_corporate_actions(self, keys):
            self.batch_calls += 1
            return {}

        def read_corporate_actions(self, ref: StockRef):
            self.single_calls += 1
            return ()

    adapter = BatchActionAdapter()
    service = PortfolioBacktestService(adapter=adapter)
    service._refresh_corporate_action_cache()
    service._preload_corporate_actions([("SH", "600000"), ("SH", "600001")])

    assert (
        service._corporate_actions(
            StockRef(market="SH", code="600000", path=Path("/tmp/600000.day"))
        )
        == ()
    )
    assert adapter.batch_calls == 1
    assert adapter.single_calls == 0


@pytest.mark.parametrize("prefix_length", [2, 3, 4])
def test_halted_stock_applies_due_actions_on_its_next_bar_and_resumes_idempotently(
    prefix_length: int,
) -> None:
    global_dates = np.asarray([20240101, 20240102, 20240103, 20240104, 20240105], dtype=np.int64)
    stock_dates = np.asarray([20240101, 20240102, 20240104, 20240105], dtype=np.int64)
    context = _StockSeries(
        market="SH",
        code="600000",
        instrument_type="stock",
        board="main",
        dates=stock_dates,
        opens=np.asarray([20.0, 20.0, 5.0, 5.0]),
        highs=np.asarray([20.0, 20.0, 5.0, 5.0]),
        lows=np.asarray([20.0, 20.0, 5.0, 5.0]),
        closes=np.asarray([20.0, 20.0, 5.0, 5.0]),
        atrs=np.full(4, np.nan),
        buy_mask=np.asarray([True, False, False, False]),
        sell_mask=np.asarray([False, False, False, False]),
        signals={},
        values={"custom.rank": np.asarray([1.0, 1.0, 1.0, 1.0])},
        date_to_index={int(value): index for index, value in enumerate(stock_dates)},
        data_fingerprint="halted-action-context",
    )
    companion_dates = global_dates
    companion = _StockSeries(
        market="SH",
        code="600001",
        instrument_type="stock",
        board="main",
        dates=companion_dates,
        opens=np.full(5, 10.0),
        highs=np.full(5, 10.0),
        lows=np.full(5, 10.0),
        closes=np.full(5, 10.0),
        atrs=np.full(5, np.nan),
        buy_mask=np.zeros(5, dtype=bool),
        sell_mask=np.zeros(5, dtype=bool),
        signals={},
        values={"custom.rank": np.ones(5)},
        date_to_index={int(value): index for index, value in enumerate(companion_dates)},
        data_fingerprint="companion-context",
    )
    actions = (
        CorporateAction(
            market="SH",
            code="600000",
            ex_date=date(2024, 1, 3),
            share_ratio=1.0,
            cash_per_share=0.0,
            factor_ratio=2.0,
            action_type="stock_dividend",
        ),
        CorporateAction(
            market="SH",
            code="600000",
            ex_date=date(2024, 1, 4),
            share_ratio=1.0,
            cash_per_share=0.0,
            factor_ratio=2.0,
            action_type="stock_dividend",
        ),
    )

    class ActionReader:
        def read_corporate_actions(self, ref: StockRef):
            return actions if ref.code == "600000" else ()

    service = PortfolioBacktestService(adapter=ActionReader())
    settings = config(
        formula_text=None,
        selected_signals=(),
        buy_rule=RuleGroup(
            conditions=(
                ScreenCondition(left_value="price.close", operator="gt", right_constant=0.0),
            ),
        ),
        sell_rule=RuleGroup(
            conditions=(
                ScreenCondition(left_value="price.close", operator="lt", right_constant=0.0),
            ),
        ),
        stop_loss_pct=None,
        take_profit_pct=10.0,
        initial_cash=100_000.0,
        max_positions=1,
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 5),
    )
    full = service._simulate(
        [context, companion],
        global_dates.tolist(),
        settings,
        data_fingerprint="halted-action-context",
    )
    prefix = service._simulate(
        [context, companion],
        global_dates[:prefix_length].tolist(),
        settings,
        data_fingerprint="halted-action-context",
    )
    prefix_checkpoint = prefix["checkpoint"]
    assert isinstance(prefix_checkpoint, SimulationCheckpoint)
    if prefix_length == 3:
        assert prefix["equity"][-1]["total"] == pytest.approx(100_000.0)
        assert not prefix_checkpoint.positions["SH600000"].applied_corporate_actions
    restored = SimulationCheckpoint.from_dict(prefix_checkpoint.to_dict())
    resumed = service._simulate(
        [context, companion],
        global_dates.tolist(),
        settings,
        checkpoint=restored,
        data_fingerprint="halted-action-context",
    )

    equity = full["equity"]
    assert [row["total"] for row in equity] == pytest.approx([100_000.0] * len(global_dates))
    assert resumed["equity"] == full["equity"]
    assert resumed["trades"] == full["trades"]
    assert resumed["states"] == full["states"]
    final_position = full["checkpoint"].positions["SH600000"]
    assert final_position.size == pytest.approx(20_000.0)
    assert len(final_position.applied_corporate_actions) == 2


@pytest.mark.parametrize(
    ("max_hold_days", "expected_sell_date"),
    [(1, "2024-01-07"), (2, "2024-01-09")],
)
def test_max_hold_days_counts_only_the_held_stock_bars(
    max_hold_days: int,
    expected_sell_date: str,
) -> None:
    global_dates = np.asarray(
        [
            20240101,
            20240102,
            20240103,
            20240104,
            20240105,
            20240106,
            20240107,
            20240108,
            20240109,
            20240110,
            20240111,
            20240112,
        ],
        dtype=np.int64,
    )
    target_dates = np.asarray([20240103, 20240104, 20240107, 20240109, 20240112], dtype=np.int64)

    def context(
        code: str,
        dates: np.ndarray,
        prices: np.ndarray,
        buy_mask: np.ndarray,
    ) -> _StockSeries:
        return _StockSeries(
            market="SH",
            code=code,
            instrument_type="stock",
            board="main",
            dates=dates,
            opens=prices.copy(),
            highs=prices.copy(),
            lows=prices.copy(),
            closes=prices.copy(),
            atrs=np.full(len(dates), np.nan),
            buy_mask=buy_mask,
            sell_mask=np.zeros(len(dates), dtype=bool),
            signals={},
            values={"price.close": prices},
            date_to_index={int(value): index for index, value in enumerate(dates)},
            data_fingerprint=f"{code}-bars",
        )

    target = context(
        "600000",
        target_dates,
        np.full(len(target_dates), 20.0),
        np.asarray([True, False, False, False, False]),
    )
    companion = context(
        "600001",
        global_dates,
        np.full(len(global_dates), 10.0),
        np.zeros(len(global_dates), dtype=bool),
    )
    settings = config(
        formula_text=None,
        selected_signals=(),
        buy_rule=RuleGroup(
            conditions=(
                ScreenCondition(left_value="price.close", operator="gt", right_constant=0.0),
            ),
        ),
        sell_rule=RuleGroup(
            conditions=(
                ScreenCondition(left_value="price.close", operator="lt", right_constant=0.0),
            ),
        ),
        initial_cash=100_000.0,
        commission=0.0,
        min_commission=0.0,
        stamp_tax=0.0,
        slippage=0.0,
        max_positions=1,
        max_hold_days=max_hold_days,
        ranking_value="price.close",
        stop_loss_pct=None,
        take_profit_pct=10.0,
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 12),
    )

    report = PortfolioBacktestService().run_prepared_contexts(settings, [target, companion])
    trades = list(report.trades)

    assert [trade["direction"] for trade in trades] == ["BUY", "SELL"]
    assert trades[0]["date"] == "2024-01-04"
    assert trades[1]["date"] == expected_sell_date
    assert trades[1]["reason"] == "持有到期"


def test_corporate_actions_before_entry_and_simulation_start_are_ignored() -> None:
    dates = np.asarray([20240102, 20240103, 20240104], dtype=np.int64)
    context = _StockSeries(
        market="SH",
        code="600000",
        instrument_type="stock",
        board="main",
        dates=dates,
        opens=np.full(3, 10.0),
        highs=np.full(3, 10.0),
        lows=np.full(3, 10.0),
        closes=np.full(3, 10.0),
        atrs=np.full(3, np.nan),
        buy_mask=np.asarray([True, False, False]),
        sell_mask=np.zeros(3, dtype=bool),
        signals={},
        values={"price.close": np.full(3, 10.0)},
        date_to_index={int(value): index for index, value in enumerate(dates)},
        data_fingerprint="pre-entry-action-context",
    )
    action = CorporateAction(
        market="SH",
        code="600000",
        ex_date=date(2024, 1, 1),
        share_ratio=1.0,
        cash_per_share=0.0,
        factor_ratio=2.0,
        action_type="stock_dividend",
    )

    class ActionReader:
        def read_corporate_actions(self, ref: StockRef):
            return (action,)

    service = PortfolioBacktestService(adapter=ActionReader())
    settings = config(
        formula_text=None,
        selected_signals=(),
        buy_rule=RuleGroup(
            conditions=(
                ScreenCondition(left_value="price.close", operator="gt", right_constant=0.0),
            ),
        ),
        sell_rule=RuleGroup(
            conditions=(
                ScreenCondition(left_value="price.close", operator="lt", right_constant=0.0),
            ),
        ),
        initial_cash=100_000.0,
        max_positions=1,
        ranking_value="price.close",
        stop_loss_pct=None,
        take_profit_pct=10.0,
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 4),
    )

    result = service._simulate([context], dates.tolist(), settings)

    assert [row["total"] for row in result["equity"]] == pytest.approx([100_000.0] * 3)
    assert result["checkpoint"].positions["SH600000"].entry_date == 20240103
    assert not result["checkpoint"].positions["SH600000"].applied_corporate_actions


def test_indicators_stay_below_threshold_without_corporate_actions() -> None:
    """对照：同一段行情没有事件时，指标就是 raw 口径，一笔都不该做。"""

    dates = pd.date_range("2024-01-02", periods=60, freq="B")
    close = np.where(
        np.arange(60) < 41, 11.0 + np.arange(60) % 9 * 0.1, 12.6 + np.arange(60) % 7 * 0.1
    )
    frame = pd.DataFrame(
        {
            "date": dates,
            "open": close - 0.07,
            "high": close + 0.03,
            "low": close - 0.11,
            "close": close,
            "volume": np.full(60, 100_000.0),
            "amount": close * 100_000.0,
        }
    )

    class PlainAdapter(FakeAdapter):
        def list_stock_refs(self, vipdoc_path, universe, universe_file=None):
            return [
                StockRef(
                    market="SH",
                    code="600000",
                    name="测试600000",
                    path=Path(vipdoc_path) / "sh600000.day",
                )
            ]

        def read_stock(self, ref: StockRef) -> pd.DataFrame:
            return frame.copy(deep=True)

    report = PortfolioBacktestService(adapter=PlainAdapter()).run(
        config(
            formula_text="BUY:C>16; RANK:C;",
            max_positions=1,
            initial_cash=100_000.0,
            start_date=dates[0].date(),
            end_date=dates[-1].date(),
            stop_loss_pct=None,
            take_profit_pct=10.0,
        )
    )

    assert not report.trades


def test_portfolio_fills_a_vacated_slot_by_rank_after_stop_loss() -> None:
    service = PortfolioBacktestService(adapter=FakeAdapter())

    report = service.run(config())

    assert report.total_candidates == 3
    assert report.processed == 3
    assert [trade["direction"] for trade in report.trades] == [
        "BUY",
        "BUY",
        "SELL",
        "BUY",
        "SELL",
        "BUY",
    ]
    assert [(trade["code"], trade["date"]) for trade in report.trades] == [
        ("600001", "2024-01-02"),
        ("600000", "2024-01-02"),
        ("600001", "2024-01-03"),
        ("600001", "2024-01-04"),
        ("600001", "2024-01-05"),
        ("600001", "2024-01-06"),
    ]
    assert report.trades[0]["name"] == "测试600001"
    assert {holding["code"] for holding in report.states[-1]["holdings"]} == {"600000", "600001"}
    assert {holding["name"] for holding in report.states[-1]["holdings"]} == {
        "测试600000",
        "测试600001",
    }
    assert report.states[-1]["positions_count"] == 2
    assert report.performance["end_value"] == 11_100.0
    json.dumps(report.to_dict(), ensure_ascii=False)


def test_daily_candidate_scan_is_independent_of_rebalance_day_and_reports_cross_values() -> None:
    report = PortfolioBacktestService(adapter=FakeAdapter()).run(
        config(
            formula_text="BUY:C>0; RANK:C;",
            selected_signals=(),
            ranking_value="custom.rank",
            rebalance_frequency="monthly",
            buy_rule=RuleGroup(
                signals=("custom.buy",),
                conditions=(
                    ScreenCondition(
                        left_value="custom.rank",
                        operator="cross_up",
                        right_constant=14.0,
                    ),
                ),
            ),
            sell_rule=RuleGroup(
                conditions=(
                    ScreenCondition(
                        left_value="custom.rank",
                        operator="cross_down",
                        right_constant=0.0,
                    ),
                ),
            ),
            stop_loss_pct=None,
        )
    )

    scan = report.today_candidates

    assert scan["as_of"] == "2024-01-06"
    assert scan["total"] == 1
    assert [candidate["code"] for candidate in scan["rows"]] == ["600000"]
    candidate = scan["rows"][0]
    assert candidate["rank"] == 1
    assert candidate["score"] == 15.0
    assert candidate["buy_signal"]["matched"] is True
    condition = candidate["buy_conditions"][0]
    assert condition["operator"] == "cross_up"
    assert condition["previous_left_value"] == 14.0
    assert condition["current_left_value"] == 15.0
    assert condition["matched"] is True
    assert report.next_plan["rebalance_due"] is False
    assert report.next_plan["buys"] == []


def test_candidate_scan_limits_rows_but_keeps_the_full_match_count() -> None:
    dates = np.asarray([20240101, 20240102], dtype=np.int64)
    contexts = [
        _StockSeries(
            market="SH",
            code=f"{index:06d}",
            instrument_type="stock",
            board="main",
            dates=dates,
            opens=np.asarray([10.0, 10.0]),
            highs=np.asarray([10.0, 10.0]),
            lows=np.asarray([10.0, 10.0]),
            closes=np.asarray([10.0, 10.0]),
            atrs=np.asarray([np.nan, np.nan]),
            buy_mask=np.asarray([True, True]),
            sell_mask=np.asarray([False, False]),
            signals={"indicator_one.main_force_entry": np.asarray([True, True])},
            values={"indicator_one.var3": np.asarray([float(index), float(index)])},
            date_to_index={20240101: 0, 20240102: 1},
            data_fingerprint=f"fingerprint-{index}",
        )
        for index in range(25)
    ]

    report = PortfolioBacktestService().run_prepared_contexts(
        config(
            formula_text=None,
            selected_signals=("indicator_one.main_force_entry",),
            sell_rule=RuleGroup(
                conditions=(
                    ScreenCondition(
                        left_value="indicator_one.var3", operator="gt", right_constant=100.0
                    ),
                )
            ),
            ranking_value="indicator_one.var3",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 2),
            initial_cash=100_000.0,
        ),
        contexts,
    )

    scan = report.today_candidates
    assert scan["total"] == 25
    assert len(scan["rows"]) == 20
    assert [candidate["rank"] for candidate in scan["rows"]] == list(range(1, 21))
    assert scan["rows"][0]["code"] == "000024"
    assert scan["rows"][-1]["code"] == "000005"


def test_current_state_shows_sell_indicator_values_for_a_held_position() -> None:
    report = PortfolioBacktestService(adapter=FakeAdapter()).run(
        config(
            universe="codes",
            codes=("SH600001",),
            max_positions=1,
            sell_rule=RuleGroup(
                conditions=(
                    ScreenCondition(
                        left_value="custom.rank",
                        operator="eq",
                        right_constant=15.0,
                    ),
                ),
            ),
            stop_loss_pct=None,
        )
    )

    state = report.current_state
    assert state is not None
    assert state["positions_count"] == 1
    holding = state["holdings"][0]
    assert holding["code"] == "600001"
    assert holding["latest_price"] == 15.0
    assert holding["entry_date"] == "2024-01-02"
    assert holding["market_value"] == holding["size"] * 15.0
    assert holding["sell_signal"]["matched"] is True
    condition = holding["sell_signal"]["conditions"][0]
    assert condition["current_left_value"] == 15.0
    assert condition["threshold"] == 15.0


def test_portfolio_uses_all_local_dates_when_date_window_is_blank() -> None:
    report = PortfolioBacktestService(adapter=FakeAdapter()).run(
        config(start_date=None, end_date=None)
    )

    assert report.bars == 6
    assert report.start_date == "2024-01-01"
    assert report.end_date == "2024-01-06"


def test_prepared_backtest_skips_simulation_when_no_candidate_can_be_bought(monkeypatch) -> None:
    context = _StockSeries(
        market="SH",
        code="600000",
        instrument_type="stock",
        board="main",
        dates=np.asarray([20240101, 20240102, 20240103], dtype=np.int64),
        opens=np.asarray([10.0, 10.0, 10.0]),
        highs=np.asarray([10.0, 10.0, 10.0]),
        lows=np.asarray([10.0, 10.0, 10.0]),
        closes=np.asarray([10.0, 10.0, 10.0]),
        atrs=np.asarray([np.nan, np.nan, np.nan]),
        buy_mask=np.asarray([False, False, False]),
        sell_mask=np.asarray([False, False, False]),
        signals={},
        values={"technical.ma5": np.asarray([10.0, 10.0, 10.0])},
        date_to_index={20240101: 0, 20240102: 1, 20240103: 2},
        data_fingerprint="fingerprint",
    )
    service = PortfolioBacktestService()
    monkeypatch.setattr(
        service,
        "_simulate",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("simulation should skip")),
    )

    report = service.run_prepared_contexts(
        config(
            formula_text=None,
            selected_signals=(),
            buy_rule=RuleGroup(
                conditions=(
                    ScreenCondition(
                        left_value="technical.ma5",
                        operator="gt",
                        right_constant=100.0,
                    ),
                ),
            ),
            ranking_value="technical.ma5",
            sell_rule=RuleGroup(
                conditions=(
                    ScreenCondition(
                        left_value="technical.ma5",
                        operator="lt",
                        right_constant=1.0,
                    ),
                ),
            ),
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 3),
        ),
        [context],
        include_details=True,
    )

    assert report.performance["total_return"] == 0.0
    assert report.trades == ()
    assert report.diagnostic == "预检无可交易候选"
    assert report.current_state == {
        "as_of": "2024-01-03",
        "available": True,
        "equity": 10_000.0,
        "total_asset": 10_000.0,
        "cash": 10_000.0,
        "position_value": 0.0,
        "positions_count": 0,
        "holdings": [],
    }
    assert report.today_candidates["total"] == 0
    assert report.today_candidates["rows"] == []


def test_simulation_checkpoint_continuation_matches_full_run() -> None:
    dates = np.asarray([20240101, 20240102, 20240103, 20240104, 20240105, 20240106], dtype=np.int64)
    context = _StockSeries(
        market="SH",
        code="600000",
        instrument_type="stock",
        board="main",
        dates=dates,
        opens=np.asarray([10.0, 10.0, 10.5, 11.0, 11.5, 12.0]),
        highs=np.asarray([10.0, 10.2, 10.7, 11.2, 11.7, 12.2]),
        lows=np.asarray([9.8, 9.9, 10.2, 10.7, 11.2, 11.7]),
        closes=np.asarray([10.0, 10.5, 11.0, 11.5, 12.0, 12.5]),
        atrs=np.full(6, np.nan),
        buy_mask=np.asarray([True, True, True, True, True, True]),
        sell_mask=np.asarray([False, False, False, False, False, False]),
        signals={},
        values={"technical.ma5": np.asarray([10.0, 10.5, 11.0, 11.5, 12.0, 12.5])},
        date_to_index={int(value): index for index, value in enumerate(dates)},
        data_fingerprint="fingerprint",
    )
    service = PortfolioBacktestService()
    simulation_config = config(
        formula_text=None,
        selected_signals=(),
        buy_rule=RuleGroup(
            conditions=(
                ScreenCondition(
                    left_value="technical.ma5",
                    operator="gt",
                    right_constant=1.0,
                ),
            ),
        ),
        ranking_value="technical.ma5",
        sell_rule=RuleGroup(
            conditions=(
                ScreenCondition(
                    left_value="technical.ma5",
                    operator="lt",
                    right_constant=0.0,
                ),
            ),
        ),
        stop_loss_pct=None,
        trailing_stop_pct=0.05,
        trailing_stop_cooldown_days=2,
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 6),
    )

    full = service._simulate([context], dates.tolist(), simulation_config)
    prefix = service._simulate([context], dates[:3].tolist(), simulation_config)
    checkpoint = prefix["checkpoint"]
    assert isinstance(checkpoint, SimulationCheckpoint)
    restored = SimulationCheckpoint.from_dict(checkpoint.to_dict())
    resumed = service._simulate([context], dates.tolist(), simulation_config, checkpoint=restored)

    assert resumed["equity"] == full["equity"]
    assert resumed["trades"] == full["trades"]
    assert resumed["states"] == full["states"]
    assert resumed["ranking_events"] == full["ranking_events"]
    assert resumed["cash"] == full["cash"]
    json.dumps(restored.to_dict(), ensure_ascii=False)


def test_portfolio_supports_indicator_threshold_exit() -> None:
    service = PortfolioBacktestService(adapter=FakeAdapter())

    report = service.run(
        config(
            formula_text="BUY:C>0; RANK:C; LEVEL:C;",
            ranking_value="custom.rank",
            sell_value="custom.level",
            sell_value_operator="gte",
            sell_value_threshold=14.0,
            stop_loss_pct=None,
        )
    )

    assert any(
        trade["direction"] == "SELL" and trade["code"] == "600000" for trade in report.trades
    )


def test_portfolio_accepts_signed_indicator_thresholds() -> None:
    configured = config(
        formula_text="BUY:C>0; RANK:C; LEVEL:C-20;",
        ranking_value="custom.rank",
        sell_value="custom.level",
        sell_value_operator="lte",
        sell_value_threshold=-10.0,
        stop_loss_pct=None,
    )

    assert configured.sell_value_threshold == -10.0


def test_portfolio_supports_indicator_comparison_exit() -> None:
    service = PortfolioBacktestService(adapter=FakeAdapter())

    report = service.run(
        config(
            formula_text="BUY:C>0; RANK:C; FAST:C; SLOW:MA(C,2);",
            ranking_value="custom.rank",
            compare_left_value="custom.fast",
            compare_operator="lt",
            compare_right_value="custom.slow",
            stop_loss_pct=None,
        )
    )

    assert any(trade["direction"] == "SELL" for trade in report.trades)


def test_empty_portfolio_report_counts_failed_stocks_as_skipped() -> None:
    report = PortfolioBacktestService(adapter=BrokenAdapter()).run(config())

    assert report.total_candidates == 1
    assert report.processed == 0
    assert report.skipped == 1
    assert report.errors == 1


def test_portfolio_does_not_carry_an_unfilled_buy_into_a_later_bar() -> None:
    report = PortfolioBacktestService(adapter=SparseAdapter()).run(
        config(
            rebalance_frequency="monthly",
            max_positions=1,
            stop_loss_pct=None,
            take_profit_pct=0.5,
        )
    )

    assert report.trades == ()


def test_portfolio_holding_days_are_paired_per_stock() -> None:
    report = PortfolioBacktestService(adapter=StaggeredAdapter()).run(
        config(
            formula_text="BUY:C>0; RANK:C; LEVEL:C;",
            ranking_value="custom.rank",
            max_positions=2,
            stop_loss_pct=None,
            sell_value="custom.level",
            sell_value_operator="lte",
            sell_value_threshold=8.0,
        )
    )

    assert [(trade["code"], trade["direction"]) for trade in report.trades] == [
        ("600000", "BUY"),
        ("600001", "BUY"),
        ("600001", "SELL"),
    ]
    assert report.performance["avg_holding_days"] == 1.0


def test_portfolio_rejects_unknown_sell_comparison_operators() -> None:
    with pytest.raises(ValueError, match="指标阈值比较方式"):
        config(
            sell_value="custom.rank",
            sell_value_operator="unknown",
            sell_value_threshold=1.0,
        )

    with pytest.raises(ValueError, match="指标比较方式"):
        config(
            compare_left_value="custom.rank",
            compare_operator="unknown",
            compare_right_value="custom.rank",
        )


def test_portfolio_fitness_weight_uses_only_history_before_the_signal_date() -> None:
    report = PortfolioBacktestService(adapter=FakeAdapter()).run(
        config(
            fitness_rank_weight=0.5,
            fitness_min_trades=1,
            fitness_max_drawdown=1.0,
        )
    )

    first_event = report.ranking_events[0]
    assert first_event["date"] == "2024-01-01"
    assert first_event["candidates"]
    assert all(candidate["fitness_trades"] == 0 for candidate in first_event["candidates"])
    assert all("rank_score" in candidate for candidate in first_event["candidates"])


def _decision(score: float, trades: int) -> RollingFitnessDecision:
    return RollingFitnessDecision(
        score=score,
        trades=trades,
        expectancy=None,
        profit_factor=None,
        total_return=None,
        max_drawdown=None,
        note="",
    )


def test_zero_fitness_weight_leaves_ranking_untouched_and_unreported() -> None:
    report = PortfolioBacktestService(adapter=FakeAdapter()).run(config())

    assert report.fitness_rank_weight == 0.0
    for event in report.ranking_events:
        assert all("rank_score" not in candidate for candidate in event["candidates"])
        assert all("fitness_score" not in candidate for candidate in event["candidates"])


def test_fitness_weight_can_outweigh_the_plain_indicator_ranking() -> None:
    strong_indicator = (30.0, SimpleNamespace(symbol="SH600000"), _decision(0.0, 5))
    strong_fitness = (10.0, SimpleNamespace(symbol="SH600001"), _decision(100.0, 5))
    entries = [strong_indicator, strong_fitness]

    plain = PortfolioSimulationMixin._weighted_scores(
        entries, config(fitness_rank_weight=0.0, fitness_min_trades=5)
    )
    blended = PortfolioSimulationMixin._weighted_scores(
        entries, config(fitness_rank_weight=0.75, fitness_min_trades=5)
    )

    assert plain[0] > plain[1]
    assert blended[1] > blended[0]


def test_fitness_weight_fills_thin_history_with_the_pool_mean() -> None:
    fresh = (30.0, SimpleNamespace(symbol="SH600000"), _decision(0.0, 0))
    seasoned = (10.0, SimpleNamespace(symbol="SH600001"), _decision(100.0, 5))

    scores = PortfolioSimulationMixin._weighted_scores(
        [fresh, seasoned], config(fitness_rank_weight=0.5, fitness_min_trades=5)
    )

    assert scores[0] > scores[1]


def test_portfolio_uses_composable_buy_and_sell_rule_groups() -> None:
    service = PortfolioBacktestService(adapter=FakeAdapter())
    report = service.run(
        config(
            selected_signals=(),
            combine_mode="any",
            minimum_matches=None,
            buy_rule=RuleGroup(
                signals=("custom.buy",),
                conditions=(
                    ScreenCondition(
                        left_value="custom.rank",
                        operator="gte",
                        right_constant=10.0,
                    ),
                ),
                combine_mode="all",
            ),
            sell_rule=RuleGroup(
                conditions=(
                    ScreenCondition(
                        left_value="custom.rank",
                        operator="gte",
                        right_constant=14.0,
                    ),
                )
            ),
            formula_text="BUY:C>0; RANK:C;",
            stop_loss_pct=None,
            take_profit_pct=None,
        )
    )

    assert any(trade["direction"] == "SELL" for trade in report.trades)


def test_portfolio_resolves_per_symbol_rules_risk_controls_and_priority() -> None:
    buy_up = RuleGroup(
        conditions=(ScreenCondition(left_value="custom.rank", operator="gt", right_constant=12),)
    )
    sell_up = RuleGroup(
        conditions=(ScreenCondition(left_value="custom.rank", operator="gt", right_constant=14),)
    )
    buy_down = RuleGroup(
        conditions=(ScreenCondition(left_value="custom.rank", operator="gte", right_constant=19),)
    )
    sell_down = RuleGroup(
        conditions=(ScreenCondition(left_value="custom.rank", operator="lt", right_constant=17),)
    )

    report = PortfolioBacktestService(adapter=FakeAdapter()).run(
        config(
            selected_signals=(),
            buy_rule=None,
            sell_rule=None,
            stop_loss_pct=None,
            max_positions=2,
            symbol_strategies={
                "SH600000": {"buy_rule": buy_up, "sell_rule": sell_up},
                "SH600001": {
                    "buy_rule": buy_down,
                    "sell_rule": sell_down,
                    "stop_loss_pct": 0.05,
                },
            },
            symbol_priority_scores={"SH600000": 2.0, "SH600001": 1.0},
        )
    )

    bought = {str(trade["code"]) for trade in report.trades if trade["direction"] == "BUY"}
    assert bought == {"600000", "600001"}
    assert any(
        trade.get("risk_trigger") == "stop_loss" and trade["code"] == "600001"
        for trade in report.trades
    )


def test_available_slot_uses_validation_priority_instead_of_cross_sectional_indicator() -> None:
    dates = np.asarray([20240101], dtype=np.int64)
    contexts = {
        f"SH{code}": _StockSeries(
            market="SH",
            code=code,
            instrument_type="stock",
            board="main",
            dates=dates,
            opens=np.asarray([10.0]),
            highs=np.asarray([10.5]),
            lows=np.asarray([9.5]),
            closes=np.asarray([10.0]),
            atrs=np.asarray([np.nan]),
            buy_mask=np.asarray([True]),
            sell_mask=np.asarray([False]),
            signals={},
            values={"custom.rank": np.asarray([cross_sectional_rank])},
            date_to_index={20240101: 0},
            data_fingerprint=code,
            priority_score=priority,
        )
        for code, cross_sectional_rank, priority in (
            ("600000", 1.0, 2.0),
            ("600001", 99.0, 1.0),
        )
    }

    pending, _event = PortfolioBacktestService(adapter=FakeAdapter())._select_candidates(
        20240101,
        positions={},
        pending_sells={},
        by_symbol=contexts,
        last_prices={},
        cash=10_000.0,
        total_value=10_000.0,
        config=config(max_positions=1),
        fitness_filter=None,
        cooldown_until={},
        date_index=0,
        include_event=False,
    )

    assert [item.symbol for item in pending] == ["SH600000"]


def test_portfolio_preloads_indicator_cache_in_bounded_chunks() -> None:
    cache = SpyIndicatorCache()
    service = PortfolioBacktestService(adapter=FakeAdapter(), indicator_cache=cache)
    config_with_rules = PortfolioBacktestConfig(
        vipdoc_path="/tmp/vipdoc",
        universe="all",
        selected_signals=(),
        combine_mode="all",
        minimum_matches=None,
        ranking_value="indicator_three.varo7",
        buy_rule=RuleGroup(
            conditions=(
                ScreenCondition(
                    left_value="indicator_three.var1",
                    operator="gt",
                    right_constant=8,
                ),
            )
        ),
        sell_rule=RuleGroup(
            conditions=(
                ScreenCondition(
                    left_value="indicator_three.var1",
                    operator="gt",
                    right_constant=90,
                ),
            )
        ),
        stop_loss_pct=None,
        take_profit_pct=None,
    )
    frames = {("SH", f"{600000 + index:06d}"): stock_frame([10, 11, 12]) for index in range(33)}

    service._preload_indicator_cache(frames, config_with_rules, None)

    assert [size for size, _ in cache.calls] == [32, 1]
    assert all(formulas == ("indicator_three",) for _, formulas in cache.calls)


def test_sorted_date_index_supports_mapping_lookup_without_a_python_date_dict() -> None:
    index = SortedDateIndex(np.asarray([20240101, 20240103, 20240105], dtype=np.int64))

    assert index.get(20240101) == 0
    assert index.get(20240105) == 2
    assert index.get(20240102) is None
    assert len(index) == 3


def test_codes_universe_limits_the_backtest_to_the_requested_symbols() -> None:
    report = PortfolioBacktestService(adapter=FakeAdapter()).run(
        config(universe="codes", codes=("SH:600000", "600002"), max_positions=1)
    )

    assert report.total_candidates == 2
    assert {trade["code"] for trade in report.trades} <= {"600000", "600002"}


def test_codes_universe_deduplicates_and_accepts_bare_codes() -> None:
    report = PortfolioBacktestService(adapter=FakeAdapter()).run(
        config(universe="codes", codes=("600000", "SH600000", "600000"), max_positions=1)
    )

    assert report.total_candidates == 1


def test_codes_universe_requires_at_least_one_code() -> None:
    with pytest.raises(ValueError, match="指定标的范围必须提供至少一个代码"):
        config(universe="codes", codes=())


def test_codes_are_rejected_outside_the_codes_universe() -> None:
    with pytest.raises(ValueError, match="只有指定标的范围才需要填写代码列表"):
        config(universe="all", codes=("600000",))


def test_codes_universe_reports_codes_that_never_reached_the_simulation() -> None:
    """点名指定的标的一个都不能悄悄丢掉。

    只有「一个都没找到」才报错的话，填 10 只实际跑 8 只时回测照样出报告，
    用户从结果里看不出样本少了。
    """

    report = PortfolioBacktestService(adapter=FakeAdapter()).run(
        config(universe="codes", codes=("600000", "600999"), max_positions=1)
    )

    assert report.total_candidates == 1
    assert report.missing_codes == ("SH600999",)
    assert report.filtered_codes == ()


def test_codes_universe_reports_codes_dropped_by_the_scope() -> None:
    report = PortfolioBacktestService(adapter=FakeAdapter()).run(
        config(universe="codes", codes=("600000",), boards=("star",), max_positions=1)
    )

    assert report.filtered_codes == ("SH600000",)
    assert report.missing_codes == ()


def test_exclude_st_leaves_the_pool_untouched_when_disabled() -> None:
    """开关默认关闭，老配置的回测结果不能悄悄变样。"""

    report = PortfolioBacktestService(adapter=StAdapter()).run(config(max_positions=2))

    assert report.total_candidates == 2
    assert report.excluded_st_count == 0
    assert report.excluded_st_codes == ()


def test_exclude_st_drops_risk_warning_symbols_from_the_pool() -> None:
    report = PortfolioBacktestService(adapter=StAdapter()).run(
        config(max_positions=2, exclude_st=True)
    )

    assert report.total_candidates == 1
    assert report.excluded_st_count == 1
    assert report.excluded_st_codes == ("SH600002",)
    assert {trade["code"] for trade in report.trades} == {"600000"}


def test_exclude_st_reports_the_dropped_codes_in_the_codes_universe() -> None:
    """点名指定的标的被剔除时必须说出来，不能只在候选数里少一只。"""

    report = PortfolioBacktestService(adapter=StAdapter()).run(
        config(universe="codes", codes=("600000", "600002"), max_positions=1, exclude_st=True)
    )

    assert report.total_candidates == 1
    assert report.excluded_st_codes == ("SH600002",)
    assert report.filtered_codes == ()


def test_unknown_codes_raise_a_readable_error() -> None:
    with pytest.raises(ValueError, match="找不到已导入的行情"):
        PortfolioBacktestService(adapter=FakeAdapter()).run(
            config(universe="codes", codes=("600999",))
        )


def test_fixed_position_mode_buys_a_constant_share_count() -> None:
    service = PortfolioBacktestService(adapter=FakeAdapter())

    fixed = service.run(
        config(
            universe="codes",
            codes=("600000",),
            max_positions=1,
            position_mode="fixed",
            fixed_size=200,
        )
    )
    scaled = service.run(
        config(universe="codes", codes=("600000",), max_positions=1, position_mode="full")
    )

    # 次日开盘价 11 元：等额模式按 10000 元预算买到 900 股（整手向下取整），固定模式仍只买 200 股。
    assert fixed.trades[0]["size"] == 200
    assert scaled.trades[0]["size"] == 900


def test_fixed_position_mode_requires_a_positive_size() -> None:
    with pytest.raises(ValueError, match="固定股数模式必须设置大于 0 的买入股数"):
        config(position_mode="fixed", fixed_size=None)


def _planning_report(**overrides: object):
    return PortfolioBacktestService(adapter=FakeAdapter()).run(
        config(
            formula_text="BUY:C>0; RANK:C; LEVEL:C;",
            ranking_value="custom.rank",
            sell_value="custom.level",
            sell_value_operator="gte",
            sell_value_threshold=14.0,
            stop_loss_pct=None,
            initial_cash=100_000.0,
            max_positions=3,
            **overrides,
        )
    )


def test_next_plan_lists_the_orders_pending_for_the_next_session() -> None:
    """引擎是 T 日收盘判定、T+1 开盘撮合，所以收尾时还挂着的单就是明天的动作。"""

    plan = dict(_planning_report().next_plan)

    assert plan["as_of"] == "2024-01-06"
    assert plan["rebalance_due"] is True
    # 卖出量是持仓的实际股数，不是估算——它今天就已经确定了。
    sells = plan["sells"]
    assert sells and sells[0]["code"] == "600001"
    assert sells[0]["size"] == 2700.0
    assert sells[0]["signal_date"] == "2024-01-06"
    # 买入只有估算值：明天的开盘价今天还不存在，字段必须带 estimated_ 前缀，
    # 免得有人把它当实际成交。
    buys = plan["buys"]
    assert buys and buys[0]["code"] == "600000"
    assert buys[0]["estimated_size"] > 0
    assert buys[0]["estimated_cost"] > 0
    assert any("估算" in str(note) for note in plan["notes"])
    # 卖出回款要算进可用现金，否则会低估明天买得起多少。
    assert plan["estimated_cash_after_sells"] > plan["cash"]


def test_next_plan_withholds_buys_when_the_last_bar_is_not_a_rebalance_day() -> None:
    """月 / 周频策略在回测最后一棒也会选出候选（收尾的副产品），不能当成明日计划。

    ``_should_refresh`` 让最后一天恒为调仓日——否则回测收尾那天选不出票。这个
    副作用对回测无害（那些单没机会成交），但直接展示就等于每天给一份假计划。
    """

    plan = dict(_planning_report(rebalance_frequency="monthly").next_plan)

    assert plan["rebalance_due"] is False
    assert plan["buys"] == []
    assert any("不是调仓日" in str(note) for note in plan["notes"])
    # 卖出不受调仓频率影响：触发了就该卖。
    assert plan["sells"] or plan["holdings"]


def test_next_plan_prices_a_candidate_the_strategy_never_held() -> None:
    """买入候选按定义不在持仓里，估算价必须自己取末棒收盘。

    曾经从 ``last_prices`` 取价，而它只在遍历持仓时更新——从没买过的标的恒为 0，
    整笔单被 ``price <= 0`` 静默丢掉：候选明明上榜，计划里却空空如也。
    """

    # 600003 前五天最便宜（排名低、没被选中），最后一天暴涨到榜首，全程从未持有。
    adapter = FakeAdapter()
    adapter.frames["600003"] = stock_frame([1, 1, 1, 1, 1, 100])
    report = PortfolioBacktestService(adapter=adapter).run(
        config(
            formula_text="BUY:C>0; RANK:C; LEVEL:C;",
            ranking_value="custom.rank",
            sell_value="custom.level",
            sell_value_operator="gte",
            sell_value_threshold=14.0,
            stop_loss_pct=None,
            initial_cash=100_000.0,
            max_positions=1,
        )
    )
    plan = dict(report.next_plan)

    codes = {str(item["code"]) for item in plan["buys"]}
    assert "600003" in codes
    assert "600003" not in {str(h["code"]) for h in plan["holdings"]}
    buy = next(item for item in plan["buys"] if str(item["code"]) == "600003")
    assert float(buy["estimated_size"]) > 0
    assert float(buy["estimated_cost"]) > 0


def test_rebalance_due_compares_with_the_next_workday_not_the_next_calendar_day() -> None:
    """外推「下一个工作日」再比，否则周频永远拿不到计划、月频漏掉月末撞周末的月份。

    拿自然日 +1 比的话，周频只在 as_of 是周日时才判跨周，而 A 股没有周日交易；
    月频则会在月末落在周末时漏判（最后一根 bar 的次日仍在本月）。
    """

    due = PortfolioBacktestService(adapter=FakeAdapter())._rebalance_due

    # 2026-09-21 周一 … 2026-09-28 周一：只有周五（次日跨到下周）该给计划。
    assert due(20260921, "weekly") is False
    assert due(20260924, "weekly") is False
    assert due(20260925, "weekly") is True
    assert due(20260928, "weekly") is False
    # 2026-05-30 周六、31 周日：最后一个交易日是 29 日周五，真实次日已跨月。
    assert due(20260529, "monthly") is True
    # 月中的日子不该被判成调仓日。
    assert due(20260515, "monthly") is False
    assert due(20240106, "monthly") is False
    # daily 每天都调仓。
    assert due(20260515, "daily") is True


def test_portfolio_benchmark_credits_cash_dividends() -> None:
    """组合基准必须是分红再投资口径：现金分红不能凭空蒸发。

    这段行情除权后价格稳定在 19.5：raw 看是 20 → 19.5（-2.5%），可持有者手上多出
    0.5 元现金，真实收益是 0%。基准若用不复权价，-2.5% 这个数会被当成"这类股票的
    自然损耗"，策略只要不那么亏就算跑赢——超额收益系统性虚高。
    """

    before, cash_per_share = 20.0, 0.5
    after = before - cash_per_share
    ex_index = 30
    dates = pd.date_range("2024-01-02", periods=50, freq="B")
    close = np.where(np.arange(50) < ex_index, before, after)
    frame = pd.DataFrame(
        {
            "date": dates,
            "open": close,
            "high": close + 0.02,
            "low": close - 0.02,
            "close": close,
            "volume": np.full(50, 100_000.0),
            "amount": close * 100_000.0,
        }
    )
    actions = (
        CorporateAction(
            market="SH",
            code="600000",
            ex_date=dates[ex_index].date(),
            share_ratio=0.0,
            cash_per_share=cash_per_share,
            factor_ratio=compute_factor_ratio(
                prev_close=before,
                share_ratio=0.0,
                cash_per_share=cash_per_share,
            ),
            action_type="dividend",
        ),
    )

    class DividendAdapter(FakeAdapter):
        def list_stock_refs(self, vipdoc_path, universe, universe_file=None):
            return [
                StockRef(
                    market="SH",
                    code="600000",
                    name="测试600000",
                    path=Path(vipdoc_path) / "sh600000.day",
                )
            ]

        def read_stock(self, ref: StockRef) -> pd.DataFrame:
            return frame.copy(deep=True)

        def read_corporate_actions(self, ref: StockRef):
            return actions

    settings = config(
        formula_text="BUY:C>19; RANK:C;",
        max_positions=1,
        initial_cash=100_000.0,
        start_date=dates[0].date(),
        end_date=dates[-1].date(),
        stop_loss_pct=None,
        # 只为满足"至少一个卖出规则"的校验，这段行情里不会触发。
        take_profit_pct=10.0,
    )

    report = PortfolioBacktestService(adapter=DividendAdapter()).run(settings)

    assert report.benchmark is not None
    performance = cast("Mapping[str, object]", report.benchmark["performance"])
    # 不复权口径会是 (19.5 / 20) - 1 = -2.5%。
    assert float(cast("float", performance["total_return"])) == pytest.approx(0.0, abs=1e-9)
