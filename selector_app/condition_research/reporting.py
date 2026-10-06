"""Condition-research result and evidence shaping."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Mapping, MutableMapping, Sequence

import pandas as pd

from selector_app.screening.models import ScreenCondition
from selector_app.screening.rules import RuleGroup

from .mining import (
    DEFAULT_DUPLICATE_CORRELATION,
    MinedCondition,
    TopNContext,
    build_top_n_context,
    equivalent_condition_signatures,
    mine_binary_conditions,
)
from .models import ConditionResearchConfig
from .samples import (
    MiningSamples,
)
from .time_split import (
    MIN_WINDOW_DAYS,
    DateWindow,
    ResearchSplits,
)
from .trial_ledger import (
    sidak_adjust,
)


def _rolling_note(
    requested: int,
    *,
    used: int,
    holdout: DateWindow,
    has_candidates: bool,
) -> str | None:
    """Explain why fewer (or no) windows ran instead of doing it silently."""

    if requested <= 1:
        return None
    if not has_candidates:
        return "没有候选通过验证进入留出期，未做滚动复验"
    if used >= requested:
        return None
    days = (holdout.end_date - holdout.start_date).days + 1
    return (
        f"留出期只有 {days} 个自然日，切不出 {requested} 个窗口"
        f"（每窗至少 {MIN_WINDOW_DAYS} 个自然日），本次按 {max(used, 1)} 个窗口处理"
    )


def candidate_constraint_failures(
    candidate_summary: Mapping[str, object],
    baseline_summary: Mapping[str, object],
    *,
    min_trades: int,
    min_instruments: int,
    significant: bool | None = None,
) -> tuple[str, ...]:
    candidate = _pooled(candidate_summary)
    baseline = _pooled(baseline_summary)
    failures: list[str] = []
    # None 表示这次没做置换检验，此时不判断显著性；False 表示做过但没通过。
    if significant is False:
        failures.append("提升未超过噪声门槛")
    if (_number(candidate.get("closed_trades")) or 0.0) < min_trades:
        failures.append("交易样本不足")
    if (_number(candidate.get("stocks_with_trades")) or 0.0) < min_instruments:
        failures.append("覆盖股票不足")
    candidate_win = _number(candidate.get("win_rate"))
    baseline_win = _number(baseline.get("win_rate"))
    if candidate_win is None or baseline_win is None:
        failures.append("基础规则样本不足，无法比较胜率")
    elif candidate_win <= baseline_win:
        failures.append("胜率未高于基础规则")
    for key, label in (
        ("payoff_ratio", "盈亏比低于基础规则"),
        ("mean_trade_return", "平均单笔收益低于基础规则"),
    ):
        candidate_metric = _number(candidate.get(key))
        baseline_metric = _number(baseline.get(key))
        if baseline_metric is not None and (
            candidate_metric is None or candidate_metric + 1e-12 < baseline_metric
        ):
            failures.append(label)
    return tuple(failures)


def candidate_performance_warnings(
    candidate_summary: Mapping[str, object],
    baseline_summary: Mapping[str, object],
) -> tuple[str, ...]:
    """Return advisory per-stock metrics that should not block holdout evaluation."""

    candidate = _per_stock(candidate_summary)
    baseline = _per_stock(baseline_summary)
    warnings: list[str] = []
    for key, label in (
        ("median_total_return", "逐股中位总收益低于基础规则"),
        ("median_sharpe", "逐股中位夏普低于基础规则"),
        ("median_annual_return", "逐股中位年化收益低于基础规则"),
    ):
        candidate_metric = _number(candidate.get(key))
        baseline_metric = _number(baseline.get(key))
        if (
            candidate_metric is not None
            and baseline_metric is not None
            and candidate_metric + 1e-12 < baseline_metric
        ):
            warnings.append(label)
    return tuple(warnings)


def assign_validation_objective_scores(
    results: Sequence[MutableMapping[str, object]],
) -> None:
    """Score passing candidates by equal-weight validation metric percentiles.

    Percentile ranks keep win rate, per-stock median Sharpe, and per-stock
    median annual return on comparable scales. The score is only a shortlist
    ordering aid; all final conclusions still come from the untouched holdout.
    """

    by_side: dict[str, list[MutableMapping[str, object]]] = {}
    for result in results:
        result["validation_objective_score"] = None
        if result.get("passed_constraints") is True:
            by_side.setdefault(str(result.get("side") or ""), []).append(result)

    for candidates in by_side.values():
        component_scores: dict[int, list[float]] = {id(candidate): [] for candidate in candidates}
        for metric in ("win_rate", "median_sharpe", "median_annual_return"):

            def sort_key(item: MutableMapping[str, object]) -> tuple[bool, float, str]:
                value = _candidate_validation_metric(item, metric)
                return (
                    value is None,
                    -(value if value is not None else 0.0),
                    str(item.get("candidate_id") or ""),
                )

            ranked = sorted(
                candidates,
                key=sort_key,
            )
            index = 0
            while index < len(ranked):
                value = _candidate_validation_metric(ranked[index], metric)
                end = index + 1
                while end < len(ranked):
                    next_value = _candidate_validation_metric(ranked[end], metric)
                    if next_value != value:
                        break
                    end += 1
                average_rank = (index + end - 1) / 2
                percentile = 1.0 if len(ranked) == 1 else 1.0 - average_rank / (len(ranked) - 1)
                for candidate in ranked[index:end]:
                    component_scores[id(candidate)].append(percentile)
                index = end

        for candidate in candidates:
            scores = component_scores[id(candidate)]
            candidate["validation_objective_score"] = round(math.fsum(scores) / len(scores), 6)


def _candidate_validation_metric(result: Mapping[str, object], metric: str) -> float | None:
    validation = result.get("validation")
    if not isinstance(validation, Mapping):
        return None
    if metric == "win_rate":
        return _number(_pooled(validation).get("win_rate"))
    return _number(_per_stock(validation).get(metric))


def _per_stock(summary: Mapping[str, object]) -> Mapping[str, object]:
    value = summary.get("per_stock")
    return value if isinstance(value, Mapping) else {}


def _mine_sample_sets(
    samples: Sequence[MiningSamples],
    *,
    min_support: int,
    min_instruments: int,
    limit: int,
    excluded: frozenset[str] = frozenset(),
    top_n_context: TopNContext | None = None,
    permutations: int = 0,
    significance_quantile: float = 0.95,
    duplicate_correlation: float = DEFAULT_DUPLICATE_CORRELATION,
    stats: MutableMapping[str, int] | None = None,
    abort_check: Callable[[], bool] | None = None,
) -> tuple[MinedCondition, ...]:
    usable = [sample for sample in samples if len(sample)]
    if not usable:
        return ()
    # 前一日特征要么每份样本都有，要么一份都没有：混着拼会让穿越候选只覆盖一部分
    # 标的，命中率看着好看，实际是样本被悄悄换过了。
    previous_frames = [
        sample.previous_features for sample in usable if sample.previous_features is not None
    ]
    return mine_binary_conditions(
        pd.concat([sample.features for sample in usable], ignore_index=True),
        pd.concat([sample.outcomes for sample in usable], ignore_index=True),
        pd.concat([sample.instruments for sample in usable], ignore_index=True),
        sample_groups=pd.concat([sample.groups for sample in usable], ignore_index=True),
        min_support=min_support,
        min_instruments=min_instruments,
        limit=limit,
        excluded=excluded,
        top_n_context=top_n_context,
        permutations=permutations,
        significance_quantile=significance_quantile,
        duplicate_correlation=duplicate_correlation,
        previous_features=(
            pd.concat(previous_frames, ignore_index=True)
            if len(previous_frames) == len(usable)
            else None
        ),
        stats=stats,
        abort_check=abort_check,
    )


def _resolved_ranking(
    config: ConditionResearchConfig,
    ranking: Mapping[str, object],
) -> tuple[str | None, str]:
    """Pick the ranking value mining should assume: explicit choice first.

    Without a choice we fall back to the recommendation rather than skipping the
    diagnostic, so the top-N column still reflects a sensible slot order.
    """

    if config.ranking_value:
        return config.ranking_value, config.rank_order
    recommended = ranking.get("recommended")
    if isinstance(recommended, Mapping):
        value = recommended.get("value_id")
        order = recommended.get("order")
        if isinstance(value, str) and value:
            return value, order if isinstance(order, str) else config.rank_order
    return None, config.rank_order


def _top_n_context(
    samples: Sequence[MiningSamples],
    ranking_value: str | None,
    rank_order: str,
    top_n: int,
) -> TopNContext | None:
    """Build the slot-limited diagnostic input for one sample set.

    Returns None when no ranking value was resolved, which simply means the
    candidate table omits the top-N column instead of inventing a ranking.
    """

    if not ranking_value:
        return None
    usable = [sample for sample in samples if len(sample)]
    if not usable or ranking_value not in usable[0].features.columns:
        return None
    return build_top_n_context(
        pd.concat([sample.dates for sample in usable], ignore_index=True),
        pd.concat([sample.features[ranking_value] for sample in usable], ignore_index=True),
        top_n=top_n,
        ascending=rank_order == "asc",
    )


def _rule_condition_signatures(rule: RuleGroup | None) -> frozenset[str]:
    """Signatures of a base rule, so mining never re-proposes its own conditions.

    A buy candidate is ANDed with the base buy rule and a sell candidate is ORed
    with the base sell rule; a condition already present on the same side can
    therefore only produce a no-op candidate.
    """

    if rule is None:
        return frozenset()
    return frozenset(
        signature
        for condition in (*rule.required_conditions, *rule.conditions)
        for signature in equivalent_condition_signatures(condition)
    )


def _condition_dict(condition: ScreenCondition) -> dict[str, object]:
    return {
        "left_value": condition.left_value,
        "operator": condition.operator,
        "right_value": condition.right_value,
        "right_constant": condition.right_constant,
    }


def _trial_context(
    *,
    tested_conditions: int,
    trials: int,
    studies: int,
    cumulative: int | None = None,
) -> dict[str, object]:
    """The ledger summary a report carries.

    ``trials`` is independent data-window rounds and drives the correction;
    ``studies`` is how many studies the ledger holds. The gap between them is
    itself the finding: nine studies over one window are one round, and that
    says more about over-mining than any p-value does.
    """

    return {
        "trial_count": trials,
        "study_count": studies,
        "tested_conditions": tested_conditions,
        "cumulative_conditions": cumulative if cumulative is not None else tested_conditions,
        # 只有真的跑过多轮独立数据才动过判定，单次研究不该显示"已校正"。
        "adjusted": trials > 1,
        # 研究次数多于轮数 = 有一段数据被反复挖过。
        "repeated_studies": max(0, studies - trials),
    }


def _mining_dict(
    candidate: MinedCondition,
    *,
    trial_count: int = 1,
    quantile: float = 0.95,
) -> dict[str, object]:
    raw_pvalue = candidate.permutation_pvalue
    adjusted_pvalue = sidak_adjust(raw_pvalue, trial_count) if raw_pvalue is not None else None
    # 判定只用校正后的 p：跑过 K 轮数据还拿这个 p，撞运气的机会是 K 倍。
    # 原始 p 一并保留，用户能看到校正了多少。
    significant = (
        adjusted_pvalue is not None and adjusted_pvalue <= (1.0 - quantile)
        if adjusted_pvalue is not None
        else candidate.significant
    )
    return {
        "support_count": candidate.support_count,
        "support_ratio": candidate.support_ratio,
        "instrument_count": candidate.instrument_count,
        "outcome_rate": candidate.outcome_rate,
        "baseline_rate": candidate.baseline_rate,
        "lift": candidate.lift,
        # Read-only slot-limit diagnostic; None when no ranking value was
        # resolved, and only meaningful when top_n_filtered_days > 0.
        "top_n_outcome_rate": candidate.top_n_outcome_rate,
        "top_n_support_count": candidate.top_n_support_count,
        "top_n_filtered_days": candidate.top_n_filtered_days,
        # 置换检验结果。三个字段同时为 None 表示这次没做检验，界面就不显示显著性列。
        "lift_threshold": candidate.lift_threshold,
        # 原始 p：本次候选池里纯运气撞出这么好的概率。
        "permutation_pvalue": raw_pvalue,
        # 累计试验校正后的 p（跑过 K 轮独立数据时才与原始值不同）。
        "adjusted_pvalue": adjusted_pvalue,
        "trial_count": trial_count,
        "significant": significant,
        # 被这条候选合并掉的同义条件。列出来而不是静默删掉，用户才能判断门槛合不合适。
        "duplicates": [
            {
                "condition": _condition_dict(item.condition),
                "correlation": item.correlation,
                "lift": item.lift,
                "support_count": item.support_count,
            }
            for item in candidate.duplicates
        ],
    }


def _disclosure(
    universe: str,
    total: int,
    processed: int,
) -> dict[str, object]:
    """Data-coverage and survivorship-bias disclosure required by the contract.

    The universe is whatever the current market-data store lists, so delisted
    symbols and historical index membership simply are not there.  The product
    contract forbids presenting that as a complete historical market without
    saying so, so every report carries the limitation explicitly.
    """

    return {
        "universe_scope": "current_listed_only",
        "universe": universe,
        "survivorship_bias": True,
        "requested_instruments": total,
        "processed_instruments": processed,
        "coverage_ratio": (processed / total) if total else 0.0,
        "notes": [
            "标的范围取自当前行情库中的在市标的，无法还原历史时点成分或退市标的，"
            "结果存在幸存者偏差，不应视为完整历史全市场。",
            f"本次仅 {processed}/{total} 只标的进入统计，未覆盖标的未参与任何汇总。",
        ],
    }


def _empty_diagnostic(
    config: ConditionResearchConfig,
    sample_totals: Mapping[str, Mapping[str, object]],
) -> str:
    """Explain *why* nothing was mined.

    "Not enough trades" used to be the only answer, but the common cause is a
    saturated baseline: when every base trade is profitable (or every one is a
    loss) there is nothing to contrast against and no threshold can look better
    than 100%/0%. Telling the user to widen the range in that case sends them
    chasing the wrong fix.
    """

    counts: dict[str, int] = {}
    for side, payload in sample_totals.items():
        raw = payload.get("count")
        counts[side] = raw if isinstance(raw, int) and not isinstance(raw, bool) else 0
    searched = {side for side in ("buy", "sell") if config.search_sides in {side, "both"}}
    if searched and all(counts.get(side, 0) >= config.min_trades for side in searched):
        return (
            "样本量足够，但基准交易的盈亏方向过于一致（胜率接近 0% 或 100%），"
            "没有可对照的样本，挖不出有区分度的条件。"
            "单边模式（持续满仓 / 持有到期末）更容易出现这种饱和基线，"
            "可以换一侧搜索、放宽日期区间，或让基础条件更宽松一些。"
        )
    return "基础交易样本不足，未发现满足样本和覆盖要求的候选条件"


def _sample_totals(
    buy_samples: Sequence[MiningSamples],
    sell_samples: Sequence[MiningSamples],
) -> dict[str, dict[str, object]]:
    """Per-side mined sample totals, so the UI can show a correct hit ratio.

    The two sides count different units.  A buy sample is taken on the entry
    signal date, so ``support_count / buy total`` really is an entry hit rate.
    A sell sample is taken on every early-exit decision point inside a holding
    period, so several samples can belong to one trade; dividing those by the
    closed-trade count would overstate the ratio and could exceed 100%.
    """

    return {
        "buy": {
            "count": sum(len(samples) for samples in buy_samples),
            "unit": "entry",
            **_daily_hits(buy_samples),
        },
        "sell": {
            "count": sum(len(samples) for samples in sell_samples),
            "unit": "decision_point",
            **_daily_hits(sell_samples),
        },
    }


def _daily_hits(samples: Sequence[MiningSamples]) -> dict[str, object]:
    """Average number of stocks hitting the rule on a day that had any hit.

    This is the number that decides whether a condition matters in a slot-based
    portfolio at all: if the rule flags 300 stocks a day and the portfolio has
    five slots, the ranking - not the condition - chooses what gets bought.
    Dividing by days that had at least one hit (rather than every trading day)
    keeps the figure comparable to a slot count, which is also only spent on
    days when something passes the filter.
    """

    total = 0
    days: set[str] = set()
    for item in samples:
        total += len(item)
        days.update(str(value) for value in item.dates.tolist())
    hit_days = len(days)
    return {
        "hit_days": hit_days,
        "daily_hits": (total / hit_days) if hit_days else None,
    }


def _splits_dict(splits: ResearchSplits) -> dict[str, dict[str, str]]:
    return {
        name: {
            "start_date": window.start_date.isoformat(),
            "end_date": window.end_date.isoformat(),
        }
        for name, window in (
            ("train", splits.train),
            ("validation", splits.validation),
            ("holdout", splits.holdout),
        )
    }


def _without_stock_rows(summary: Mapping[str, object]) -> dict[str, object]:
    result = dict(summary)
    result.pop("stocks", None)
    return result


def _pooled(summary: Mapping[str, object]) -> Mapping[str, object]:
    value = summary.get("pooled_trades")
    return value if isinstance(value, Mapping) else {}


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    resolved = float(value)
    return resolved if math.isfinite(resolved) else None


def _combined_fingerprint(fingerprints: Mapping[str, str]) -> str | None:
    if not fingerprints:
        return None
    hasher = hashlib.sha256()
    for symbol, fingerprint in sorted(fingerprints.items()):
        hasher.update(symbol.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(fingerprint.encode("ascii"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def _summary_number(result: Mapping[str, object], key: str) -> float:
    validation = result.get("validation")
    if not isinstance(validation, Mapping):
        return float("-inf")
    if key in {"improved_stock_ratio", "median_sharpe", "median_annual_return"}:
        per_stock = validation.get("per_stock")
        value = per_stock.get(key) if isinstance(per_stock, Mapping) else None
    else:
        value = _pooled(validation).get(key)
    number = _number(value)
    return number if number is not None else float("-inf")


def _candidate_evidence_key(
    result: Mapping[str, object],
) -> tuple[float, float, float, float, float, float, str]:
    """候选排序键：先用验证综合名次分，再用样本外证据稳定排序。

    综合分平衡验证胜率、逐股中位夏普和逐股中位年化收益。后续字段用于综合分
    并列时定序，成交笔数只表达证据厚度。

    特意不用 mining 的 lift：它是寻优段算出来的，拿它决定「先看哪条候选、送
    哪条去留出期复验」等于按过拟合程度挑人。挖掘输出本身是 lift 降序，末尾不
    放一个显式稳定键的话，并列顺序会顺着输入把 lift 又漏回来。
    """

    objective_score = _number(result.get("validation_objective_score"))
    return (
        -(objective_score if objective_score is not None else float("-inf")),
        -_summary_number(result, "win_rate"),
        -_summary_number(result, "median_sharpe"),
        -_summary_number(result, "median_annual_return"),
        -_summary_number(result, "improved_stock_ratio"),
        -_summary_number(result, "closed_trades"),
        str(result.get("candidate_id") or ""),
    )


def _select_holdout_candidates(
    results: Sequence[Mapping[str, object]],
    limit_per_side: int,
) -> set[str]:
    by_side: dict[str, list[Mapping[str, object]]] = {}
    for result in results:
        if result.get("passed_constraints") is True:
            by_side.setdefault(str(result.get("side")), []).append(result)
    selected: set[str] = set()
    for candidates in by_side.values():
        candidates.sort(key=_candidate_evidence_key)
        selected.update(
            str(candidate["candidate_id"])
            for candidate in candidates[:limit_per_side]
            if isinstance(candidate.get("candidate_id"), str)
        )
    return selected
