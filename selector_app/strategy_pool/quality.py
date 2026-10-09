"""策略池信号展示的质量口径：样本不足与跑输基准的判定。

2026-10-09 用户决策：三个信号列表（当天触发买入 / 当天触发卖出 / 今日持仓中）
只展示「值得按其行动」的策略——全区间平仓交易不足或全区间跑输买入持有的
标的直接过滤；这类策略在增量研究里按 ``WEAK_STRATEGY_RECHECK_DAYS`` 的节奏
重新寻优（与 ``no_strategy`` 的 30 天重查同款机制，见 service._can_reuse）。

口径一律用全区间（2016 年至今的实际回放），与「全市场标的状态」表格的
「策略收益 vs 全段基准」完全一致。判定函数只吃候选指标与基准值两个输入，
供信号扫描（service._scan_signals）、跟踪投影（tracking_runner）与增量复用
判定三处共用——三条路径的口径必须一致，改动要同时过这三处。
"""

from __future__ import annotations

from collections.abc import Mapping

#: 全区间平仓交易低于该笔数的策略，胜率 / 盈亏比统计上不可信（2026-10-09 用户选定）。
WEAK_STRATEGY_MIN_TRADES = 20

#: 弱策略的重新寻优节奏（自然日），从上次研究完成时间起算。
WEAK_STRATEGY_RECHECK_DAYS = 30

REASON_INSUFFICIENT_SAMPLE = "insufficient_sample"
REASON_UNDERPERFORMING = "underperforming"


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def benchmark_full_return(benchmark: Mapping[str, object] | None) -> float | None:
    """条目 benchmark 结构里的全区间基准收益（与前端 benchmarkReturn 同口径）。"""

    if not isinstance(benchmark, Mapping):
        return None
    full = benchmark.get("full_period")
    if not isinstance(full, Mapping):
        return None
    return _finite(full.get("total_return"))


def weak_strategy_reasons(
    metrics: Mapping[str, object] | None,
    benchmark_return: float | None,
) -> tuple[str, ...]:
    """返回候选策略命中的质量缺陷；空元组表示可以正常展示 / 复用。

    - ``insufficient_sample``：全区间平仓交易不足 ``WEAK_STRATEGY_MIN_TRADES`` 笔。
    - ``underperforming``：全区间策略收益低于同口径基准（分红再投资买入持有）。
    - 全区间指标缺失时无法判定（增量补算机制会填上），视为不命中，避免把
      尚未补算的老条目整体隐藏；基准缺失时只判样本不判收益。
    """

    full = metrics.get("full_period") if isinstance(metrics, Mapping) else None
    full = full if isinstance(full, Mapping) else {}
    reasons: list[str] = []
    closed = _finite(full.get("closed_trades"))
    if closed is not None and closed < WEAK_STRATEGY_MIN_TRADES:
        reasons.append(REASON_INSUFFICIENT_SAMPLE)
    strategy_return = _finite(full.get("total_return"))
    if (
        strategy_return is not None
        and benchmark_return is not None
        and strategy_return < benchmark_return
    ):
        reasons.append(REASON_UNDERPERFORMING)
    return tuple(reasons)


def strategy_is_weak(
    metrics: Mapping[str, object] | None,
    benchmark_return: float | None,
) -> bool:
    """便捷判断：候选策略是否命中任一质量缺陷。"""

    return bool(weak_strategy_reasons(metrics, benchmark_return))
