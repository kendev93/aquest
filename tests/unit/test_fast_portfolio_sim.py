"""fast_sim 内核与 ``_simulate`` 原状态机的新旧对拍。

覆盖发现寻优实际使用的形态（include_details=False）：单股/多标的、日/周/月
调仓、风险退出（pct/atr/移动止盈 + 冷却）、除权除息、最长持有、固定股数、
排序方向、next_close、停牌缺口、优先级分数、排名索引（快路径）与全量扫描
（通用路径）。同输入强制分别走原状态机与内核（JIT 版 + 纯 Python 版），
断言净值、交易、检查点、明日计划逐项完全一致（bit-exact）。
"""

from __future__ import annotations

import os

import numpy as np
import pandas as pd
import pytest

from aquest.jit import fast_sim_stats
from aquest.market_data.adjustment import CorporateAction
from aquest.portfolio_backtest import fast_sim
from aquest.portfolio_backtest.checkpoint import SimulationCheckpoint
from aquest.portfolio_backtest.context import SortedDateIndex, _StockSeries
from aquest.portfolio_backtest.models import (
    SINGLE_SECURITY_RANKING_VALUE,
    PortfolioBacktestConfig,
)
from aquest.portfolio_backtest.service import PortfolioBacktestService
from aquest.screening.models import ScreenCondition
from aquest.screening.rules import RuleGroup


def _yyyymmdd(stamp: pd.Timestamp) -> int:
    return int(stamp.strftime("%Y%m%d"))


def _stamp_of(value: int) -> pd.Timestamp:
    text = str(value)
    return pd.Timestamp(f"{text[:4]}-{text[4:6]}-{text[6:]}")


def _date_text(value: int) -> str:
    """报告里的日期写法（``YYYY-MM-DD``），用来和 ``YYYYMMDD`` 的日期轴对照。"""

    return _stamp_of(value).strftime("%Y-%m-%d")


def _record_rows(value: object) -> list[dict[str, object]]:
    assert isinstance(value, list)
    return [dict(row) for row in value if isinstance(row, dict)]


def _trade_rows(result: dict[str, object]) -> list[dict[str, object]]:
    return _record_rows(result["trades"])


def _plan_rows(result: dict[str, object], section: str) -> list[dict[str, object]]:
    plan = result["next_plan"]
    assert isinstance(plan, dict)
    return _record_rows(plan.get(section))


def make_context(code: str, seed: int, n: int = 280, halted: tuple[int, ...] = ()) -> _StockSeries:
    rng = np.random.default_rng(seed)
    close = np.maximum(np.round(10.0 + np.cumsum(rng.normal(0.0, 0.3, size=n)), 2), 1.5)
    open_ = np.round(np.maximum(close * (1.0 + rng.normal(0.0, 0.01, size=n)), 0.5), 2)
    high = np.round(np.maximum(open_, close) + 0.1, 2)
    low = np.maximum(np.round(np.minimum(open_, close) - 0.1, 2), 0.1)
    keep = np.ones(n, dtype=bool)
    for day in halted:
        keep[day] = False
    stamps = pd.bdate_range("2023-01-02", periods=n)[keep]
    dates = np.asarray([_yyyymmdd(stamp) for stamp in stamps], dtype=np.int64)
    return _StockSeries(
        market="SH",
        code=code,
        instrument_type="stock",
        board="main",
        dates=dates,
        opens=open_[keep],
        highs=high[keep],
        lows=low[keep],
        closes=close[keep],
        atrs=np.round(np.abs(rng.normal(0.4, 0.1, size=n))[keep] + 0.05, 3),
        buy_mask=rng.random(n)[keep] < 0.15,
        sell_mask=rng.random(n)[keep] < 0.10,
        signals={},
        values={"custom.rank": np.round(rng.random(n)[keep], 4)},
        date_to_index=SortedDateIndex(dates),
        data_fingerprint=f"fp-{code}-{seed}",
        name=f"测试{code}",
    )


def global_dates(contexts: list[_StockSeries]) -> list[int]:
    return sorted({int(value) for context in contexts for value in context.dates})


def make_config(**overrides: object) -> PortfolioBacktestConfig:
    base: dict[str, object] = {
        "vipdoc_path": "",
        "universe": "codes",
        "codes": ("SH600000", "SH600001"),
        "selected_signals": (),
        "combine_mode": "all",
        "minimum_matches": None,
        "ranking_value": "custom.rank",
        "max_positions": 2,
        "rebalance_frequency": "daily",
        "execution": "next_open",
        "initial_cash": 100_000.0,
        "buy_rule": RuleGroup(
            conditions=(
                ScreenCondition(left_value="price.close", operator="gt", right_constant=0.0),
            )
        ),
        "sell_rule": RuleGroup(
            conditions=(
                ScreenCondition(left_value="price.close", operator="gt", right_constant=0.0),
            )
        ),
    }
    base.update(overrides)
    return PortfolioBacktestConfig(**base)


class ActionAdapter:
    """提供除权事件的最小适配器；没有事件时返回空元组。"""

    def __init__(self, actions: dict[str, tuple[CorporateAction, ...]] | None = None) -> None:
        self._actions = actions or {}

    def read_corporate_actions(self, ref):  # noqa: ANN001, ANN202 - 测试桩
        return self._actions.get(ref.code, ())


class _SimService(PortfolioBacktestService):
    """绕过 DuckDB 默认适配器的测试服务。"""

    def __init__(self, actions: dict[str, tuple[CorporateAction, ...]] | None = None) -> None:
        self._adapter = ActionAdapter(actions)
        self._registry = None  # type: ignore[assignment]
        self._indicator_cache = None
        self._risk_ranked_context_source = None
        self._risk_ranked_context_key = None
        self._risk_ranked_candidate_index = None


def run_sim(
    service: PortfolioBacktestService,
    contexts: list[_StockSeries],
    dates: list[int],
    config: PortfolioBacktestConfig,
    *,
    ranked: bool,
    pure_kernel: bool = False,
    checkpoint: SimulationCheckpoint | None = None,
) -> dict[str, object]:
    ranked_index = (
        service._build_ranked_candidate_index(contexts, dates, config) if ranked else None
    )
    fast_sim_stats(reset=True)
    try:
        if pure_kernel:
            # 内核未包装的纯 Python 版本：验证移植逻辑本身，不依赖 numba。
            original = fast_sim._PORTFOLIO_KERNEL
            fast_sim._PORTFOLIO_KERNEL = fast_sim._portfolio_kernel_impl
            try:
                result = service._simulate(
                    contexts,
                    dates,
                    config,
                    None,
                    include_details=False,
                    data_fingerprint="test",
                    ranked_candidate_index=ranked_index,
                    checkpoint=checkpoint,
                )
            finally:
                fast_sim._PORTFOLIO_KERNEL = original
        else:
            result = service._simulate(
                contexts,
                dates,
                config,
                None,
                include_details=False,
                data_fingerprint="test",
                ranked_candidate_index=ranked_index,
                checkpoint=checkpoint,
            )
    finally:
        stats = fast_sim_stats(reset=True)
    # 内核回退时产出的是原实现的结果，对拍照样「通过」—— 所以必须显式断言内核真的
    # 跑了。否则内核静默失效（曾经因为除权事件排序键取错属性，生产环境 100% 回退）
    # 在这套测试里完全看不出来。
    if os.environ.get("AQUEST_FAST_SIM", "1") != "0":
        assert stats.get("portfolio:hit") == 1, f"内核未命中（静默回退）：{stats}"
    return result


def assert_same(baseline: dict[str, object], candidate: dict[str, object]) -> None:
    for key in (
        "cash",
        "equity",
        "trades",
        "states",
        "ranking_events",
        "current_state",
        "today_candidates",
        "next_plan",
    ):
        assert baseline[key] == candidate[key], key
    assert baseline["checkpoint"] == candidate["checkpoint"]


def make_hold_context(
    code: str,
    n: int = 280,
    buy_days: tuple[int, ...] = (10,),
    sell_days: tuple[int, ...] = (),
) -> _StockSeries:
    """买卖点完全指定的上下文：把持仓/挂单精确摆在检查点分界两侧用。"""

    rng = np.random.default_rng(7)
    close = np.maximum(np.round(10.0 + np.cumsum(rng.normal(0.0, 0.3, size=n)), 2), 1.5)
    open_ = np.round(np.maximum(close * (1.0 + rng.normal(0.0, 0.01, size=n)), 0.5), 2)
    high = np.round(np.maximum(open_, close) + 0.1, 2)
    low = np.maximum(np.round(np.minimum(open_, close) - 0.1, 2), 0.1)
    stamps = pd.bdate_range("2023-01-02", periods=n)
    dates = np.asarray([_yyyymmdd(stamp) for stamp in stamps], dtype=np.int64)
    buy_mask = np.zeros(n, dtype=bool)
    buy_mask[list(buy_days)] = True
    sell_mask = np.zeros(n, dtype=bool)
    sell_mask[list(sell_days)] = True
    return _StockSeries(
        market="SH",
        code=code,
        instrument_type="stock",
        board="main",
        dates=dates,
        opens=open_,
        highs=high,
        lows=low,
        closes=close,
        atrs=np.round(np.abs(rng.normal(0.4, 0.1, size=n)) + 0.05, 3),
        buy_mask=buy_mask,
        sell_mask=sell_mask,
        signals={},
        values={"custom.rank": np.round(rng.random(n), 4)},
        date_to_index=SortedDateIndex(dates),
        data_fingerprint=f"fp-hold-{code}",
        name=f"测试{code}",
    )


_RISK_STOP_LOSS_PCT = 0.05
_RISK_COOLDOWN_DAYS = 3
_RISK_BREACH_DAY = 99


def make_risk_hold_context(
    code: str,
    n: int = 280,
    *,
    buy_day: int = 10,
    breach_day: int = _RISK_BREACH_DAY,
) -> _StockSeries:
    """价格平直、只在指定那根 bar 打到止损线的上下文。

    买入次日开盘 10.0 → 止损线 9.5；其余各日低点 9.95 恒在线上，只有 breach_day
    那根收到 9.45。风控挂单因此可以精确摆到检查点分界的那一根上。
    """

    stamps = pd.bdate_range("2023-01-02", periods=n)
    dates = np.asarray([_yyyymmdd(stamp) for stamp in stamps], dtype=np.int64)
    flat = np.full(n, 10.0)
    lows = np.full(n, 9.95)
    lows[breach_day] = 9.45
    buy_mask = np.zeros(n, dtype=bool)
    buy_mask[buy_day] = True
    return _StockSeries(
        market="SH",
        code=code,
        instrument_type="stock",
        board="main",
        dates=dates,
        opens=flat,
        highs=flat.copy(),
        lows=lows,
        closes=flat.copy(),
        atrs=np.full(n, 0.4),
        buy_mask=buy_mask,
        sell_mask=np.zeros(n, dtype=bool),
        signals={},
        values={"custom.rank": np.full(n, 1.0)},
        date_to_index=SortedDateIndex(dates),
        data_fingerprint=f"fp-risk-{code}",
        name=f"测试{code}",
    )


def risk_config(**overrides: object) -> PortfolioBacktestConfig:
    base: dict[str, object] = {
        "codes": ("SH600000",),
        "max_positions": 1,
        "stop_loss_pct": _RISK_STOP_LOSS_PCT,
        "stop_loss_cooldown_days": _RISK_COOLDOWN_DAYS,
    }
    base.update(overrides)
    return make_config(**base)


def _resume_case(
    monkeypatch: pytest.MonkeyPatch,
    service: PortfolioBacktestService,
    contexts: list[_StockSeries],
    config: PortfolioBacktestConfig,
    *,
    ranked: bool,
    split_fraction: float = 0.4,
    split: int | None = None,
) -> SimulationCheckpoint:
    """前缀 → 检查点 → 续跑的三路对拍（原实现 / JIT 内核 / 纯 Python 内核）。

    前缀用原实现跑出检查点，续跑分别用三条路径跑完整日期区间；
    run_sim 的「必须命中」断言保证内核路径不会静默回退。返回检查点供调用方继续
    断言挂单字段——没有这个断言，「边界上挂着风控单」的用例可能其实没挂到。
    """

    dates = global_dates(contexts)
    boundary = split if split is not None else max(1, int(len(dates) * split_fraction))

    monkeypatch.setenv("AQUEST_FAST_SIM", "0")
    prefix = run_sim(service, contexts, dates[:boundary], config, ranked=ranked)
    checkpoint = prefix["checkpoint"]
    assert isinstance(checkpoint, SimulationCheckpoint)
    assert not checkpoint.include_details
    baseline = run_sim(service, contexts, dates, config, ranked=ranked, checkpoint=checkpoint)

    monkeypatch.setenv("AQUEST_FAST_SIM", "1")
    jitted = run_sim(service, contexts, dates, config, ranked=ranked, checkpoint=checkpoint)
    assert_same(baseline, jitted)

    pure = run_sim(
        service,
        contexts,
        dates,
        config,
        ranked=ranked,
        checkpoint=checkpoint,
        pure_kernel=True,
    )
    assert_same(baseline, pure)
    return checkpoint


_SCENARIOS: list[tuple[str, dict[str, object], list[_StockSeries]]] = [
    (
        "single_security_daily",
        {
            "codes": ("SH600000",),
            "ranking_value": SINGLE_SECURITY_RANKING_VALUE,
            "single_security_mode": True,
            "max_positions": 1,
        },
        [make_context("600000", 1)],
    ),
    (
        "two_symbols_weekly",
        {"rebalance_frequency": "weekly", "max_positions": 2},
        [make_context("600000", 2), make_context("600001", 3, halted=(7, 8, 40))],
    ),
    (
        "risk_exits",
        {
            "stop_loss_pct": 0.05,
            "stop_loss_cooldown_days": 3,
            "take_profit_pct": 0.08,
            "take_profit_cooldown_days": 2,
            "trailing_stop_mode": "atr",
            "trailing_stop_atr_multiplier": 2.0,
            "trailing_stop_cooldown_days": 5,
        },
        [make_context("600000", 4), make_context("600001", 5)],
    ),
    (
        "atr_stop_loss",
        {
            "stop_loss_mode": "atr",
            "stop_loss_atr_multiplier": 1.5,
            "stop_loss_cooldown_days": 4,
        },
        [make_context("600000", 6)],
    ),
    (
        "max_hold_days",
        {"max_hold_days": 10, "rebalance_frequency": "weekly"},
        [make_context("600000", 7), make_context("600001", 8)],
    ),
    (
        "fixed_size",
        {"position_mode": "fixed", "fixed_size": 1000, "max_positions": 2},
        [make_context("600000", 9), make_context("600001", 10)],
    ),
    (
        "rank_asc_next_close",
        {"rank_order": "asc", "execution": "next_close", "max_positions": 1},
        [make_context("600000", 11), make_context("600001", 12)],
    ),
    (
        "priority_scores",
        {"symbol_priority_scores": {"SH600000": 0.7}, "max_positions": 2},
        [make_context("600000", 13), make_context("600001", 14)],
    ),
]


@pytest.fixture(params=[True, False], ids=["ranked", "general"])
def ranked_mode(request: pytest.FixtureRequest) -> bool:
    return bool(request.param)


@pytest.mark.parametrize("name,overrides,contexts", _SCENARIOS, ids=[s[0] for s in _SCENARIOS])
def test_fast_portfolio_sim_matches_original(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    overrides: dict[str, object],
    contexts: list[_StockSeries],
    ranked_mode: bool,
) -> None:
    del name
    dates = global_dates(contexts)
    config = make_config(**overrides)
    service = _SimService()

    monkeypatch.setenv("AQUEST_FAST_SIM", "0")
    baseline = run_sim(service, contexts, dates, config, ranked=ranked_mode)

    monkeypatch.setenv("AQUEST_FAST_SIM", "1")
    # 先直接确认内核路径可用（而不是静默回退到 Python）。
    ranked_index = (
        service._build_ranked_candidate_index(contexts, dates, config) if ranked_mode else None
    )
    assert (
        fast_sim.try_fast_portfolio_simulation(
            service,
            contexts,
            dates,
            config,
            data_fingerprint="test",
            ranked_candidate_index=ranked_index,
        )
        is not None
    )
    jitted = run_sim(service, contexts, dates, config, ranked=ranked_mode)
    assert_same(baseline, jitted)

    pure = run_sim(service, contexts, dates, config, ranked=ranked_mode, pure_kernel=True)
    assert_same(baseline, pure)


def test_fast_portfolio_sim_with_corporate_actions(
    monkeypatch: pytest.MonkeyPatch, ranked_mode: bool
) -> None:
    context = make_context("600000", 21)
    dates = global_dates([context])
    actions = (
        CorporateAction(
            market="SH",
            code="600000",
            ex_date=_stamp_of(dates[60]).date(),
            share_ratio=0.0,
            cash_per_share=0.3,
            factor_ratio=1.001,
            action_type="dividend",
        ),
        CorporateAction(
            market="SH",
            code="600000",
            ex_date=_stamp_of(dates[140]).date(),
            share_ratio=0.5,
            cash_per_share=0.0,
            factor_ratio=1.5,
            action_type="bonus",
        ),
    )
    config = make_config(codes=("SH600000",), max_positions=1)
    service = _SimService({"600000": actions})

    monkeypatch.setenv("AQUEST_FAST_SIM", "0")
    baseline = run_sim(service, [context], dates, config, ranked=ranked_mode)

    monkeypatch.setenv("AQUEST_FAST_SIM", "1")
    jitted = run_sim(service, [context], dates, config, ranked=ranked_mode)
    assert_same(baseline, jitted)

    pure = run_sim(service, [context], dates, config, ranked=ranked_mode, pure_kernel=True)
    assert_same(baseline, pure)


def test_fast_portfolio_sim_monthly_rebalance(
    monkeypatch: pytest.MonkeyPatch, ranked_mode: bool
) -> None:
    contexts = [make_context("600000", 31), make_context("600001", 32)]
    dates = global_dates(contexts)
    config = make_config(rebalance_frequency="monthly", max_positions=2)
    service = _SimService()

    monkeypatch.setenv("AQUEST_FAST_SIM", "0")
    baseline = run_sim(service, contexts, dates, config, ranked=ranked_mode)

    monkeypatch.setenv("AQUEST_FAST_SIM", "1")
    jitted = run_sim(service, contexts, dates, config, ranked=ranked_mode)
    assert_same(baseline, jitted)


@pytest.mark.parametrize("name,overrides,contexts", _SCENARIOS, ids=[s[0] for s in _SCENARIOS])
def test_fast_portfolio_sim_resume_matches_original(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    overrides: dict[str, object],
    contexts: list[_StockSeries],
    ranked_mode: bool,
) -> None:
    """全部既有场景走「前缀 → 检查点 → 续跑」三路对拍。"""

    del name
    config = make_config(**overrides)
    _resume_case(monkeypatch, _SimService(), contexts, config, ranked=ranked_mode)


def test_fast_portfolio_sim_resume_with_corporate_actions(
    monkeypatch: pytest.MonkeyPatch, ranked_mode: bool
) -> None:
    """持仓跨过分界日：前缀内已应用的除权不得在续跑段重复应用。

    day 60 的分红落在前缀（进检查点的 applied 集合），day 140 的送股落在续跑段；
    若内核续跑不还原 applied 状态，day 60 的除权会被重复应用并立刻在对拍里暴露。
    """

    context = make_hold_context("600000")
    dates = global_dates([context])
    actions = (
        CorporateAction(
            market="SH",
            code="600000",
            ex_date=_stamp_of(dates[60]).date(),
            share_ratio=0.0,
            cash_per_share=0.3,
            factor_ratio=1.001,
            action_type="dividend",
        ),
        CorporateAction(
            market="SH",
            code="600000",
            ex_date=_stamp_of(dates[140]).date(),
            share_ratio=0.5,
            cash_per_share=0.0,
            factor_ratio=1.5,
            action_type="bonus",
        ),
    )
    config = make_config(codes=("SH600000",), max_positions=1)
    service = _SimService({"600000": actions})
    _resume_case(monkeypatch, service, [context], config, ranked=ranked_mode, split=100)


def test_fast_portfolio_sim_resume_pending_sell_at_boundary(
    monkeypatch: pytest.MonkeyPatch, ranked_mode: bool
) -> None:
    """分界日收盘挂出规则卖单、续跑首日开盘执行——覆盖挂单状态的恢复。"""

    context = make_hold_context("600000", sell_days=(99,))
    config = make_config(codes=("SH600000",), max_positions=1)
    service = _SimService()
    _resume_case(monkeypatch, service, [context], config, ranked=ranked_mode, split=100)


def test_fast_portfolio_sim_resume_risk_sell_at_boundary(
    monkeypatch: pytest.MonkeyPatch, ranked_mode: bool
) -> None:
    """风控挂单恰好停在检查点边界——内核恢复那一段唯一的真载荷。

    分界日就是触碰日，所以检查点里的这条单既不是规则卖出、也还没有成交记录：
    reason 要用 int8 编码回「止损」，risk_trigger 必须与它自洽（对不上内核会交回
    原实现），冷却天数走 ps_cd 单独一列。任一项没还原，续跑首日就少一笔冷却或者
    直接静默回退——run_sim 的「必须命中」断言和三路对拍一起兜住。
    """

    context = make_risk_hold_context("600000")
    service = _SimService()
    checkpoint = _resume_case(
        monkeypatch,
        service,
        [context],
        risk_config(),
        ranked=ranked_mode,
        split=_RISK_BREACH_DAY + 1,
    )
    pending = checkpoint.pending_sells["SH600000"]
    assert pending.reason == "止损"
    assert pending.risk_trigger == "stop_loss"
    assert pending.cooldown_days == _RISK_COOLDOWN_DAYS
    assert pending.signal_date == global_dates([context])[_RISK_BREACH_DAY]


def test_checkpoint_roundtrip_keeps_risk_fields_and_loads_legacy_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """风控挂单的序列化：新键原样往返，旧形状（没有这两个键）也要读得进来。

    检查点格式没有升 version——研究侧的多段检查点只在进程内存在，池跟踪另有更硬
    的盐，所以这里靠「缺键取默认」保持可读，而不是把历史 blob 判死。
    """

    context = make_risk_hold_context("600000")
    dates = global_dates([context])
    monkeypatch.setenv("AQUEST_FAST_SIM", "0")
    prefix = run_sim(
        _SimService(),
        [context],
        dates[: _RISK_BREACH_DAY + 1],
        risk_config(),
        ranked=False,
    )
    checkpoint = prefix["checkpoint"]
    assert isinstance(checkpoint, SimulationCheckpoint)
    pending = checkpoint.pending_sells["SH600000"]
    assert pending.risk_trigger == "stop_loss"
    assert pending.cooldown_days == _RISK_COOLDOWN_DAYS

    payload = checkpoint.to_dict()
    sells = payload["pending_sells"]
    assert isinstance(sells, dict)
    assert sells["SH600000"]["risk_trigger"] == "stop_loss"
    assert sells["SH600000"]["cooldown_days"] == _RISK_COOLDOWN_DAYS
    assert SimulationCheckpoint.from_dict(payload) == checkpoint

    for key in ("risk_trigger", "cooldown_days"):
        sells["SH600000"].pop(key)
    restored = SimulationCheckpoint.from_dict(payload)
    legacy = restored.pending_sells["SH600000"]
    assert legacy.reason == "止损"
    assert legacy.risk_trigger is None
    assert legacy.cooldown_days == 0


def test_kernel_defers_the_risk_exit_to_the_next_open(
    monkeypatch: pytest.MonkeyPatch, ranked_mode: bool
) -> None:
    """内核与状态机同一条口径：触碰当根不平仓，下一根开盘才成交。

    寻优跑的是内核、上报的是状态机；两边成交日不同，选出来的风控参数就是在优化
    一个界面上看不到的口径。
    """

    context = make_risk_hold_context("600000")
    dates = global_dates([context])
    config = risk_config()
    service = _SimService()

    monkeypatch.setenv("AQUEST_FAST_SIM", "0")
    baseline = run_sim(service, [context], dates, config, ranked=ranked_mode)

    monkeypatch.setenv("AQUEST_FAST_SIM", "1")
    jitted = run_sim(service, [context], dates, config, ranked=ranked_mode)
    assert_same(baseline, jitted)
    pure = run_sim(service, [context], dates, config, ranked=ranked_mode, pure_kernel=True)
    assert_same(baseline, pure)

    trades = _trade_rows(baseline)
    assert [row["direction"] for row in trades] == ["BUY", "SELL"]
    assert trades[0]["date"] == _date_text(dates[11])
    sell = trades[1]
    assert sell["reason"] == "止损"
    assert sell["risk_trigger"] == "stop_loss"
    assert sell["cooldown_days"] == _RISK_COOLDOWN_DAYS
    assert sell["signal_date"] == _date_text(dates[_RISK_BREACH_DAY])
    assert sell["date"] == _date_text(dates[_RISK_BREACH_DAY + 1])
    assert float(sell["price"]) == 10.0


def test_kernel_leaves_the_trigger_day_exit_in_the_next_plan(
    monkeypatch: pytest.MonkeyPatch, ranked_mode: bool
) -> None:
    """回测停在触碰日：明天要卖的单据已经排好，线位与判定日期一并给出。"""

    context = make_risk_hold_context("600000")
    dates = global_dates([context])
    window = dates[: _RISK_BREACH_DAY + 1]
    service = _SimService()

    monkeypatch.setenv("AQUEST_FAST_SIM", "0")
    baseline = run_sim(service, [context], window, risk_config(), ranked=ranked_mode)

    monkeypatch.setenv("AQUEST_FAST_SIM", "1")
    jitted = run_sim(service, [context], window, risk_config(), ranked=ranked_mode)
    assert_same(baseline, jitted)
    pure = run_sim(service, [context], window, risk_config(), ranked=ranked_mode, pure_kernel=True)
    assert_same(baseline, pure)

    assert [row for row in _trade_rows(baseline) if row["direction"] == "SELL"] == []
    sells = _plan_rows(baseline, "sells")
    assert len(sells) == 1
    order = sells[0]
    assert order["code"] == "600000"
    assert order["reason"] == "止损"
    assert order["risk_trigger"] == "stop_loss"
    assert order["signal_date"] == _date_text(dates[_RISK_BREACH_DAY])
    assert order["stop_loss_price"] == pytest.approx(9.5)
    assert order["take_profit_price"] is None
    assert order["trailing_stop_price"] is None
    assert order["risk_level_as_of"] == _date_text(dates[_RISK_BREACH_DAY])

    holdings = _plan_rows(baseline, "holdings")
    assert len(holdings) == 1
    assert holdings[0]["pending_exit_reason"] == "止损"
    assert holdings[0]["pending_exit_risk_trigger"] == "stop_loss"
    assert holdings[0]["pending_exit_signal_date"] == _date_text(dates[_RISK_BREACH_DAY])
    assert holdings[0]["max_hold_days"] is None
    assert holdings[0]["max_hold_expiry_date"] is None


def test_equal_length_segments_do_not_share_the_rebalance_mask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """分段评估不能复用另一段的调仓掩码。

    train/validation/test 用的是同一个整帧指纹、切的却是各自的日期区间。缓存键只记天数
    时，两段等长就会让后跑的那段拿到前一段的星期对齐，报出来的分段收益不是它自己的，
    换个执行顺序结论就变。
    """

    monkeypatch.setenv("AQUEST_FAST_SIM", "1")
    contexts = [make_context("600000", 1), make_context("600001", 2)]
    dates = global_dates(contexts)
    head, tail = dates[:139], dates[141:]
    assert len(head) == len(tail)
    config = make_config(rebalance_frequency="weekly")

    warm = _SimService()
    run_sim(warm, contexts, head, config, ranked=False)
    warmed_tail = run_sim(warm, contexts, tail, config, ranked=False)
    cold_tail = run_sim(_SimService(), contexts, tail, config, ranked=False)

    assert_same(cold_tail, warmed_tail)
