"""弱策略质量口径（quality 模块）的单测。

2026-10-09 用户决策：三个信号列表过滤「全区间样本不足 / 跑输基准」的策略，
判定逻辑被信号扫描、跟踪投影、增量复用三处共用，口径改动必须先改这里。
"""

from datetime import date

from aquest.strategy_pool.quality import (
    WEAK_STRATEGY_MIN_TRADES,
    benchmark_full_return,
    fresh_quality_inputs,
    post_launch_degraded,
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


def test_post_launch_degradation_requires_window_return_and_benchmark() -> None:
    degraded = {
        "post_launch_days": 70,
        "post_launch_return": -0.05,
        "post_launch_benchmark_return": 0.15,
    }
    assert post_launch_degraded(degraded)
    # 没满窗口 / 跑赢基准 / 字段缺失（老快照）都不算衰减
    assert not post_launch_degraded({**degraded, "post_launch_days": 59})
    assert not post_launch_degraded({**degraded, "post_launch_return": 0.1})
    assert not post_launch_degraded({**degraded, "post_launch_benchmark_return": -0.2})
    assert not post_launch_degraded({"post_launch_days": 70, "post_launch_return": -0.05})
    assert not post_launch_degraded(None)


def test_fresh_metrics_override_research_metrics_only_when_the_window_covers_research() -> None:
    """弱策略判定用最新口径，但回放窗口必须至少覆盖到研究结论写下那天。

    正常日更下回放只会比研究窗口更长；构造出来的短窗口（数据被清过、夹具）里用它
    判「样本不足」会把正常策略误伤，所以那时回退研究口径。
    """

    candidate = {"metrics": {"full_period": {"closed_trades": 30, "total_return": 0.8}}}
    fresh = {
        "full_period": {"as_of_date": "2026-10-27", "closed_trades": 30, "total_return": 0.1},
        "benchmark": {"full_period": {"total_return": 0.5}},
    }

    metrics, benchmark = fresh_quality_inputs(
        fresh, candidate, fallback_benchmark=0.5, research_end=date(2026, 10, 24)
    )
    assert metrics is not None and metrics["full_period"]["total_return"] == 0.1
    assert strategy_is_weak(metrics, benchmark)  # 研究时跑赢、最新跑输 → 判弱

    metrics, benchmark = fresh_quality_inputs(
        fresh, candidate, fallback_benchmark=0.5, research_end=date(2026, 10, 28)
    )
    assert metrics is not None and metrics["full_period"]["total_return"] == 0.8
    assert not strategy_is_weak(metrics, benchmark)  # 窗口更短：回退研究口径，不误伤

    # 没有研究指标的老条目本来就不参与质量过滤。
    assert fresh_quality_inputs(fresh, {"candidate_id": "x"}) == (None, None)
