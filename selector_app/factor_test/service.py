"""Run one factor test over a universe."""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd

from selector_app.formulas.registry import FORMULA_REGISTRY
from selector_app.market_data.adapter import BatchMarketDataAdapter
from selector_app.market_data.models import StockRef
from selector_app.strategy_discovery.catalog import default_indicator_catalog
from selector_app.swing_research.values import custom_value_ids, display_name_for

from .metrics import FactorEvidence, annotate_significance, evaluate_factor
from .models import FactorTestConfig
from .panel import build_panel, check_panel_budget, estimate_panel_bytes, scan_calendar

ProgressCallback = Callable[[int, int], None] | None
_FACTOR_FORMULA_IDS = ("technical", "main_force_absorption")

#: Shown verbatim in the UI. A factor test measures whether a value ranks
#: stocks, not whether the ranking can be traded, and a reader who misses that
#: will read "annualised spread" as money they can withdraw.
BOUNDARIES: tuple[str, ...] = (
    "标签是收盘价到收盘价的前瞻收益，不含交易成本、滑点、停牌、涨跌停与流动性约束",
    "最后若干个交易日没有未来收益可用，这些天自动退出横截面，不是记成 0",
    "没有样本外切分：方向（买高还是买低）是看全样本的符号定的，"
    "前后半段一致只说明不是某一段独撑，不代表样本外能复现",
    "多空曲线是每日价差的累加而非复利：持仓期重叠，复合出一个净值会凭空放大",
)


class FactorTestService:
    """Score indicators against the forward return of every stock on every day."""

    def __init__(self, adapter: BatchMarketDataAdapter) -> None:
        self._adapter = adapter

    def run(
        self,
        config: FactorTestConfig,
        progress_callback: ProgressCallback = None,
    ) -> dict[str, object]:
        resolved = config.normalized()
        value_ids = _resolve_value_ids(resolved)
        refs = self._list_refs(resolved)
        catalog = default_indicator_catalog()

        def report(current: int, total: int) -> None:
            if progress_callback is not None:
                progress_callback(current, total)

        dates, rows = scan_calendar(self._adapter, refs, resolved, report)
        baseline = {
            "universe": resolved.universe,
            "stock_count": len(refs),
            "scanned_stocks": 0,
            "value_count": len(value_ids),
            "horizon": resolved.horizon,
            "min_cross_section": resolved.min_cross_section,
            "permutations": resolved.permutations,
            "block_size": resolved.block_size,
            "alpha": resolved.alpha,
            "quantile_count": resolved.quantile_count,
            "date_range": {"start": None, "end": None, "days": int(dates.size)},
            "dates": [],
            "factors": [],
            "significant_count": 0,
            "reason": None,
            "boundaries": list(BOUNDARIES),
        }
        if dates.size == 0 or not rows:
            baseline["reason"] = "所选范围内没有任何可用日线"
            return baseline

        check_panel_budget(estimate_panel_bytes(len(rows), int(dates.size), len(value_ids)))
        panel, filled = build_panel(self._adapter, refs, resolved, value_ids, dates, rows, report)
        evidence: list[FactorEvidence] = [
            evaluate_factor(
                np.asarray(panel.values[index], dtype=float),
                panel.labels,
                value_id=value_id,
                display_name=display_name_for(value_id, catalog),
                config=resolved,
            )
            for index, value_id in enumerate(value_ids)
        ]
        evidence = annotate_significance(evidence, alpha=resolved.alpha)
        # Ordered by strength, but strength alone never decides: the note says
        # how many days it rests on and whether the halves agree.
        evidence.sort(
            key=lambda item: (
                not item.significant,
                -(item.ic_mean if np.isfinite(item.ic_mean) else 0.0),
            )
        )
        date_texts = [pd.Timestamp(item).strftime("%Y-%m-%d") for item in dates]
        payload = dict(baseline)
        payload["scanned_stocks"] = filled
        payload["date_range"] = {
            "start": date_texts[0] if date_texts else None,
            "end": date_texts[-1] if date_texts else None,
            "days": int(dates.size),
        }
        payload["dates"] = date_texts
        payload["factors"] = [item.to_dict() for item in evidence]
        payload["significant_count"] = sum(1 for item in evidence if item.significant)
        payload["reason"] = _reason(evidence, resolved)
        return payload

    def _list_refs(self, config: FactorTestConfig) -> list[StockRef]:
        refs = self._adapter.list_stock_refs(
            config.vipdoc_path,
            config.universe,
            instrument_types=config.instrument_types or None,
            boards=config.boards or None,
        )
        if config.limit_stocks is not None:
            refs = refs[: config.limit_stocks]
        return refs


def _resolve_value_ids(config: FactorTestConfig) -> tuple[str, ...]:
    """Rankable technical/measurement outputs plus the allow-listed custom ones.

    The custom formulas (主力清洗 / 庄家顶底 / 准备拉升) are trading formulas
    rather than measurement libraries, so only their continuous, dimensionless
    outputs are offered - see :mod:`selector_app.swing_research.values` for the
    measurement that picked them. Everything else about them is either a
    constant, a two-valued pulse, or the price itself, and ranking by the price
    simply re-discovers that some stocks cost more than others.
    """

    catalog = default_indicator_catalog()
    available = {
        value_id
        for value_id in catalog.rankable_ids
        if value_id.partition(".")[0] in _FACTOR_FORMULA_IDS
    }
    if config.include_custom:
        available.update(custom_value_ids())
    if config.value_ids:
        requested = tuple(catalog.canonical_id(item) for item in config.value_ids)
        unknown = tuple(item for item in requested if item not in available)
        if unknown:
            raise ValueError(
                f"以下指标不能用于因子检验: {', '.join(unknown)}；"
                "只支持技术指标、主力吸筹指标中可排序的无量纲输出，"
                "以及三个自定义公式里已核实的连续输出"
            )
        return tuple(item for item in requested if item in available)
    ordered = tuple(
        f"{formula_id}.{name}"
        for formula_id in _FACTOR_FORMULA_IDS
        for name in FORMULA_REGISTRY.formula(formula_id).value_names
        if f"{formula_id}.{name}" in available
    )
    if config.include_custom:
        ordered += tuple(item for item in custom_value_ids() if item in available)
    return ordered


def _reason(evidence: list[FactorEvidence], config: FactorTestConfig) -> str | None:
    if not evidence:
        return "没有任何指标能形成有效的横截面样本"
    if config.permutations <= 0:
        return "未做噪声门槛检验，p 值与多重检验校正不可用"
    # A p-value from N permutations cannot go below 1/(N+1). Left unsaid, a
    # reader who tightens alpha to 0.01 sees "nothing is significant" and
    # concludes the indicators are junk, when the real answer is that the test
    # was never able to reach that bar.
    floor = 1.0 / (1 + config.permutations)
    if config.alpha < floor:
        return (
            f"噪声门槛检验只做了 {config.permutations} 次，"
            f"最小可能的 p 值是 {floor:.3f}，比显著性水平 {config.alpha} 还高，"
            "因此不可能有指标被判为显著；请提高检验次数或放宽显著性水平"
        )
    return None


__all__ = ["BOUNDARIES", "FactorTestService"]
