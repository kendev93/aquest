"""弱策略质量口径（quality 模块）的单测。

2026-10-09 用户决策：三个信号列表过滤「全区间样本不足 / 跑输基准」的策略，
判定逻辑被信号扫描、跟踪投影、增量复用三处共用，口径改动必须先改这里。
"""

from selector_app.strategy_pool.quality import (
    WEAK_STRATEGY_MIN_TRADES,
    benchmark_full_return,
    strategy_is_weak,
    weak_strategy_reasons,
)


def metrics(closed_trades: float | None = None, total_return: float | None = None) -> dict:
    full: dict = {}
    if closed_trades is not None:
        full["closed_trades"] = closed_trades
    if total_return is not None:
        full["total_return"] = total_return
    return {"full_period": full} if full else {}


def test_insufficient_sample_below_threshold_and_boundary_is_inclusive() -> None:
    reasons = weak_strategy_reasons(
        metrics(closed_trades=WEAK_STRATEGY_MIN_TRADES - 1, total_return=0.9), 0.5
    )
    assert reasons == ("insufficient_sample",)
    # 恰好达到门槛不算样本不足
    assert "insufficient_sample" not in weak_strategy_reasons(
        metrics(closed_trades=WEAK_STRATEGY_MIN_TRADES, total_return=0.9), 0.5
    )


def test_underperforming_only_when_below_full_period_benchmark() -> None:
    assert weak_strategy_reasons(metrics(closed_trades=50, total_return=0.1), 0.5) == (
        "underperforming",
    )
    assert not strategy_is_weak(metrics(closed_trades=50, total_return=0.5), 0.5)
    assert not strategy_is_weak(metrics(closed_trades=50, total_return=0.6), 0.5)


def test_missing_data_does_not_hide_entries() -> None:
    # 全区间指标缺失（尚未补算）与基准缺失（待回填）都无法判定，不应隐藏，
    # 否则补算完成前整个信号面板会被清空。
    assert weak_strategy_reasons(None, 0.5) == ()
    assert weak_strategy_reasons({}, 0.5) == ()
    assert weak_strategy_reasons({"full_period": {}}, 0.5) == ()
    # 基准缺失时样本判定仍然生效
    assert weak_strategy_reasons(metrics(closed_trades=5), None) == ("insufficient_sample",)


def test_benchmark_full_return_follows_entry_shape() -> None:
    assert benchmark_full_return({"full_period": {"total_return": 0.2}}) == 0.2
    assert benchmark_full_return({"full_period": None}) is None
    assert benchmark_full_return({"validation": {"total_return": 0.2}}) is None
    assert benchmark_full_return(None) is None
    assert benchmark_full_return("bad") is None
