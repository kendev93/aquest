"""Interpretable contrastive rule mining over completed trade samples."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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
# for the study; only the best-ranked one is reported.
_DUPLICATE_AGREEMENT = 0.995
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


def _masks_agree(
    left: np.ndarray,
    right: np.ndarray,
    *,
    threshold: float = _DUPLICATE_AGREEMENT,
) -> bool:
    """True when two candidate match sets select effectively the same samples.

    Both arguments are sorted arrays of sample positions. Overlap is measured
    as ``intersection / union`` over those positions, which is the same ratio
    the earlier boolean-mask version computed on the rows where either matched.
    """

    union = np.union1d(left, right)
    if union.size == 0:
        return False
    intersection = np.intersect1d(left, right)
    return float(intersection.size / union.size) >= threshold


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
    selected: list[MinedCondition] = []
    kept_matches: list[np.ndarray] = []
    for scored_candidate, matched, _baseline_key in ranked:
        if any(_masks_agree(matched, kept) for kept in kept_matches):
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
        if len(selected) == limit:
            break
    return tuple(selected)


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
