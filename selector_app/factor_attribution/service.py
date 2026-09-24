"""Explain one finished backtest: what separated the winners from the losers.

Nothing is backtested here. The trades already exist in the archived result, and
re-running the strategy to get them again would be a second implementation of
"what did this strategy do" - the one thing this report must not re-litigate.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence

import numpy as np

from selector_app.formulas.registry import FORMULA_REGISTRY
from selector_app.market_data.adapter import BatchMarketDataAdapter
from selector_app.market_data.models import StockRef
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.strategy_discovery.catalog import default_indicator_catalog
from selector_app.swing_research.scoring import block_permutations, effective_block_size
from selector_app.swing_research.values import custom_value_ids, display_name_for

from .metrics import apply_fdr, evaluate_factor
from .models import (
    DEFAULT_SEED,
    MIN_SIGNAL_DAYS,
    MIN_TOTAL_TRADES,
    AttributionFactor,
    FactorAttributionConfig,
)
from .trades import RoundTrip, TradeSample, pair_round_trips
from .values import collect_signal_values

ProgressCallback = Callable[[int, int], None] | None

#: Shown verbatim in the UI. The point of the list is to keep the reader from
#: turning an explanation into a prediction.
BOUNDARIES: tuple[str, ...] = (
    "收益是净口径：pnl 已扣双边佣金、印花税与滑点，不是裸价格收益",
    "比的是入场**信号日**的指标值，不是成交日；信号日与成交日不同的单要留意",
    "胜 / 败是事后标签：这是解释工具，不是预测模型，也不能拿去筛下一批票",
    "期末未平仓的单没有已实现收益，不计入胜负",
    "没有样本外切分：结论只解释这一次回测，换个区间要重新跑",
)

#: History types whose results carry a ``trades`` list. Attribution reads
#: archived trades, so a type without them is not a candidate at all.
TRADABLE_TYPES: tuple[str, ...] = ("portfolio", "condition_research", "single_stock")


class FactorAttributionService:
    """Compare the winning and losing trades of one archived backtest."""

    def __init__(self, adapter: BatchMarketDataAdapter, store: DuckDbMarketDataStore) -> None:
        self._adapter = adapter
        self._store = store

    def run(
        self,
        config: FactorAttributionConfig,
        progress_callback: ProgressCallback = None,
    ) -> dict[str, object]:
        resolved = config.normalized()
        record = self._store.read_backtest_history(resolved.source_history_id)
        if record is None:
            raise ValueError("找不到这条回测记录；每种类型只保留最近 20 条，更早的会被自动淘汰")
        trades = _extract_trades(record)
        sample = pair_round_trips(trades)
        value_ids = _resolve_value_ids(resolved)

        report: dict[str, object] = {
            "source": _source_summary(record),
            "samples": _sample_summary(sample),
            "value_count": len(value_ids),
            "significant_count": 0,
            "permutations": resolved.permutations,
            "block_size_days": resolved.block_size_days,
            "block_size_trades": None,
            "alpha": resolved.alpha,
            "factors": [],
            "notes": [],
            "boundaries": list(BOUNDARIES),
            "reason": None,
        }
        if sample.total < MIN_TOTAL_TRADES:
            report["reason"] = (
                f"可配对的平仓交易只有 {sample.total} 笔，不足 {MIN_TOTAL_TRADES} 笔，不做归因"
                + (
                    "（没有已平仓的成交：未平仓的头寸没有已实现收益，不能算胜单）"
                    if sample.total == 0 and sample.open_positions
                    else ""
                )
            )
            return report

        def report_progress(current: int, total: int) -> None:
            if progress_callback is not None:
                progress_callback(current, total)

        refs = self._resolve_refs(sample.trips)
        signal = collect_signal_values(
            self._adapter,
            refs,
            sample.trips,
            value_ids,
            batch_size=resolved.batch_size,
            progress=report_progress,
        )
        trips = tuple(
            trip.with_holding_days(int(days))
            for trip, days in zip(sample.trips, signal.holding_days)
        )
        wins = np.fromiter((trip.win for trip in trips), dtype=bool, count=len(trips))
        signal_days = sample.signal_days
        span_days = (
            max(trip.signal_date for trip in trips) - min(trip.signal_date for trip in trips)
        ).days + 1
        orders, block_trades = _permutation_orders(
            total=len(trips),
            signal_days=signal_days,
            span_days=span_days,
            permutations=resolved.permutations,
            block_size_days=resolved.block_size_days,
        )
        report["block_size_trades"] = block_trades

        catalog = default_indicator_catalog()
        factors = [
            evaluate_factor(
                np.asarray(signal.values[:, index], dtype=float),
                wins,
                value_id=value_id,
                display_name=display_name_for(value_id, catalog),
                orders=orders,
                min_group_trades=resolved.min_group_trades,
            )
            for index, value_id in enumerate(value_ids)
        ]
        factors = apply_fdr(factors, alpha=resolved.alpha)
        # Verdict first, then strength - and strength is the standardized
        # difference, never the raw p-value: with thirty indicators the smallest
        # raw p-value is always impressive.
        factors.sort(
            key=lambda item: (
                not bool(item.significant),
                -(abs(item.standardized) if item.standardized is not None else 0.0),
            )
        )

        # Holding periods are only known once the bars are in hand, so every
        # summary downstream has to use the filled sample - otherwise the
        # holding-period caveat silently never fires.
        filled = TradeSample(
            trips=trips,
            open_positions=sample.open_positions,
            orphan_sells=sample.orphan_sells,
            rejected=sample.rejected,
            unparsable=sample.unparsable,
        )
        payload = dict(report)
        payload["samples"] = _sample_summary(filled, missing_stocks=signal.missing_stocks)
        payload["factors"] = [item.to_dict() for item in factors]
        payload["significant_count"] = sum(1 for item in factors if item.significant)
        payload["notes"] = _notes(filled, signal.missing_stocks)
        payload["reason"] = _reason(factors, resolved, sample.total, signal_days)
        return payload

    def _resolve_refs(self, trips: Sequence[RoundTrip]) -> list[StockRef]:
        wanted = {(trip.market.lower(), trip.code) for trip in trips}
        return [
            ref
            for ref in self._adapter.list_stock_refs("", "all")
            if (str(ref.market).lower(), ref.code) in wanted
        ]


def _extract_trades(record: Mapping[str, object]) -> list[Mapping[str, object]]:
    result = record.get("result")
    if not isinstance(result, Mapping):
        raise ValueError("这条回测记录没有结果数据，无法归因")
    trades = result.get("trades")
    if not isinstance(trades, list) or not trades:
        raise ValueError("这条回测记录没有成交明细，无法归因")
    return [item for item in trades if isinstance(item, Mapping)]


def _resolve_value_ids(config: FactorAttributionConfig) -> tuple[str, ...]:
    """Same menu as the factor test: dimensionless technicals plus the checked
    custom outputs. Attribution compares groups within one measurement, so a
    value that cannot rank cannot be compared either."""

    catalog = default_indicator_catalog()
    technical = FORMULA_REGISTRY.formula("technical")
    available = {value_id for value_id in catalog.rankable_ids if value_id.startswith("technical.")}
    if config.include_custom:
        available.update(custom_value_ids())
    if config.value_ids:
        requested = tuple(catalog.canonical_id(item) for item in config.value_ids)
        unknown = tuple(item for item in requested if item not in available)
        if unknown:
            raise ValueError(
                f"以下指标不能用于因子归因: {', '.join(unknown)}；"
                "只支持 technical 公式中可排序的无量纲指标，"
                "以及三个自定义公式里已核实的连续输出"
            )
        return tuple(item for item in requested if item in available)
    ordered = tuple(
        f"technical.{name}" for name in technical.value_names if f"technical.{name}" in available
    )
    if config.include_custom:
        ordered += tuple(item for item in custom_value_ids() if item in available)
    return ordered


def _permutation_orders(
    *,
    total: int,
    signal_days: int,
    span_days: int,
    permutations: int,
    block_size_days: int,
) -> tuple[list[np.ndarray], int | None]:
    """Block permutations over trade positions, sized in calendar days.

    The study permutes trades, but the dependence it must respect lives in the
    calendar, so the block is asked for in days and converted with the calendar
    span rather than the count of distinct signal days: entries bunched onto a
    few days do not make those days longer, and dividing by the day count would
    stretch each block until only two or three of them remained. Two or three
    blocks are not a null distribution - with k blocks there are only k!
    arrangements, so the p-value cannot even express 0.05.
    """

    if permutations <= 0 or signal_days < MIN_SIGNAL_DAYS or total < 2:
        return [], None
    block_trades = max(1, math.ceil(block_size_days * total / max(1, span_days)))
    # 块数下限由 block_permutations 统一把关（块太少时它自己把块切短）。这里把切完
    # 之后的块长取回来再报出去——两处各定一个门槛迟早会长出两个不同的数，而报给
    # 用户的必须是真正用上的那个。
    block_trades = effective_block_size(total, block_trades)
    orders = block_permutations(
        total, block_size=block_trades, count=permutations, seed=DEFAULT_SEED
    )
    return orders, block_trades


def _source_summary(record: Mapping[str, object]) -> dict[str, object]:
    request = record.get("request")
    window: dict[str, object] = {"start": None, "end": None}
    if isinstance(request, Mapping):
        for key, target in (("start_date", "start"), ("end_date", "end")):
            value = request.get(key)
            if isinstance(value, str):
                window[target] = value[:10]
    return {
        "history_id": record.get("history_id"),
        "backtest_type": record.get("backtest_type"),
        "finished_at": record.get("finished_at"),
        "date_range": window,
    }


def _sample_summary(
    sample: TradeSample,
    *,
    missing_stocks: Sequence[str] = (),
) -> dict[str, object]:
    holding = [trip.holding_days for trip in sample.trips if trip.holding_days > 0]
    win_holding = [trip.holding_days for trip in sample.trips if trip.win and trip.holding_days > 0]
    loss_holding = [
        trip.holding_days for trip in sample.trips if not trip.win and trip.holding_days > 0
    ]
    return {
        "trades": sample.total,
        "wins": sample.wins,
        "losses": sample.losses,
        "open_positions": sample.open_positions,
        "orphan_sells": sample.orphan_sells,
        "rejected": sample.rejected,
        "unparsable": sample.unparsable,
        "signal_days": sample.signal_days,
        "stocks": sample.stocks,
        "holding_days": round(sum(holding) / len(holding), 2) if holding else None,
        "win_holding_days": round(sum(win_holding) / len(win_holding), 2) if win_holding else None,
        "loss_holding_days": (
            round(sum(loss_holding) / len(loss_holding), 2) if loss_holding else None
        ),
        "missing_stocks": list(missing_stocks),
    }


def _notes(sample: TradeSample, missing_stocks: Sequence[str]) -> list[str]:
    notes: list[str] = []
    if sample.open_positions:
        notes.append(f"期末还有 {sample.open_positions} 笔未平仓，没有已实现收益，不计入胜负")
    if sample.orphan_sells:
        notes.append(f"{sample.orphan_sells} 笔卖出找不到对应的买入，已丢弃")
    if sample.rejected:
        # Only counts trades the archived result already flagged as unfillable.
        # Do not guess a reason here: no engine in the codebase decides on limit
        # up/down, and insufficient cash does not reject either - the buy size is
        # reduced until it fits, and a size of zero records no trade at all.
        notes.append(f"{sample.rejected} 笔委托被标为未成交，未计入")
    if missing_stocks:
        notes.append(
            f"{len(missing_stocks)} 只标的的行情读不到，它们的成交未计入："
            + "、".join(missing_stocks[:5])
            + ("…" if len(missing_stocks) > 5 else "")
        )
    win_days = [trip.holding_days for trip in sample.trips if trip.win and trip.holding_days > 0]
    loss_days = [
        trip.holding_days for trip in sample.trips if not trip.win and trip.holding_days > 0
    ]
    if win_days and loss_days:
        win_mean = sum(win_days) / len(win_days)
        loss_mean = sum(loss_days) / len(loss_days)
        if loss_mean > 0 and abs(win_mean - loss_mean) / loss_mean > 0.3:
            notes.append(
                f"胜单平均持有 {win_mean:.1f} 个交易日、败单 {loss_mean:.1f} 个，"
                "两组持有期差得较多：指标差异可能来自持有长短，而不只是入场信号"
            )
    return notes


def _reason(
    factors: Sequence[AttributionFactor],
    config: FactorAttributionConfig,
    trades: int,
    signal_days: int,
) -> str | None:
    if not factors:
        return "没有任何指标能形成可用样本"
    if config.permutations <= 0:
        return "未做噪声门槛检验，p 值与多重检验校正不可用"
    # A p-value from N permutations cannot go below 1/(N+1). Left unsaid, a
    # reader who tightens alpha sees "nothing is significant" and concludes the
    # indicators are useless, when the test simply could not reach that bar.
    floor = 1.0 / (1 + config.permutations)
    if config.alpha < floor:
        return (
            f"噪声门槛检验只做了 {config.permutations} 次，"
            f"最小可能的 p 值是 {floor:.3f}，比显著性水平 {config.alpha} 还高，"
            "因此不可能有指标被判为显著；请提高检验次数或放宽显著性水平"
        )
    if signal_days < MIN_SIGNAL_DAYS:
        return (
            f"入场信号日只有 {signal_days} 天，样本太集中，"
            "无法估计噪声门槛；下面的差值只作描述，不判断显著性"
        )
    winners = [item for item in factors if item.significant]
    if not winners:
        return f"{trades} 笔平仓交易、{len(factors)} 个指标里，没有一个能在噪声门槛之上区分胜负"
    best = winners[0]
    direction = "高于" if (best.difference or 0.0) > 0 else "低于"
    gap = abs(best.difference or 0.0)
    return (
        f"{len(factors)} 个指标里有 {len(winners)} 个通过多重检验校正；"
        f"区分最强的是「{best.display_name}」：胜组均值 {direction}败组约 {gap:.4g}"
        f"（p={best.p_value:.3f}）"
    )


__all__ = ["BOUNDARIES", "TRADABLE_TYPES", "FactorAttributionService"]
