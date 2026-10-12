"""Read stored research records and lay them out side by side.

这一层只做编排与**可比性判断**，不重新计算任何研究结果。它最重要的产物其实是
``warnings``：把「这两条不是一回事」说清楚，比把图好看地画出来重要得多。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace

from aquest.market_data.store import DuckDbMarketDataStore

from .extract import extract_curve, extract_date_range, extract_label, extract_metrics
from .models import (
    CURVE_KIND_LABELS,
    CURVE_UNIT_LABELS,
    MAX_COMPARE_ITEMS,
    MAX_CURVE_POINTS,
    METRIC_SAMPLE_DAYS,
    MIN_COMPARE_ITEMS,
    SAMPLE_LENGTH_WARNING_RATIO,
    TYPE_LABELS,
    CompareCurve,
    CompareItem,
    HistoryCompareReport,
)


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


class HistoryCompareService:
    """Compare archived research runs without rerunning anything."""

    def __init__(self, store: DuckDbMarketDataStore) -> None:
        self._store = store

    def candidates(
        self,
        *,
        limit_per_type: int = 20,
    ) -> dict[str, object]:
        """List selectable records across every kind, newest first.

        列表页拿不到完整 result（曲线只在里面），所以这里按**类型 + 摘要**判断
        「大概有没有曲线」。判断错了不会说谎：真去对比时每自己会给出准确原因。
        """

        rows = self._store.list_backtest_history_all(limit_per_type=limit_per_type)
        items: list[dict[str, object]] = []
        for row in rows:
            backtest_type = str(row.get("backtest_type") or "")
            summary = _mapping(row.get("summary"))
            title, subtitle = _candidate_label(backtest_type, summary)
            items.append(
                {
                    "history_id": str(row.get("history_id") or ""),
                    "backtest_type": backtest_type,
                    "type_label": TYPE_LABELS.get(backtest_type, backtest_type),
                    "label": title,
                    "subtitle": subtitle,
                    "created_at": str(row.get("created_at") or ""),
                    "finished_at": str(row.get("finished_at") or ""),
                    "date_range": extract_date_range(summary),
                    "curve_kind": _expected_curve_kind(backtest_type, summary),
                    "curve_kind_label": CURVE_KIND_LABELS.get(
                        _expected_curve_kind(backtest_type, summary) or "", ""
                    ),
                }
            )
        types = sorted({str(item["backtest_type"]) for item in items})
        return {
            "items": items,
            "types": [
                {"backtest_type": value, "type_label": TYPE_LABELS.get(value, value)}
                for value in types
            ],
            "max_items": MAX_COMPARE_ITEMS,
        }

    def compare(
        self,
        history_ids: Iterable[str],
        *,
        normalize: str = "capital",
        x_axis: str = "date",
    ) -> HistoryCompareReport:
        """Build one comparison report for the given records."""

        ids: list[str] = []
        for value in history_ids:
            text = str(value or "").strip()
            if text and text not in ids:
                ids.append(text)
        if len(ids) < MIN_COMPARE_ITEMS:
            raise ValueError(f"至少需要选择 {MIN_COMPARE_ITEMS} 条历史记录")
        if len(ids) > MAX_COMPARE_ITEMS:
            raise ValueError(f"一次最多对比 {MAX_COMPARE_ITEMS} 条历史记录")
        records = self._store.read_backtest_histories(ids)
        found = {str(record.get("history_id")) for record in records}
        missing = [value for value in ids if value not in found]
        if missing:
            raise ValueError(f"以下历史记录不存在: {', '.join(missing)}")
        items = tuple(
            self._build_item(record, normalize=normalize)
            for record in records
            if isinstance(record.get("result"), Mapping)
        )
        if not items:
            raise ValueError("所选历史记录没有可用的结果")
        curve_group = _curve_group(items)
        return HistoryCompareReport(
            normalize=normalize,
            x_axis=x_axis,
            items=items,
            curve_group=curve_group,
            warnings=_warnings(items, curve_group),
        )

    def _build_item(
        self,
        record: Mapping[str, object],
        *,
        normalize: str,
    ) -> CompareItem:
        backtest_type = str(record.get("backtest_type") or "")
        result = _mapping(record.get("result"))
        curve, reason = extract_curve(backtest_type, result, normalize=normalize)
        metrics, native_metrics = extract_metrics(backtest_type, result)
        title, subtitle = extract_label(backtest_type, result)
        history_id = str(record.get("history_id") or "")
        return CompareItem(
            history_id=history_id,
            short_id=history_id[:8],
            backtest_type=backtest_type,
            type_label=TYPE_LABELS.get(backtest_type, backtest_type),
            label=title,
            subtitle=subtitle,
            created_at=str(record.get("created_at") or ""),
            finished_at=str(record.get("finished_at") or ""),
            date_range=extract_date_range(result),
            curve=_thin(curve) if curve is not None else None,
            curve_unavailable_reason=reason,
            metrics=metrics,
            native_metrics=native_metrics,
        )


def _expected_curve_kind(backtest_type: str, summary: Mapping[str, object]) -> str | None:
    """Guess whether a record will have a drawable curve, from its summary."""

    days = summary.get("bars")
    if backtest_type in {"portfolio", "single_stock"}:
        return "equity" if isinstance(days, (int, float)) and days >= 2 else None
    if backtest_type == "factor_test":
        date_range = _mapping(summary.get("date_range"))
        count = date_range.get("days")
        return "factor_long_short" if isinstance(count, (int, float)) and count >= 2 else None
    return None


def _candidate_label(backtest_type: str, summary: Mapping[str, object]) -> tuple[str, str]:
    start = _text(summary.get("start_date"))
    end = _text(summary.get("end_date"))
    if start is None or end is None:
        date_range = _mapping(summary.get("date_range"))
        start = start or _text(date_range.get("start"))
        end = end or _text(date_range.get("end"))
    span = f"{start} → {end}" if start and end else "区间未记录"
    title = f"{TYPE_LABELS.get(backtest_type, backtest_type)} · {span}"
    return title, _candidate_subtitle(backtest_type, summary)


def _candidate_subtitle(backtest_type: str, summary: Mapping[str, object]) -> str:
    universe = _text(summary.get("universe"))
    prefix = universe.upper() if universe else "候选池"
    if backtest_type == "single_stock":
        name = _text(summary.get("name"))
        code = _text(summary.get("code"))
        if name and code:
            return f"{name}（{code}）"
        return name or code or "单只标的"
    if backtest_type == "strategy_fitness":
        count = summary.get("result_count")
        return f"{prefix} · {int(count) if isinstance(count, (int, float)) else 0} 个标的"
    if backtest_type == "condition_research":
        count = summary.get("candidate_count")
        return f"{prefix} · {int(count) if isinstance(count, (int, float)) else 0} 条候选"
    for key in ("scanned_stocks", "stock_count", "processed"):
        count = summary.get(key)
        if isinstance(count, (int, float)) and count:
            return f"{prefix} · 扫描 {int(count)} 只"
    return prefix


def _thin(curve: CompareCurve) -> CompareCurve:
    """Downsample long series, always keeping the first and last point.

    抽的是点不是形状：首尾必留，中间等距取，否则「最终收益」这个最该看的数字
    会被抽掉。
    """

    if len(curve.points) <= MAX_CURVE_POINTS:
        return curve
    last = len(curve.points) - 1
    step = last / (MAX_CURVE_POINTS - 1)
    indices = sorted({min(last, int(round(index * step))) for index in range(MAX_CURVE_POINTS - 1)})
    if last not in indices:
        indices.append(last)
    return replace(
        curve,
        points=tuple(curve.points[index] for index in indices),
        truncated=True,
    )


def _curve_group(items: Sequence[CompareItem]) -> dict[str, object]:
    """Decide which series may share one chart.

    只有同一种口径才能叠绘，而且至少要两条——一条曲线不叫对比。
    """

    kinds = Counter(item.curve.kind for item in items if item.curve is not None)
    if not kinds:
        return {
            "kind": None,
            "kind_label": "",
            "unit": "",
            "plotted": False,
            "history_ids": [],
            "excluded": [],
            "note": "所选记录都没有可绘制的曲线（收益类数字仍可在下表对照）",
        }
    kind, count = kinds.most_common(1)[0]
    plotted_ids = [
        item.history_id for item in items if item.curve is not None and item.curve.kind == kind
    ]
    excluded = [
        {
            "history_id": item.history_id,
            "kind_label": CURVE_KIND_LABELS.get(item.curve.kind, item.curve.kind),
        }
        for item in items
        if item.curve is not None and item.curve.kind != kind
    ]
    kind_label = CURVE_KIND_LABELS.get(kind, kind)
    if count < 2:
        note = f"只有 1 条「{kind_label}」曲线，没有可叠绘的对象——下面仍然画它，但请当作单条看"
    else:
        note = f"叠绘 {len(plotted_ids)} 条「{kind_label}」"
    unit = next(
        (item.curve.unit for item in items if item.curve is not None and item.curve.kind == kind),
        CURVE_UNIT_LABELS.get(kind, ""),
    )
    return {
        "kind": kind,
        "kind_label": kind_label,
        "unit": unit,
        "plotted": count >= 2,
        "history_ids": plotted_ids,
        "excluded": excluded,
        "note": note,
    }


def _warnings(
    items: Sequence[CompareItem],
    curve_group: Mapping[str, object],
) -> tuple[str, ...]:
    warnings: list[str] = []

    excluded = curve_group.get("excluded")
    if isinstance(excluded, list) and excluded:
        labels = sorted(
            {str(entry.get("kind_label")) for entry in excluded if isinstance(entry, dict)}
        )
        plotted_count = curve_group.get("history_ids")
        count = len(plotted_count) if isinstance(plotted_count, list) else 0
        warnings.append(
            f"所选记录里有多种曲线口径，只叠绘了「{curve_group.get('kind_label')}」"
            f"（{count} 条）；"
            f"被排除的口径：{'、'.join(labels)}——它们不是净值，画在一起会读错"
        )
    plotted = curve_group.get("plotted") is True
    if not plotted and curve_group.get("kind"):
        warnings.append(str(curve_group.get("note") or ""))

    types = sorted({item.backtest_type for item in items})
    if len(types) > 1:
        names = "、".join(TYPE_LABELS.get(value, value) for value in types)
        warnings.append(
            f"跨类型对比（{names}）：收益类数字口径不同，只能定性参照，不能直接比大小。"
            "同类型的多次实验才是可比的。"
        )

    ranges = [
        (item.date_range.get("start"), item.date_range.get("end"))
        for item in items
        if isinstance(item.date_range.get("start"), str)
        and isinstance(item.date_range.get("end"), str)
    ]
    if len(ranges) >= 2:
        latest_start = max(str(start) for start, _ in ranges)
        earliest_end = min(str(end) for _, end in ranges)
        if latest_start > earliest_end:
            warnings.append(
                f"日期区间没有重叠（最晚的起点 {latest_start} 晚于最早的终点 {earliest_end}）："
                "叠绘只能看形状，不能比较同期表现"
            )

    days = [
        metric.value
        for item in items
        for metric in item.metrics
        if metric.key == METRIC_SAMPLE_DAYS and metric.value is not None and metric.value > 0
    ]
    if len(days) >= 2:
        longest, shortest = max(days), min(days)
        if shortest > 0 and longest / shortest >= SAMPLE_LENGTH_WARNING_RATIO:
            warnings.append(
                f"样本长度相差 {longest / shortest:.1f} 倍"
                f"（{int(longest)} 天 vs {int(shortest)} 天）："
                "跑得久的那条天然更容易撞上极端回撤，回撤不能直接比大小"
            )
    return tuple(warning for warning in warnings if warning)
