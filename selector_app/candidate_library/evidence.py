"""Evidence snapshots: what a candidate looked like the moment it was stored.

The snapshot is a copy, not a reference. ``backtest_history`` keeps only the
newest 20 records, so a candidate that pointed back at its study would lose its
numbers the moment the 21st study finished - and the numbers are the only reason
to keep the candidate at all.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from selector_app.candidate_library.models import as_mapping
from selector_app.instrument_lab.conditions import condition_from_mapping, condition_label


def build_label(condition: Mapping[str, object]) -> str:
    """Human-readable one-liner for a stored condition."""

    try:
        return condition_label(condition_from_mapping(condition))
    except ValueError:
        # 存进来的条件结构坏了也要能列出来，至少让用户看见它、然后删掉。
        left = condition.get("left_value")
        operator = condition.get("operator")
        return f"{left or '?'} {operator or '?'}"


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _integer(value: object) -> int | None:
    number = _number(value)
    return None if number is None else int(number)


def _flag(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _pooled(summary: Mapping[str, object]) -> Mapping[str, object]:
    value = summary.get("pooled_trades")
    return value if isinstance(value, Mapping) else {}


def _phase(item: Mapping[str, object], key: str) -> dict[str, object] | None:
    """One backtest phase (validation / holdout) reduced to comparable numbers."""

    summary = as_mapping(item.get(key))
    if not summary:
        return None
    pooled = _pooled(summary)
    per_stock = as_mapping(summary.get("per_stock"))
    payload: dict[str, object] = {
        "trade_count": _integer(pooled.get("trade_count")),
        "win_rate": _number(pooled.get("win_rate")),
        "improved_win_rate": _flag(summary.get("improved_win_rate")),
        "improved_stock_ratio": _number(per_stock.get("improved_stock_ratio")),
    }
    return {key: value for key, value in payload.items() if value is not None} or None


def build_evidence(
    item: Mapping[str, object],
    *,
    request: Mapping[str, object],
    result: Mapping[str, object],
    created_at: str,
) -> dict[str, object]:
    """Collect the numbers a reader needs to judge one stored candidate."""

    mining = as_mapping(item.get("mining"))
    sensitivity = as_mapping(item.get("sensitivity"))
    consistency = as_mapping(item.get("consistency"))
    validation = _phase(item, "validation")
    holdout = _phase(item, "holdout")
    failures = item.get("constraint_failures")
    evidence: dict[str, object] = {
        # 挖掘口径：提升倍数与显著性。三个置换字段同时为 None 表示这次研究没做
        # 检验，界面就不显示显著性这一列，而不是显示成"未通过"。
        "lift": _number(mining.get("lift")),
        "support_count": _integer(mining.get("support_count")),
        "instrument_count": _integer(mining.get("instrument_count")),
        "outcome_rate": _number(mining.get("outcome_rate")),
        "baseline_rate": _number(mining.get("baseline_rate")),
        "support_ratio": _number(mining.get("support_ratio")),
        "permutation_pvalue": _number(mining.get("permutation_pvalue")),
        "significant": _flag(mining.get("significant")),
        "lift_threshold": _number(mining.get("lift_threshold")),
        # 槽位限制下的复核：只在那天命中数多于槽位数时才有意义。
        "top_n_outcome_rate": _number(mining.get("top_n_outcome_rate")),
        "top_n_filtered_days": _integer(mining.get("top_n_filtered_days")),
        # 参数敏感性：阈值挪一点优势还在不在（样本内指标，不是样本外表现）。
        "median_retention": _number(sensitivity.get("median_retention")),
        "worst_retention": _number(sensitivity.get("worst_retention")),
        "passed_constraints": _flag(item.get("passed_constraints")),
        "constraint_failures": [str(entry) for entry in failures]
        if isinstance(failures, (list, tuple))
        else [],
        "created_at": created_at,
    }
    if validation is not None:
        evidence["validation"] = validation
    if holdout is not None:
        evidence["holdout"] = holdout
    if consistency:
        evidence["consistency"] = {
            "requested": _integer(consistency.get("requested")),
            "used": _integer(consistency.get("used")),
            "evaluated": _integer(consistency.get("evaluated")),
            "better_than_baseline": _integer(consistency.get("better_than_baseline")),
            "note": consistency.get("note"),
        }
    evidence["study"] = _provenance(request, result, created_at)
    return evidence


def _provenance(
    request: Mapping[str, object],
    result: Mapping[str, object],
    created_at: str,
) -> dict[str, object]:
    """Where the candidate came from, so the snapshot can be traced back."""

    scanned = result.get("scanned_stocks")
    if not isinstance(scanned, (int, float)):
        scanned = result.get("processed")
    return {
        "start_date": request.get("start_date"),
        "end_date": request.get("end_date"),
        "scanned_stocks": _integer(scanned),
        "candidate_count": _integer(result.get("candidate_count")),
        "min_trades": _integer(request.get("min_trades")),
        "permutations": _integer(request.get("significance_permutations")),
        "created_at": created_at,
    }


def evidence_from_mapping(value: object) -> dict[str, Any]:
    """Normalize a stored evidence payload for the API layer."""

    evidence = as_mapping(value)
    return dict(evidence)
