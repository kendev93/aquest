"""Rolling re-validation: same candidates, several independent windows.

条件研究原来是「训练挖候选 → 验证过约束 → 留出复验一次」，结论依赖这一次怎么切。
这里把留出段再切成 K 个连续窗口，逐个回放同一批候选 + 基线，于是每个候选都拿到
K 份互相独立的样本外成绩。

刻意不做两件事：

1. **不给"稳健度评分"。** 输出是「K 窗里 M 窗优于基线」加每窗的原始数值，
   判定的阈值该由用户的用途决定——这是我们对衰减比所持的同一立场。
2. **不把"没样本"算成"没通过"。** 窗口内成交数不足 `min_trades` 时该窗标
   `insufficient_sample`，并从一致性统计的分子分母里一起剔掉；否则一个短窗口
   会被读成"这条条件在这个窗口失效了"，而事实只是那几天没几笔成交。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .time_split import MIN_WINDOW_DAYS, DateWindow


def build_window_payload(
    *,
    index: int,
    window: DateWindow,
    summary: Mapping[str, object],
    baseline_summary: Mapping[str, object],
    min_trades: int,
) -> dict[str, Any]:
    """One window's verdict for one candidate."""

    candidate = _pooled(summary)
    baseline = _pooled(baseline_summary)
    trades = _number(candidate.get("closed_trades")) or 0.0
    win_rate = _number(candidate.get("win_rate"))
    baseline_rate = _number(baseline.get("win_rate"))
    return {
        "index": index,
        "start_date": window.start_date.isoformat(),
        "end_date": window.end_date.isoformat(),
        "calendar_days": (window.end_date - window.start_date).days + 1,
        "trades": int(trades),
        "win_rate": win_rate,
        "baseline_win_rate": baseline_rate,
        "improved_stock_ratio": _stock_number(summary, "improved_stock_ratio"),
        "improved_win_rate": (
            bool(win_rate > baseline_rate)
            if win_rate is not None and baseline_rate is not None
            else None
        ),
        "insufficient_sample": trades < min_trades,
    }


def build_baseline_window_payload(
    *,
    index: int,
    window: DateWindow,
    baseline_summary: Mapping[str, object],
) -> dict[str, Any]:
    """Same window, baseline only. 没有它就没法逐窗对照。"""

    pooled = _pooled(baseline_summary)
    return {
        "index": index,
        "start_date": window.start_date.isoformat(),
        "end_date": window.end_date.isoformat(),
        "calendar_days": (window.end_date - window.start_date).days + 1,
        "trades": int(_number(pooled.get("closed_trades")) or 0.0),
        "win_rate": _number(pooled.get("win_rate")),
    }


def build_consistency(
    windows: Sequence[Mapping[str, Any]],
    *,
    requested: int,
    used: int,
    min_trades: int,
) -> dict[str, Any]:
    """Aggregate the per-window verdicts into one comparable summary."""

    usable = [item for item in windows if not bool(item.get("insufficient_sample"))]
    skipped = len(windows) - len(usable)
    win_rates = [
        float(item["win_rate"]) for item in usable if _number(item.get("win_rate")) is not None
    ]
    improved_ratios = [
        float(item["improved_stock_ratio"])
        for item in usable
        if _number(item.get("improved_stock_ratio")) is not None
    ]
    better = sum(1 for item in usable if item.get("improved_win_rate") is True)
    notes: list[str] = []
    if used < requested:
        notes.append(
            f"留出期只够切 {used} 个窗口（要求 {requested} 个），"
            f"每窗至少 {MIN_WINDOW_DAYS} 个自然日"
        )
    if skipped:
        notes.append(f"{skipped} 个窗口成交数不足 {min_trades} 笔，未计入一致性统计")
    return {
        "requested": requested,
        "used": used,
        "evaluated": len(usable),
        "skipped": skipped,
        "better_than_baseline": better,
        "median_win_rate": _median(win_rates),
        "worst_win_rate": min(win_rates) if win_rates else None,
        "median_improved_stock_ratio": _median(improved_ratios),
        "note": "；".join(notes) or None,
    }


def _pooled(summary: Mapping[str, object]) -> Mapping[str, object]:
    value = summary.get("pooled_trades")
    return value if isinstance(value, Mapping) else {}


def _stock_number(summary: Mapping[str, object], key: str) -> float | None:
    value = summary.get("per_stock")
    if not isinstance(value, Mapping):
        return None
    return _number(value.get(key))


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2
