"""Project one stored research result into comparable curves and numbers.

每一种研究类型产出的东西本来就不是一回事：组合回测出资金曲线，因子检验出 IC
序列，条件研究出一堆候选条件。这里做的事情是**把它们各自能拿出来的那点东西
如实抽出来**，并且在拿不出来的时候给出一个说得清的原因——而不是拿一个默认值
填进去让界面看起来很整齐。
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence

from .models import (
    CURVE_KIND_EQUITY,
    CURVE_KIND_FACTOR_IC,
    CURVE_KIND_FACTOR_LONG_SHORT,
    CURVE_KIND_LABELS,
    CURVE_UNIT_LABELS,
    METRIC_ANNUAL_RETURN,
    METRIC_INSTRUMENT_COUNT,
    METRIC_LABELS,
    METRIC_MAX_DRAWDOWN,
    METRIC_ORDER,
    METRIC_SAMPLE_DAYS,
    METRIC_SHARPE,
    METRIC_SIGNIFICANT_COUNT,
    METRIC_TOTAL_RETURN,
    METRIC_TRADE_COUNT,
    METRIC_UNITS,
    METRIC_WIN_RATE,
    TYPE_LABELS,
    UNIT_COUNT,
    UNIT_PERCENT,
    UNIT_RATIO,
    UNIT_TEXT,
    CompareCurve,
    CompareMetric,
)

SOURCE_EQUITY = "组合资金曲线"
SOURCE_SINGLE = "单票资金曲线"
SOURCE_FITNESS = "逐标的 test 段中位数"
SOURCE_CONDITION = "最强候选 · 留出期合并交易"
SOURCE_SWING = "波段研究 IC 口径"
SOURCE_FACTOR = "因子检验 IC 口径"

#: 收益类指标对「不产生资金曲线」的类型统一给这句原因。别用 0 或空串糊过去：
#: 表格里一个「—」加上原因，比一个看起来能比大小的假数字诚实得多。
_NO_EQUITY_NOTE = "{type}不产生资金曲线，收益类数字不能与组合回测并列"


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _rows(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _median(values: Sequence[float]) -> float | None:
    usable = [value for value in values if value is not None]
    if not usable:
        return None
    return float(statistics.median(usable))


def _median_field(items: Sequence[Mapping[str, object]], key: str) -> float | None:
    """Median of one field, skipping missing values instead of treating them as 0.

    把缺失当 0 会把中位数直接拉到 0 附近——一半标的没跑出结果时，「中位收益 0%」
    看起来像个结论，其实什么都没说。
    """

    return _median(
        [value for value in (_number(item.get(key)) for item in items) if value is not None]
    )


def _metric(
    key: str,
    value: float | None,
    *,
    source: str,
    label: str | None = None,
    unit: str | None = None,
    note: str | None = None,
    text: str | None = None,
) -> CompareMetric:
    return CompareMetric(
        key=key,
        label=label or METRIC_LABELS.get(key, key),
        value=value,
        unit=unit or METRIC_UNITS.get(key, UNIT_RATIO),
        source=source,
        text=text,
        note=note,
    )


def _align(
    values: Mapping[str, float | None],
    *,
    source: str,
    notes: Mapping[str, str],
) -> tuple[CompareMetric, ...]:
    """Fill every shared row, using an explicit note wherever a value is absent."""

    return tuple(
        _metric(key, values.get(key), source=source, note=notes.get(key)) for key in METRIC_ORDER
    )


# ---------------------------------------------------------------------------
# 曲线
# ---------------------------------------------------------------------------


def extract_curve(
    backtest_type: str,
    result: Mapping[str, object],
    *,
    normalize: str,
) -> tuple[CompareCurve | None, str | None]:
    """Return the series to draw, or ``(None, reason)`` when there is none."""

    if backtest_type in {"portfolio", "single_stock"}:
        return _equity_curve(result, normalize=normalize)
    if backtest_type == "factor_test":
        return _factor_curve(result)
    return None, f"{TYPE_LABELS.get(backtest_type, backtest_type)}不产生任何曲线"


def _equity_curve(
    result: Mapping[str, object],
    *,
    normalize: str,
) -> tuple[CompareCurve | None, str | None]:
    rows = _rows(result.get("equity_curve"))
    if len(rows) < 2:
        return None, "该记录没有资金曲线（可用点数少于 2）"
    points: list[tuple[str, float]] = []
    for row in rows:
        date = _text(row.get("date"))
        value = _number(row.get("total"))
        if date is None or value is None:
            continue
        points.append((date, value))
    if len(points) < 2:
        return None, "资金曲线里没有可用的净值点"
    base, used, note = _equity_base(result, points[0][1], normalize=normalize)
    if base is None:
        return None, note or "无法确定归一化基准"
    series = tuple(
        {"date": date, "index": index, "value": value / base}
        for index, (date, value) in enumerate(points)
    )
    return (
        CompareCurve(
            kind=CURVE_KIND_EQUITY,
            kind_label=CURVE_KIND_LABELS[CURVE_KIND_EQUITY],
            unit=CURVE_UNIT_LABELS[CURVE_KIND_EQUITY],
            label="累计收益率",
            normalize=used,
            points=series,
            note=note,
        ),
        None,
    )


def _equity_base(
    result: Mapping[str, object],
    first_value: float,
    *,
    normalize: str,
) -> tuple[float | None, str, str | None]:
    """Pick the divisor and say which one was used.

    默认除以**本金**：两次实验本金不同时，收益率口径才是可比的。只有取不到本金
    才退回除以首值，并且必须写明——首值归一会把「起点不是本金」的情况藏起来。
    """

    performance = _mapping(result.get("performance"))
    capital = _number(performance.get("start_cash"))
    if normalize == "capital":
        if capital is not None and capital > 0:
            return capital, "capital", None
        if abs(first_value) > 1e-12:
            return (
                first_value,
                "first",
                "记录里没有本金，退回按首值归一（曲线起点强制为 0%）",
            )
        return None, "capital", "既没有本金也没有非零首值，无法归一化"
    if abs(first_value) > 1e-12:
        return first_value, "first", None
    if capital is not None and capital > 0:
        return capital, "capital", "首值为 0，退回按本金归一"
    return None, "first", "既没有非零首值也没有本金，无法归一化"


def _factor_curve(result: Mapping[str, object]) -> tuple[CompareCurve | None, str | None]:
    raw_dates = result.get("dates")
    date_items = raw_dates if isinstance(raw_dates, (list, tuple)) else ()
    dates: list[str] = []
    for item in date_items:
        text = _text(item)
        if text is not None:
            dates.append(text)
    factors = _rows(result.get("factors"))
    if not factors or not dates:
        return None, "该因子检验没有可用的日期或指标"
    chosen: Mapping[str, object] | None = None
    best_score = 0.0
    for item in factors:
        ic_mean = _number(item.get("ic_mean"))
        score = abs(ic_mean) if ic_mean is not None else 0.0
        if item.get("significant") is True:
            score += 10.0
        if chosen is None or score > best_score:
            chosen, best_score = item, score
    if chosen is None:
        return None, "该因子检验没有可用指标"
    name = _text(chosen.get("display_name")) or _text(chosen.get("value_id")) or "未命名指标"
    long_short = _mapping(chosen.get("long_short"))
    cumulative = long_short.get("cumulative")
    raw: Sequence[object]
    if isinstance(cumulative, (list, tuple)) and len(cumulative) >= 2:
        kind = CURVE_KIND_FACTOR_LONG_SHORT
        raw = cumulative
        note = "多空累计价差不是资金曲线，未做归一化"
    else:
        series = chosen.get("ic_cumulative")
        if not isinstance(series, (list, tuple)) or len(series) < 2:
            return None, f"{name}没有可用的累计序列"
        kind = CURVE_KIND_FACTOR_IC
        raw = series
        note = "累计 IC 不是资金曲线，未做归一化"
    points: list[dict[str, object]] = []
    for index, value in enumerate(raw):
        number = _number(value)
        if number is None or index >= len(dates):
            continue
        points.append({"date": dates[index], "index": index, "value": number})
    if len(points) < 2:
        return None, f"{name}的累计序列可用点数少于 2"
    return (
        CompareCurve(
            kind=kind,
            kind_label=CURVE_KIND_LABELS[kind],
            unit=CURVE_UNIT_LABELS[kind],
            label=name,
            normalize="none",
            points=tuple(points),
            note=note,
        ),
        None,
    )


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------


def extract_metrics(
    backtest_type: str,
    result: Mapping[str, object],
) -> tuple[tuple[CompareMetric, ...], tuple[CompareMetric, ...]]:
    """Return ``(shared rows, type-specific rows)`` for one record."""

    handlers = {
        "portfolio": _portfolio_metrics,
        "single_stock": _single_stock_metrics,
        "strategy_fitness": _fitness_metrics,
        "condition_research": _condition_metrics,
        "swing_research": _swing_metrics,
        "individual_research": _individual_research_metrics,
        "factor_test": _factor_metrics,
    }
    handler = handlers.get(backtest_type)
    if handler is None:
        return (
            _align({}, source="未知类型", notes={key: "不支持的历史类型" for key in METRIC_ORDER}),
            (),
        )
    values, notes, native = handler(result)
    return _align(values, source=notes["_source"], notes=notes), tuple(native)


def _no_equity_notes(backtest_type: str, source: str) -> dict[str, str]:
    reason = _NO_EQUITY_NOTE.format(type=TYPE_LABELS.get(backtest_type, backtest_type))
    notes: dict[str, str] = {"_source": source}
    for key in (
        METRIC_TOTAL_RETURN,
        METRIC_ANNUAL_RETURN,
        METRIC_MAX_DRAWDOWN,
        METRIC_SHARPE,
        METRIC_WIN_RATE,
        METRIC_TRADE_COUNT,
    ):
        notes[key] = reason
    return notes


def _portfolio_metrics(
    result: Mapping[str, object],
) -> tuple[dict[str, float | None], dict[str, str], list[CompareMetric]]:
    performance = _mapping(result.get("performance"))
    values = {
        METRIC_TOTAL_RETURN: _number(performance.get("total_return")),
        METRIC_ANNUAL_RETURN: _number(performance.get("annual_return")),
        METRIC_MAX_DRAWDOWN: _number(performance.get("max_drawdown")),
        METRIC_SHARPE: _number(performance.get("sharpe")),
        METRIC_WIN_RATE: _number(performance.get("win_rate")),
        METRIC_TRADE_COUNT: _number(performance.get("total_trades")),
        METRIC_SAMPLE_DAYS: _number(result.get("bars")),
        METRIC_INSTRUMENT_COUNT: _number(result.get("processed")),
        METRIC_SIGNIFICANT_COUNT: None,
    }
    notes = {"_source": SOURCE_EQUITY, METRIC_SIGNIFICANT_COUNT: "组合回测不做显著性检验"}
    native: list[CompareMetric] = []
    benchmark = _mapping(result.get("benchmark"))
    benchmark_performance = _mapping(benchmark.get("performance"))
    # 基准对照是后加的，老记录里根本没有这个块。给出两行恒为「—」的基准指标只是
    # 噪音——没有就别占位置，有的话才摆出来。
    if benchmark:
        native.append(
            _metric(
                "benchmark_return",
                _number(benchmark_performance.get("total_return")),
                source="等权买入持有基准",
                label="基准总收益",
                unit=UNIT_PERCENT,
            )
        )
        native.append(
            _metric(
                "benchmark_drawdown",
                _number(benchmark_performance.get("max_drawdown")),
                source="等权买入持有基准",
                label="基准最大回撤",
                unit=UNIT_PERCENT,
            )
        )
    native.append(
        _metric(
            "end_value",
            _number(performance.get("end_value")),
            source=SOURCE_EQUITY,
            label="期末权益",
            unit=UNIT_COUNT,
        )
    )
    native.append(
        _metric(
            "turnover",
            _number(performance.get("turnover")),
            source=SOURCE_EQUITY,
            label="换手率（成交额/本金）",
            unit=UNIT_RATIO,
        )
    )
    native.append(
        _metric(
            "max_dd_duration",
            _number(performance.get("max_dd_duration")),
            source=SOURCE_EQUITY,
            label="最长回撤天数",
            unit=UNIT_COUNT,
        )
    )
    return values, notes, native


def _single_stock_metrics(
    result: Mapping[str, object],
) -> tuple[dict[str, float | None], dict[str, str], list[CompareMetric]]:
    values, notes, native = _portfolio_metrics(result)
    notes = dict(notes)
    notes["_source"] = SOURCE_SINGLE
    values = dict(values)
    values[METRIC_INSTRUMENT_COUNT] = 1.0 if _text(result.get("code")) else None
    for index, metric in enumerate(native):
        if metric.key in {"benchmark_return", "benchmark_drawdown"}:
            native[index] = CompareMetric(
                key=metric.key,
                label=metric.label,
                value=None,
                unit=metric.unit,
                source=metric.source,
                note="单股回测没有基准对照",
            )
    return values, notes, native


def _individual_research_metrics(
    result: Mapping[str, object],
) -> tuple[dict[str, float | None], dict[str, str], list[CompareMetric]]:
    """Compare the best validation candidate while keeping its source explicit."""

    candidates = _rows(result.get("results_by_sharpe"))
    metrics = _mapping(candidates[0].get("metrics")) if candidates else {}
    validation = _mapping(metrics.get("validation"))
    proxy = {
        "code": result.get("code"),
        "name": result.get("name"),
        "bars": _mapping(result.get("date_range")).get("days"),
        "performance": dict(validation),
    }
    values, notes, native = _single_stock_metrics(proxy)
    notes = dict(notes)
    notes["_source"] = "个股研究 · 夏普榜验证段最佳候选"
    notes[METRIC_SIGNIFICANT_COUNT] = "个股研究不做横截面显著性检验"
    return values, notes, native


def _fitness_metrics(
    result: Mapping[str, object],
) -> tuple[dict[str, float | None], dict[str, str], list[CompareMetric]]:
    results = _rows(result.get("results"))
    tests = [_mapping(_mapping(item).get("test")) for item in results]
    passed = sum(1 for item in results if item.get("passed") is True)
    values = {
        METRIC_TOTAL_RETURN: _median_field(tests, "total_return"),
        METRIC_ANNUAL_RETURN: _median_field(tests, "annual_return"),
        METRIC_MAX_DRAWDOWN: _median_field(tests, "max_drawdown"),
        METRIC_SHARPE: _median_field(tests, "sharpe"),
        METRIC_WIN_RATE: _median_field(tests, "win_rate"),
        METRIC_TRADE_COUNT: _median_field(tests, "total_trades"),
        METRIC_SAMPLE_DAYS: _number(result.get("bars")),
        METRIC_INSTRUMENT_COUNT: float(len(results)),
        METRIC_SIGNIFICANT_COUNT: float(passed),
    }
    notes = {
        "_source": SOURCE_FITNESS,
        METRIC_SIGNIFICANT_COUNT: "通过适配性检查的标的数",
    }
    native: list[CompareMetric] = []
    native.append(
        _metric(
            "pass_rate",
            (passed / len(results)) if results else None,
            source=SOURCE_FITNESS,
            label="适配通过率",
            unit=UNIT_PERCENT,
        )
    )
    for phase, label in (("train", "训练段中位收益"), ("validation", "验证段中位收益")):
        native.append(
            _metric(
                f"{phase}_median_return",
                _median_field(
                    [_mapping(item.get(phase)) for item in results],
                    "total_return",
                ),
                source=f"逐标的 {phase} 段中位数",
                label=label,
                unit=UNIT_PERCENT,
            )
        )
    native.append(
        _metric(
            "candidate_count",
            _number(result.get("total_candidates")),
            source=SOURCE_FITNESS,
            label="候选标的数",
            unit=UNIT_COUNT,
        )
    )
    return values, notes, native


def _condition_metrics(
    result: Mapping[str, object],
) -> tuple[dict[str, float | None], dict[str, str], list[CompareMetric]]:
    candidates = _rows(result.get("candidates"))
    qualified = [item for item in candidates if item.get("passed_constraints") is True]
    best: Mapping[str, object] | None = None
    best_lift = 0.0
    for item in qualified or candidates:
        lift = _number(_mapping(item.get("mining")).get("lift"))
        if lift is None:
            continue
        if best is None or lift > best_lift:
            best, best_lift = item, lift
    notes = _no_equity_notes("condition_research", SOURCE_CONDITION)
    notes[METRIC_SAMPLE_DAYS] = "条件研究未记录交易日数"
    values: dict[str, float | None] = {
        METRIC_INSTRUMENT_COUNT: _number(result.get("processed")),
        METRIC_SIGNIFICANT_COUNT: float(
            sum(1 for item in candidates if _mapping(item.get("mining")).get("significant") is True)
        ),
        METRIC_SAMPLE_DAYS: None,
    }
    native: list[CompareMetric] = [
        _metric(
            "candidate_count",
            float(len(candidates)),
            source="条件研究",
            label="候选条件数",
            unit=UNIT_COUNT,
        ),
        _metric(
            "qualified_count",
            float(len(qualified)),
            source="条件研究",
            label="合格条件数",
            unit=UNIT_COUNT,
        ),
    ]
    if best is None:
        return values, notes, native
    mining = _mapping(best.get("mining"))
    # 一条都没过约束时，下面这些数字取自「全部候选」而不是「合格候选」。不写明的话
    # 「合格 0 条」配一个「最强提升 1.8 倍」会被读成"有 1.8 倍的合格条件"。
    best_note = (
        "没有候选通过约束，最强条件取自全部候选（未经验证段的约束）" if not qualified else None
    )
    native.append(
        _metric(
            "top_lift",
            _number(mining.get("lift")),
            source="挖掘口径：命中率 / 基线命中率",
            label="最强提升倍数",
            unit=UNIT_RATIO,
            note=best_note,
        )
    )
    native.append(
        _metric(
            "baseline_rate",
            _number(mining.get("baseline_rate")),
            source="挖掘口径",
            label="基线命中率",
            unit=UNIT_PERCENT,
        )
    )
    condition = _mapping(best.get("condition"))
    native.append(
        _metric(
            "top_condition",
            None,
            source="条件研究",
            label="最强条件",
            unit=UNIT_TEXT,
            text=_condition_text(condition),
        )
    )
    segment = _mapping(best.get("holdout")) or _mapping(best.get("validation"))
    segment_name = "留出期" if isinstance(best.get("holdout"), Mapping) else "验证段"
    pooled = _mapping(segment.get("pooled_trades"))
    values[METRIC_WIN_RATE] = _number(pooled.get("win_rate"))
    values[METRIC_TRADE_COUNT] = _number(pooled.get("closed_trades"))
    per_stock = _mapping(segment.get("per_stock"))
    native.append(
        _metric(
            "median_total_return",
            _number(per_stock.get("median_total_return")),
            source=f"最强候选 · {segment_name}逐标的全仓回测中位数",
            label="逐标的中位收益",
            unit=UNIT_PERCENT,
            note="这是逐标的全仓回测的中位数，不是组合资金曲线收益",
        )
    )
    return values, notes, native


def _condition_text(condition: Mapping[str, object]) -> str:
    left = _text(condition.get("left_value")) or "?"
    operator = _text(condition.get("operator")) or "?"
    right = _text(condition.get("right_value")) or ""
    constant = _number(condition.get("right_constant"))
    if right:
        return f"{left} {operator} {right}"
    if constant is not None:
        return f"{left} {operator} {constant:g}"
    return f"{left} {operator} ?"


def _swing_metrics(
    result: Mapping[str, object],
) -> tuple[dict[str, float | None], dict[str, str], list[CompareMetric]]:
    scores = _rows(result.get("scores"))
    date_range = _mapping(result.get("date_range"))
    notes = _no_equity_notes("swing_research", SOURCE_SWING)
    values: dict[str, float | None] = {
        METRIC_INSTRUMENT_COUNT: _number(result.get("scanned_stocks")),
        METRIC_SIGNIFICANT_COUNT: _number(result.get("significant_count")),
        METRIC_SAMPLE_DAYS: _number(date_range.get("days")),
    }
    native: list[CompareMetric] = []
    thresholds: list[float] = []
    for item in scores:
        value = _number(item.get("ic_threshold"))
        if value is not None:
            thresholds.append(value)
    native.append(
        _metric(
            "noise_floor",
            max(thresholds) if thresholds else None,
            source="置换检验噪声门槛",
            label="噪声门槛（|IC|）",
            unit=UNIT_RATIO,
        )
    )
    best: Mapping[str, object] | None = None
    best_ic = 0.0
    for item in scores:
        ic_mean = _number(item.get("ic_mean"))
        if ic_mean is None:
            continue
        if best is None or abs(ic_mean) > abs(best_ic):
            best, best_ic = item, ic_mean
    if best is not None:
        native.append(
            _metric(
                "top_indicator",
                None,
                source=SOURCE_SWING,
                label="最强指标",
                unit=UNIT_TEXT,
                text=_text(best.get("display_name")) or _text(best.get("value_id")),
            )
        )
        native.append(
            _metric(
                "top_ic",
                best_ic,
                source=SOURCE_SWING,
                label="最强 IC 均值",
                unit=UNIT_RATIO,
            )
        )
        native.append(
            _metric(
                "top_icir",
                _number(best.get("icir")),
                source=SOURCE_SWING,
                label="最强 ICIR",
                unit=UNIT_RATIO,
            )
        )
    return values, notes, native


def _factor_metrics(
    result: Mapping[str, object],
) -> tuple[dict[str, float | None], dict[str, str], list[CompareMetric]]:
    factors = _rows(result.get("factors"))
    date_range = _mapping(result.get("date_range"))
    notes = _no_equity_notes("factor_test", SOURCE_FACTOR)
    values: dict[str, float | None] = {
        METRIC_INSTRUMENT_COUNT: _number(result.get("scanned_stocks")),
        METRIC_SIGNIFICANT_COUNT: _number(result.get("significant_count")),
        METRIC_SAMPLE_DAYS: _number(date_range.get("days")),
    }
    native: list[CompareMetric] = []
    if not factors:
        return values, notes, native
    best = max(
        factors,
        key=lambda item: abs(_number(item.get("ic_mean")) or 0.0),
    )
    native.append(
        _metric(
            "top_factor",
            None,
            source=SOURCE_FACTOR,
            label="最强因子",
            unit=UNIT_TEXT,
            text=_text(best.get("display_name")) or _text(best.get("value_id")),
        )
    )
    native.append(
        _metric(
            "top_ic",
            _number(best.get("ic_mean")),
            source=SOURCE_FACTOR,
            label="最强 IC 均值",
            unit=UNIT_RATIO,
        )
    )
    long_short = _mapping(best.get("long_short"))
    native.append(
        _metric(
            "spread_mean",
            _number(long_short.get("mean_spread")),
            source="最强因子 · 多空价差",
            label="多空价差均值",
            unit=UNIT_PERCENT,
        )
    )
    native.append(
        _metric(
            "spread_win_rate",
            _number(long_short.get("win_rate")),
            source="最强因子 · 多空价差",
            label="价差为正天数占比",
            unit=UNIT_PERCENT,
        )
    )
    return values, notes, native


# ---------------------------------------------------------------------------
# 标签与区间
# ---------------------------------------------------------------------------


def extract_date_range(result: Mapping[str, object]) -> dict[str, object]:
    start = _text(result.get("start_date"))
    end = _text(result.get("end_date"))
    date_range = _mapping(result.get("date_range"))
    if start is None:
        start = _text(date_range.get("start"))
    if end is None:
        end = _text(date_range.get("end"))
    days = _number(result.get("bars"))
    if days is None:
        days = _number(date_range.get("days"))
    return {"start": start, "end": end, "days": int(days) if days is not None else None}


def extract_label(
    backtest_type: str,
    result: Mapping[str, object],
) -> tuple[str, str]:
    """Return ``(title, subtitle)`` for one record."""

    type_label = TYPE_LABELS.get(backtest_type, backtest_type)
    date_range = extract_date_range(result)
    start = date_range.get("start")
    end = date_range.get("end")
    if isinstance(start, str) and isinstance(end, str):
        span = f"{start} → {end}"
    else:
        span = "区间未记录"
    title = f"{type_label} · {span}"
    subtitle = _subtitle(backtest_type, result)
    return title, subtitle


def _subtitle(backtest_type: str, result: Mapping[str, object]) -> str:
    universe = _text(result.get("universe"))
    if backtest_type == "single_stock":
        name = _text(result.get("name"))
        code = _text(result.get("code"))
        if name and code:
            return f"{name}（{code}）"
        return name or code or "单只标的"
    if backtest_type == "individual_research":
        name = _text(result.get("name"))
        code = _text(result.get("code"))
        candidate_count = _number(result.get("candidate_count")) or 0
        symbol = f"{name}（{code}）" if name and code else name or code or "单只股票"
        return f"{symbol} · {int(candidate_count)} 个候选"
    if backtest_type == "strategy_fitness":
        results = _rows(result.get("results"))
        passed = sum(1 for item in results if item.get("passed") is True)
        prefix = universe.upper() if universe else "候选池"
        return f"{prefix} · {passed}/{len(results)} 通过"
    if backtest_type == "condition_research":
        candidates = _rows(result.get("candidates"))
        prefix = universe.upper() if universe else "候选池"
        return f"{prefix} · {len(candidates)} 条候选"
    scanned = _number(result.get("scanned_stocks")) or _number(result.get("processed"))
    prefix = universe.upper() if universe else "候选池"
    if scanned is not None:
        return f"{prefix} · 扫描 {int(scanned)} 只"
    return prefix
