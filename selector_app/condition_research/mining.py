"""Interpretable contrastive rule mining over completed trade samples."""

from __future__ import annotations

import math
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from selector_app.screening.models import ScreenCondition

DEFAULT_QUANTILES = (0.2, 0.4, 0.6, 0.8)
# Fixed shuffle seed: a permutation test must not make a study less
# reproducible than the search it audits, and a fixed seed keeps the same
# inputs producing the same threshold run after run.
_PERMUTATION_SEED = 20240918
# Comparing two series whose spreads differ by orders of magnitude degenerates
# into "the varying series vs roughly zero". That is how five different feature
# pairs collapsed into the same `indicator_three.var1 < 0` condition, so pairs
# beyond this ratio are rejected. Scale-compatible pairs (ma5 vs ma20,
# rsi14 vs ma20) stay well inside the limit.
_MAX_SCALE_RATIO = 100.0
# Two conditions whose matching samples agree this closely are the same finding
# for the study; only the best-ranked one is reported. This is the fallback rule:
# it fires even when the correlation cannot be computed.
_DUPLICATE_AGREEMENT = 0.995
# Two conditions whose hit vectors correlate at least this strongly are the same
# finding wearing different thresholds. See ``docs/duplicate-merging-plan.md``:
# the same indicator at adjacent quantile thresholds lands near 0.6~0.7, while
# thresholds two quantiles apart drop to ~0.4 and stay separate - they are a
# different aggressiveness, not a repeat. Picking a cut point *within* one
# finding is what parameter sensitivity analysis is for, not what a candidate
# slot is for.
DEFAULT_DUPLICATE_CORRELATION = 0.6
# Defence in depth. The caller already restricts mined features to the
# cross-stock comparable set, so none of these should arrive today; they stay
# listed because "never mine an absolute price level into a shared threshold" is
# a correctness rule, not an incidental consequence of whoever builds the
# feature list. Nothing here is dead weight if that list ever widens.
_PRICE_SCALE_FEATURES = frozenset(
    {
        "technical.ma5",
        "technical.ma10",
        "technical.ma20",
        "technical.ma60",
        "technical.macd",
        "technical.macd_signal",
        "technical.macd_histogram",
        # Raw prices are absolute per stock; they are supported as user-written
        # condition operands but must never become a mined cross-stock threshold.
        "price.open",
        "price.high",
        "price.low",
        "price.close",
    }
)


def condition_signature(condition: ScreenCondition) -> str:
    """Stable textual key for one condition, including its constant operand."""

    right = (
        condition.right_value
        if condition.right_value is not None
        else f"={condition.right_constant}"
    )
    return f"{condition.left_value}|{condition.operator}|{right}"


def equivalent_condition_signatures(condition: ScreenCondition) -> frozenset[str]:
    """Every signature that means the same thing as one condition.

    ``a > b`` equals ``b < a`` and ``a 上穿 b`` equals ``b 下穿 a``. Mining must
    not re-propose a rule the base strategy already applies, because ANDing a
    condition with itself cannot change a single trade.
    """

    signature = condition_signature(condition)
    right = condition.right_value
    if right is None:
        return frozenset({signature})
    mirrored = {
        "gt": "lt",
        "lt": "gt",
        "gte": "lte",
        "lte": "gte",
        "cross_up": "cross_down",
        "cross_down": "cross_up",
    }.get(str(condition.operator))
    if mirrored is None:
        return frozenset({signature})
    return frozenset(
        {
            signature,
            condition_signature(
                ScreenCondition(
                    left_value=right, operator=mirrored, right_value=condition.left_value
                )
            ),
        }
    )


def _spread(values: np.ndarray) -> float:
    """Interquartile range; 0 means the comparison treats the series as constant."""

    low, high = np.percentile(values, [25, 75])
    spread = float(high) - float(low)
    return spread if np.isfinite(spread) and spread > 0.0 else 0.0


def _comparable_scale(left: np.ndarray, right: np.ndarray) -> bool:
    """Reject pairwise comparisons dominated by one operand's magnitude."""

    left_spread = _spread(left)
    right_spread = _spread(right)
    if left_spread == 0.0 or right_spread == 0.0:
        return False
    return max(left_spread, right_spread) / min(left_spread, right_spread) <= _MAX_SCALE_RATIO


def _jaccard(
    left: np.ndarray,
    right: np.ndarray,
    *,
    valid: np.ndarray,
    other_valid: np.ndarray,
) -> float:
    """Overlap of two match sets, measured as ``intersection / union``.

    Both arguments are sorted arrays of sample positions. This is the cheap
    pre-filter in front of ``_phi_correlation``: a Jaccard of ``j`` caps the phi
    coefficient at ``sqrt(j)``, so a pair below ``t ** 2`` can never reach a
    threshold of ``t`` and is skipped before any of the expensive counting
    happens.

    **The ratio must be taken on the samples both conditions can evaluate**, not
    on the whole sample space. Two conditions rarely share a valid space - a
    250-day moving average simply has no value in its first 250 rows - and rows
    one of them cannot evaluate are not in the phi table at all. Counting them in
    the union dilutes ``j`` below its true value and the bound stops holding: a
    pair that is perfectly nested on the shared rows can look barely overlapping
    overall and get skipped, which is exactly a duplicate that should have been
    merged.
    """

    both = int(np.intersect1d(left, right).size)
    if both == 0:
        return 0.0
    only_left = int(other_valid[left].sum()) - both
    only_right = int(valid[right].sum()) - both
    union = both + only_left + only_right
    if union <= 0:
        return 0.0
    return both / union


def _bits(mask: np.ndarray) -> int:
    """Pack a sample-space mask into one integer for O(n/64) intersections.

    Counting the intersection of two evaluable-sample sets is the only step in
    the duplicate check that touches the whole sample space, and it runs once
    per candidate pair. Packing turns that into a single machine-word-wise AND
    plus a popcount instead of a full-length boolean pass.
    """

    return int.from_bytes(np.packbits(mask).tobytes(), "big")


def _phi_correlation(
    matched: np.ndarray,
    other_matched: np.ndarray,
    *,
    valid: np.ndarray,
    other_valid: np.ndarray,
    valid_bits: int,
    other_valid_bits: int,
) -> float | None:
    """Correlation of two conditions' hit vectors (the phi coefficient).

    Two conditions are scored on different sample spaces - every indicator has
    its own gaps - so the comparison is restricted to the samples both can
    evaluate. Everything else would silently credit one condition with hits it
    never had a chance to make.

    Returns None when the coefficient is undefined (an empty overlap, or a
    degenerate table where the two masks together cover every evaluable sample).
    The caller decides what to do; nothing here guesses a value.
    """

    total = (valid_bits & other_valid_bits).bit_count()
    if total == 0:
        return None
    both = int(np.intersect1d(matched, other_matched).size)
    # A match set is always inside its own valid space, so what remains is how
    # much of it survives into the *other* condition's space.
    only_left = int(other_valid[matched].sum()) - both
    only_right = int(valid[other_matched].sum()) - both
    neither = total - both - only_left - only_right
    if min(only_left, only_right, neither) < 0:
        return None
    numerator = both * neither - only_left * only_right
    denominator = math.sqrt(
        float(both + only_left)
        * float(only_right + neither)
        * float(both + only_right)
        * float(only_left + neither)
    )
    if denominator <= 0.0:
        return None
    return float(numerator / denominator)


def _permutation_max_lifts(
    pool: Sequence[tuple[np.ndarray, bytes]],
    baselines: Mapping[bytes, tuple[np.ndarray, np.ndarray]],
    target: np.ndarray,
    *,
    permutations: int,
    seed: int | None,
) -> list[float]:
    """Best lift each shuffled label set produces across the mask pool.

    Only the labels move: the masks, the trade-group reduction and the
    support/instrument floors are all label-independent, so they are reused.
    That makes a shuffle roughly an order of magnitude cheaper than re-running
    the whole search.

    The pool must be every structurally usable mask, **not** the shortlist that
    beat the baseline on the real labels. Selecting the pool on the labels
    shrinks it, and the maximum over a smaller pool is smaller, so the
    resulting threshold lets through exactly the lucky-but-empty conditions the
    test is meant to reject.

    Each run reports the *maximum* lift, matching the selection pressure the
    real search applies - this is the max-T null distribution, not the null
    distribution of a single fixed rule.

    **Why this shuffles cells, not trading days.** 波段研究和因子检验按交易日块
    搬动标签，这里不这么做，是有原因的，不是漏改：

    1. 这里的样本已经被 trade group（股票 + 信号日）归约成一笔交易一个单位，
       同一天的样本数每天都不相等。按日期块搬动要求「源日期的样本集合」和
       「目标日期的样本集合」一一对应，长度不等时只能循环填充或有放回抽样，
       那引入的近似误差和要修的偏差是同一个量级。
    2. 实测过代价。用真实 A 股日线（主板 200 只、2021-2025、持有 5 日，见
       ``scripts/perm_caliber_regression.py``）：日度胜率的相邻日自相关高达
       0.78，名义样本 24 万实际只有 1212 个独立交易日 —— 结构上确实该按块搬。
       但两种口径的 95% 噪声门槛只差 **1.5%**（逐单元格 1.044 / 按日期块
       1.059），而且是**按日期块更严**。条件池从 70 放大到 266 个，差异稳定在
       1.5%，不随多重检验规模放大。

    合成数据上曾经得到过相反的结论（逐单元格更保守 2.5%），原因是合成数据的
    日间相关是拍出来的。**判断置换粒度时不要用合成数据下结论。**

    结论：影响 1.5% 且方向偏保守，为了它换成无法干净实现的近似置换不划算。
    这里保持逐单元格，代价是门槛比理论上严一点点的口径略松 1.5%。
    """

    rng = np.random.default_rng(_PERMUTATION_SEED if seed is None else seed)
    results: list[float] = []
    for _ in range(permutations):
        shuffled = rng.permutation(target)
        baseline_rates = {
            key: _grouped_outcome_rate(shuffled[valid], groups)
            for key, (valid, groups) in baselines.items()
        }
        best = 0.0
        for matched, baseline_key in pool:
            baseline = baseline_rates[baseline_key]
            if not baseline > 0.0:
                continue
            rate = float(np.mean(shuffled[matched]))
            if rate <= baseline:
                continue
            lift = rate / baseline
            if lift > best:
                best = lift
        # No candidate survived the shuffle: nothing beat chance, so the
        # run contributes the neutral lift instead of a misleading 0.
        results.append(best if best > 0.0 else 1.0)
    return results


def _with_significance(
    candidate: MinedCondition,
    threshold: float | None,
    null_max_lifts: Sequence[float],
    *,
    quantile: float,
) -> MinedCondition:
    if threshold is None or not null_max_lifts:
        return candidate
    reached = sum(1 for value in null_max_lifts if value >= candidate.lift)
    pvalue = (1 + reached) / (1 + len(null_max_lifts))
    return replace(
        candidate,
        lift_threshold=threshold,
        permutation_pvalue=pvalue,
        # One verdict, one number. Deriving "significant" from ``lift > threshold``
        # while showing a p-value computed as ``(1 + reached) / (1 + n)`` made the
        # badge and the tooltip disagree: with 20 permutations the 0.95 quantile
        # sits near the second-largest null lift, so finishing runner-up to noise
        # counted as significant (2/21) even though p was 0.095.
        significant=pvalue <= (1.0 - quantile),
    )


@dataclass(frozen=True)
class DuplicateCondition:
    """A condition the study dropped because it repeats the one it is listed on.

    Dropping is invisible by default, which is exactly the problem: a condition
    the study threw away and a condition it never found look identical on the
    report. Keeping the text, the correlation and the lift makes the merge
    auditable - the reader can disagree with the threshold and see what it cost.
    """

    condition: ScreenCondition
    correlation: float
    lift: float
    support_count: int


@dataclass(frozen=True)
class MinedCondition:
    """A single numeric condition enriched for the requested trade outcome."""

    condition: ScreenCondition
    support_count: int
    support_ratio: float
    instrument_count: int
    outcome_rate: float
    baseline_rate: float
    lift: float
    # Read-only "would this survive a slot-limited portfolio?" diagnostic. The
    # study itself backtests every qualifying signal, so its win rate describes
    # the whole pool; a portfolio only fills ``max_positions`` slots by ranking.
    # These fields re-score the same matched samples after keeping just the
    # top-ranked ones per signal day. They never influence mining order.
    top_n_outcome_rate: float | None = None
    top_n_support_count: int = 0
    # Days where more stocks matched than there are slots - i.e. days the
    # ranking actually had to choose. Without them the top-N split is a no-op
    # and the column would silently duplicate the pool-wide rate.
    top_n_filtered_days: int = 0
    # Permutation-test diagnostics. Both stay None when the caller did not ask
    # for a permutation test, so a study can still run without one.
    # ``lift_threshold`` is the lift a candidate has to beat before it is
    # distinguishable from the best of ``permutations`` shuffled label sets.
    lift_threshold: float | None = None
    # Fraction of shuffled runs whose best lift reached this candidate's lift.
    permutation_pvalue: float | None = None
    # The single verdict, derived from ``permutation_pvalue`` against the
    # configured quantile so it can never contradict the p-value shown next to
    # it. None when no permutation test ran.
    significant: bool | None = None
    # Conditions this one absorbed: same finding, different wording, ranked
    # lower. Empty when nothing was merged into it.
    duplicates: tuple[DuplicateCondition, ...] = ()


@dataclass(frozen=True)
class TopNContext:
    """Everything needed to re-score matches as a slot-limited portfolio would.

    ``day_codes`` is a factorized signal-day id per sample, so grouping is an
    integer operation inside the candidate loop instead of a string sort.
    """

    day_codes: np.ndarray
    values: np.ndarray
    top_n: int
    ascending: bool


def build_top_n_context(
    dates: pd.Series,
    values: pd.Series,
    *,
    top_n: int,
    ascending: bool,
) -> TopNContext | None:
    """Build a diagnostic context, or None when the value cannot be used."""

    if top_n < 1 or len(dates) == 0 or len(dates) != len(values):
        return None
    numeric = pd.to_numeric(values, errors="coerce").to_numpy(dtype=float)
    if not np.isfinite(numeric).any():
        return None
    return TopNContext(
        day_codes=pd.factorize(dates)[0],
        values=numeric,
        top_n=top_n,
        ascending=ascending,
    )


def mine_binary_conditions(
    features: pd.DataFrame,
    outcomes: Sequence[bool] | pd.Series,
    instruments: Sequence[str] | pd.Series,
    *,
    sample_groups: Sequence[str] | pd.Series | None = None,
    quantiles: Sequence[float] = DEFAULT_QUANTILES,
    min_support: int = 20,
    min_instruments: int = 5,
    limit: int = 50,
    excluded: frozenset[str] = frozenset(),
    top_n_context: TopNContext | None = None,
    permutations: int = 0,
    significance_quantile: float = 0.95,
    permutation_seed: int | None = None,
    duplicate_correlation: float = DEFAULT_DUPLICATE_CORRELATION,
    # 每个决策点**前一个交易日**的特征（行与 features 对齐，取不到就是 NaN）。
    # 给了它，值值对比才会额外生成「上穿/下穿」候选——穿越要靠前后两根比，
    # 而样本本身是横着摆的快照，相邻两行之间没有先后关系。不给就只挖大小比较。
    previous_features: pd.DataFrame | None = None,
    # 出参：本次**实际评分过**的条件数（= 送入置换零分布的 mask 池大小）。
    # 用出参而不是改返回值，是因为 18 处既有测试把返回值直接当候选元组用，
    # 改返回类型会连带改一大片与本次无关的东西。
    stats: MutableMapping[str, int] | None = None,
) -> tuple[MinedCondition, ...]:
    """Find readable thresholds whose matching samples are enriched for an outcome.

    Outcomes should indicate profitable trades for buy-filter discovery and
    losing trades for sell-trigger discovery. Candidate thresholds are derived
    only from the samples passed by the caller, allowing chronological
    train/validation/holdout separation.

    ``excluded`` carries ``equivalent_condition_signatures`` of the base rule so
    the miner never proposes a condition the strategy already applies.

    Sample keys are factorized to integer codes once so the per-candidate work
    stays vectorized. ``pd.factorize`` hands out codes in order of first
    appearance, which is the same order the earlier dict-based implementation
    iterated, keeping results reproducible.

    ``permutations > 0`` runs a label-shuffling test. Thousands of thresholds
    are scored here, so the best lift among them is inflated even on pure
    noise; the test re-scores the *same* candidate masks against shuffled
    labels and reports how high the best lift gets by chance. Results stay
    deterministic because the shuffle uses a fixed seed by default.

    Conditions whose hit vectors correlate at ``duplicate_correlation`` or above
    are the same finding, so only the best-ranked one is returned and the rest
    are attached to it as ``duplicates`` rather than vanishing. Slots freed this
    way are refilled from further down the ranking, so merging never shrinks the
    result - it changes which conditions occupy it.
    """

    if min_support < 1:
        raise ValueError("最少支持样本数必须大于 0")
    if min_instruments < 1:
        raise ValueError("最少覆盖股票数必须大于 0")
    if limit < 1:
        raise ValueError("候选条件上限必须大于 0")
    if permutations < 0:
        raise ValueError("置换检验次数不能为负数")
    if not 0.0 < significance_quantile <= 1.0:
        raise ValueError("显著性分位数必须在 0 和 1 之间")
    # 1.0 means "only merge exact repeats", which is as close to off as the rule
    # gets - the 0.995 overlap fallback still applies below it.
    if not 0.5 <= duplicate_correlation <= 1.0:
        raise ValueError("同义条件合并门槛必须在 0.5 和 1 之间")
    resolved_quantiles = tuple(sorted(set(float(value) for value in quantiles)))
    if not resolved_quantiles or any(not 0.0 < value < 1.0 for value in resolved_quantiles):
        raise ValueError("候选分位数必须在 0 和 1 之间")

    target = np.asarray(outcomes, dtype=bool)
    symbols = np.asarray(instruments, dtype=object)
    groups = (
        np.asarray(sample_groups, dtype=object)
        if sample_groups is not None
        else np.arange(len(target), dtype=object)
    )
    if target.ndim != 1 or symbols.ndim != 1:
        raise ValueError("交易样本必须是一维数据")
    if groups.ndim != 1 or len(features) != len(target) or len(target) != len(symbols):
        raise ValueError("样本数量不一致")
    if len(target) != len(groups):
        raise ValueError("样本数量不一致")
    if len(target) == 0 or features.empty:
        if stats is not None:
            stats["tested_conditions"] = 0
        return ()

    valid_symbols = pd.notna(symbols) & pd.notna(groups)
    if top_n_context is not None and top_n_context.values.size != len(target):
        # The context is indexed by full-sample position. A length mismatch would
        # silently score unrelated rows, so drop the diagnostic instead.
        top_n_context = None
    feature_ids = tuple(str(column) for column in features.columns)
    arrays = {
        feature_id: pd.to_numeric(features[feature_id], errors="coerce").to_numpy(dtype=float)
        for feature_id in feature_ids
    }
    finite = {feature_id: np.isfinite(values) for feature_id, values in arrays.items()}
    # 前一日特征：只在行列都对得上时才用来生成穿越候选，缺列或长度不符就整段跳过，
    # 而不是拿错位的行去编造「昨天的值」。
    previous_arrays: dict[str, np.ndarray] = {}
    if previous_features is not None and len(previous_features) == len(target):
        previous_arrays = {
            feature_id: pd.to_numeric(previous_features[feature_id], errors="coerce").to_numpy(
                dtype=float
            )
            for feature_id in feature_ids
            if feature_id in previous_features.columns
        }
    comparable = {
        feature_id: _has_two_distinct(arrays[feature_id], finite[feature_id])
        for feature_id in feature_ids
    }
    target_float = target.astype(np.float64)
    group_codes = pd.factorize(groups)[0]
    instrument_codes = pd.factorize(symbols)[0]

    # Each scored candidate keeps the sample rows it matched (already reduced to
    # one per trade group) plus the key of the valid-sample window its baseline
    # was measured on. That is everything the permutation test needs to rescore
    # it without recomputing a single threshold mask.
    candidates: list[tuple[MinedCondition, np.ndarray, bytes]] = []
    # The null distribution draws from every structurally usable mask, winners
    # and losers alike. See ``_candidate_for_mask`` for why the losers matter.
    null_pool: list[tuple[np.ndarray, bytes]] = []
    # Keyed by the valid-sample mask itself rather than by the feature pair:
    # when no feature has gaps, every pair shares one mask, and keying by pair
    # used to store hundreds of identical copies of it.
    baselines: dict[bytes, tuple[np.ndarray, np.ndarray]] = {}
    # Packed form of each distinct evaluable-sample mask, built once per mask
    # rather than once per candidate pair.
    bit_cache: dict[bytes, int] = {}
    seen_rules: set[tuple[str, str, str | None, float | None]] = set()

    def record(
        scored: tuple[MinedCondition | None, np.ndarray | None],
        valid: np.ndarray,
    ) -> None:
        candidate, matched_global = scored
        if matched_global is None:
            return
        token = valid.tobytes()
        if token not in baselines:
            baselines[token] = (valid, group_codes[valid])
        null_pool.append((matched_global, token))
        if candidate is not None:
            candidates.append((candidate, matched_global, token))

    for feature_id in feature_ids:
        if feature_id in _PRICE_SCALE_FEATURES:
            continue
        values = arrays[feature_id]
        valid = finite[feature_id] & valid_symbols
        if int(valid.sum()) < min_support:
            continue
        valid_values = values[valid]
        if len(np.unique(valid_values)) < 2:
            continue
        positions = np.flatnonzero(valid)

        valid_target = target_float[valid]
        valid_instruments = instrument_codes[valid]
        valid_groups = group_codes[valid]
        baseline_rate = _grouped_outcome_rate(valid_target, valid_groups)
        if not 0.0 < baseline_rate < 1.0:
            continue
        thresholds = np.unique(np.quantile(valid_values, resolved_quantiles))

        for threshold in thresholds:
            for operator, condition_mask in (
                ("gte", valid_values >= threshold),
                ("lte", valid_values <= threshold),
            ):
                condition = ScreenCondition(
                    left_value=feature_id,
                    operator=operator,
                    right_constant=float(threshold),
                )
                key = _condition_key(condition)
                if key in seen_rules or condition_signature(condition) in excluded:
                    continue
                seen_rules.add(key)
                candidate = _candidate_for_mask(
                    condition,
                    condition_mask,
                    valid_target,
                    valid_instruments,
                    valid_groups,
                    min_support=min_support,
                    min_instruments=min_instruments,
                    baseline_rate=baseline_rate,
                    top_n_context=top_n_context,
                    sample_positions=positions,
                )
                record(candidate, valid)

    for left_index, left_id in enumerate(feature_ids):
        if not comparable[left_id]:
            continue
        left_values = arrays[left_id]
        left_finite = finite[left_id]
        for right_id in feature_ids[left_index + 1 :]:
            if not comparable[right_id]:
                continue
            right_values = arrays[right_id]
            valid = left_finite & finite[right_id] & valid_symbols
            if int(valid.sum()) < min_support:
                continue
            valid_left = left_values[valid]
            valid_right = right_values[valid]
            if np.all(valid_left == valid_right):
                continue
            # One side nearly constant next to a wide-ranging side means the rule
            # is really "the wide side vs ~0"; those collapse into duplicates.
            if not _comparable_scale(valid_left, valid_right):
                continue
            positions = np.flatnonzero(valid)
            valid_target = target_float[valid]
            valid_instruments = instrument_codes[valid]
            valid_groups = group_codes[valid]
            baseline_rate = _grouped_outcome_rate(valid_target, valid_groups)
            if not 0.0 < baseline_rate < 1.0:
                continue
            for operator, condition_mask in (
                ("gt", valid_left > valid_right),
                ("lt", valid_left < valid_right),
            ):
                condition = ScreenCondition(
                    left_value=left_id,
                    operator=operator,
                    right_value=right_id,
                )
                key = _condition_key(condition)
                if key in seen_rules or condition_signature(condition) in excluded:
                    continue
                seen_rules.add(key)
                candidate = _candidate_for_mask(
                    condition,
                    condition_mask,
                    valid_target,
                    valid_instruments,
                    valid_groups,
                    min_support=min_support,
                    min_instruments=min_instruments,
                    baseline_rate=baseline_rate,
                    top_n_context=top_n_context,
                    sample_positions=positions,
                )
                record(candidate, valid)

            # 同一对指标再看「昨天在下面、今天翻上去」这类时点。穿越要用前一交易日
            # 的值，样本窗口因此比大小比较更小——第一根 K 线永远没有昨天可比。
            if left_id in previous_arrays and right_id in previous_arrays:
                previous_left_all = previous_arrays[left_id]
                previous_right_all = previous_arrays[right_id]
                cross_valid = (
                    valid & np.isfinite(previous_left_all) & np.isfinite(previous_right_all)
                )
                if int(cross_valid.sum()) >= min_support:
                    previous_left = previous_left_all[cross_valid]
                    previous_right = previous_right_all[cross_valid]
                    crossed_left = left_values[cross_valid]
                    crossed_right = right_values[cross_valid]
                    cross_groups = group_codes[cross_valid]
                    cross_target = target_float[cross_valid]
                    cross_baseline = _grouped_outcome_rate(cross_target, cross_groups)
                    if 0.0 < cross_baseline < 1.0:
                        for operator, condition_mask in (
                            (
                                "cross_up",
                                (previous_left <= previous_right)
                                & (crossed_left > crossed_right),
                            ),
                            (
                                "cross_down",
                                (previous_left >= previous_right)
                                & (crossed_left < crossed_right),
                            ),
                        ):
                            condition = ScreenCondition(
                                left_value=left_id,
                                operator=operator,
                                right_value=right_id,
                            )
                            key = _condition_key(condition)
                            if key in seen_rules or condition_signature(condition) in excluded:
                                continue
                            seen_rules.add(key)
                            candidate = _candidate_for_mask(
                                condition,
                                condition_mask,
                                cross_target,
                                instrument_codes[cross_valid],
                                cross_groups,
                                min_support=min_support,
                                min_instruments=min_instruments,
                                baseline_rate=cross_baseline,
                                top_n_context=top_n_context,
                                sample_positions=np.flatnonzero(cross_valid),
                            )
                            record(candidate, cross_valid)

    ranked = sorted(
        candidates,
        key=lambda item: (
            -item[0].lift,
            -item[0].outcome_rate,
            -item[0].support_count,
            -item[0].instrument_count,
            item[0].condition.left_value,
            str(item[0].condition.operator),
            float(item[0].condition.right_constant or 0.0),
        ),
    )
    null_max_lifts = (
        _permutation_max_lifts(
            null_pool,
            baselines,
            target_float,
            permutations=permutations,
            seed=permutation_seed,
        )
        if permutations > 0 and null_pool
        else ()
    )
    threshold = (
        float(np.quantile(np.asarray(null_max_lifts, dtype=float), significance_quantile))
        if null_max_lifts
        else None
    )
    # A Jaccard below this cannot reach ``duplicate_correlation`` no matter how
    # the rest of the table falls out, so those pairs never pay for a count.
    #
    # The bound is ``sqrt(j)``, not ``2j / (1 + j)``: given a Jaccard of ``j``,
    # phi is maximised by pushing the "neither matched" cell to infinity and
    # letting one match set sit entirely inside the other, which lands on
    # ``sqrt(j)``. The symmetric ``2j / (1 + j)`` is only what you get when the
    # two sets are forced to be the same size - using it here is stricter than
    # the truth and silently skips nested pairs, which are the most common shape
    # a stricter threshold of the same indicator takes.
    prefilter = min(duplicate_correlation**2, _DUPLICATE_AGREEMENT)
    selected: list[MinedCondition] = []
    kept_matches: list[np.ndarray] = []
    kept_valid: list[np.ndarray] = []
    kept_bits: list[int] = []
    # Parallel to ``selected``: what each kept condition absorbed. Filled in
    # place because a candidate only learns its duplicates after the whole
    # ranked list has been walked.
    merged: list[list[DuplicateCondition]] = []
    for scored_candidate, matched, baseline_key in ranked:
        valid_mask, _valid_groups = baselines[baseline_key]
        valid_bits = bit_cache.setdefault(baseline_key, _bits(valid_mask))
        duplicate_of: int | None = None
        correlation = 0.0
        for index, kept in enumerate(kept_matches):
            overlap = _jaccard(matched, kept, valid=valid_mask, other_valid=kept_valid[index])
            if overlap < prefilter:
                continue
            value = _phi_correlation(
                matched,
                kept,
                valid=valid_mask,
                other_valid=kept_valid[index],
                valid_bits=valid_bits,
                other_valid_bits=kept_bits[index],
            )
            if value is None:
                # Undefined, not zero. Fall back to the plain overlap rule
                # rather than pretending the pair is independent.
                if overlap < _DUPLICATE_AGREEMENT:
                    continue
                value = 1.0
            if value >= duplicate_correlation or overlap >= _DUPLICATE_AGREEMENT:
                duplicate_of = index
                correlation = value
                break
        if duplicate_of is not None:
            merged[duplicate_of].append(
                DuplicateCondition(
                    condition=scored_candidate.condition,
                    correlation=correlation,
                    lift=scored_candidate.lift,
                    support_count=scored_candidate.support_count,
                )
            )
            continue
        selected.append(
            _with_significance(
                scored_candidate,
                threshold,
                null_max_lifts,
                quantile=significance_quantile,
            )
        )
        kept_matches.append(matched)
        kept_valid.append(valid_mask)
        kept_bits.append(valid_bits)
        merged.append([])
        if len(selected) == limit:
            break
    if stats is not None:
        # 计数口径：null_pool 是"结构可用"的 mask 全集（赢家和输家都算），
        # 不是最终返回的 limit 条候选。抬高噪声门槛要的是前者——输家也在
        # 参与"谁最幸运"的竞争，把它们排除会让零分布偏小、门槛偏松。
        stats["tested_conditions"] = len(null_pool)
    return tuple(
        replace(candidate, duplicates=tuple(items)) for candidate, items in zip(selected, merged)
    )


def _has_two_distinct(values: np.ndarray, finite_mask: np.ndarray) -> bool:
    finite_values = values[finite_mask]
    return finite_values.size >= 2 and np.unique(finite_values).size >= 2


def _top_n_diagnosis(
    matched_indices: np.ndarray,
    outcomes: np.ndarray,
    context: TopNContext | None,
    sample_positions: np.ndarray | None = None,
) -> tuple[float | None, int, int]:
    """Re-score matched samples the way a slot-limited portfolio would.

    Returns ``(outcome_rate, support_count, filtered_days)``. ``filtered_days``
    counts signal days where ranking actually had to drop something - when it is
    zero the top-N split changed nothing, and the caller must say so instead of
    showing a column that silently duplicates the pool-wide rate.

    ``matched_indices`` live in the candidate's compressed sample space (rows
    where the mined feature is finite). The context is indexed by full-sample
    position, so ``sample_positions`` (``flatnonzero(valid)``) must map them
    back; skipping that step silently reads the wrong rows.
    """

    if context is None or matched_indices.size == 0:
        return None, 0, 0
    positions = matched_indices if sample_positions is None else sample_positions[matched_indices]
    score = context.values[positions]
    usable = np.flatnonzero(np.isfinite(score))
    if usable.size == 0:
        return None, 0, 0
    day = context.day_codes[positions][usable]
    score = score[usable]
    day_outcomes = outcomes[matched_indices][usable]
    # Sort by day, then by rank quality: a running counter then yields each
    # sample's position within its own signal day.
    key = score if context.ascending else -score
    order = np.lexsort((key, day))
    sorted_day = day[order]
    is_new_day = np.concatenate(([True], sorted_day[1:] != sorted_day[:-1]))
    starts = np.flatnonzero(is_new_day)
    group_sizes = np.diff(np.append(starts, sorted_day.size))
    group_index = np.cumsum(is_new_day) - 1
    rank_in_group = np.arange(sorted_day.size) - starts[group_index]
    chosen = order[rank_in_group < context.top_n]
    if chosen.size == 0:
        return None, 0, 0
    return (
        float(np.mean(day_outcomes[chosen])),
        int(chosen.size),
        int(np.sum(group_sizes > context.top_n)),
    )


def _candidate_for_mask(
    condition: ScreenCondition,
    condition_mask: np.ndarray,
    outcomes: np.ndarray,
    instrument_codes: np.ndarray,
    group_codes: np.ndarray,
    *,
    min_support: int,
    min_instruments: int,
    baseline_rate: float,
    top_n_context: TopNContext | None = None,
    sample_positions: np.ndarray | None = None,
) -> tuple[MinedCondition | None, np.ndarray | None]:
    """Score one mask, returning the candidate and the global rows it matched.

    ``sample_positions`` maps the compressed indices back to full-sample
    positions; the returned rows are global so a permutation test can rescore
    the same match set against shuffled labels.

    ``matched_global`` stays populated for every structurally usable mask even
    when ``candidate`` is None because the mask lost on the real labels. That
    distinction is what keeps the permutation null honest: dropping
    losers from the pool would shrink it to the masks that already got lucky,
    and the maximum over a smaller pool is smaller — which quietly loosens the
    threshold into the very false positives the test exists to reject.
    """

    order = np.flatnonzero(condition_mask)
    if order.size == 0:
        # Never true: it cannot produce a single trade.
        return None, None
    if order.size == outcomes.size:
        # Always true: an AND with it cannot change any trade.
        return None, None
    # A group contributes at most one sample: its first matching row.
    # np.unique(..., return_index=True) reports that first occurrence, and
    # sorting restores the ascending order the previous dict loop produced.
    _, first_positions = np.unique(group_codes[order], return_index=True)
    matched_indices = np.sort(order[first_positions])
    # uint32, not intp: every match set is stored per candidate, and with tens
    # of thousands of samples the pointer-sized indices were the single largest
    # allocation in a full-market study. Sample counts never approach 2**32.
    matched_global = np.asarray(
        matched_indices if sample_positions is None else sample_positions[matched_indices],
        dtype=np.uint32,
    )
    support_count = int(matched_indices.size)
    if support_count < min_support:
        return None, None
    outcome_rate = float(np.mean(outcomes[matched_indices]))
    if outcome_rate <= baseline_rate:
        # Lost on the labels only — the mask is still a fair draw under the null.
        return None, matched_global
    instrument_count = int(np.unique(instrument_codes[matched_indices]).size)
    if instrument_count < min_instruments:
        return None, None
    group_count = int(np.unique(group_codes).size)
    top_n_rate, top_n_support, filtered_days = _top_n_diagnosis(
        matched_indices, outcomes, top_n_context, sample_positions
    )
    scored = MinedCondition(
        condition=condition,
        support_count=support_count,
        support_ratio=float(support_count / group_count),
        instrument_count=instrument_count,
        outcome_rate=outcome_rate,
        baseline_rate=baseline_rate,
        lift=float(outcome_rate / baseline_rate),
        top_n_outcome_rate=top_n_rate,
        top_n_support_count=top_n_support,
        top_n_filtered_days=filtered_days,
    )
    return scored, matched_global


def _grouped_outcome_rate(outcomes: np.ndarray, group_codes: np.ndarray) -> float:
    """Equal-weight mean of per-group outcome rates.

    Encoded with ``bincount`` so no Python loop runs per candidate. Group codes
    come from ``pd.factorize``, i.e. first-appearance order, so the summation
    order matches the previous dict-based implementation.
    """

    if group_codes.size == 0:
        return float("nan")
    size = int(group_codes.max()) + 1
    counts = np.bincount(group_codes, minlength=size)
    wins = np.bincount(group_codes, weights=outcomes, minlength=size)
    present = counts > 0
    return float(np.mean(wins[present] / counts[present]))


def _condition_key(
    condition: ScreenCondition,
) -> tuple[str, str, str | None, float | None]:
    return (
        condition.left_value,
        str(condition.operator),
        condition.right_value,
        condition.right_constant,
    )
