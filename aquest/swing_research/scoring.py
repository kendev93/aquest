"""Score indicators against swing labels without fooling ourselves.

Two things make a naive version of this study produce nothing but false
positives, and both are handled here:

**Overlapping daily samples.** A panel of 5,000 stocks over 1,200 days looks
like six million observations, but the stocks share the same market clock: if
every stock is about to bounce on Tuesday, then Wednesday's labels are mostly
the same story. The independent unit is the *trading day*, not the cell. So the
statistic is built as a daily cross-sectional information coefficient (rank
correlation across all stocks on that day) and only then averaged - the
resulting IC series has one point per day and can be tested honestly.

**Permutations that respect time.** Shuffling individual cells, as a
per-sample test would, destroys the day-to-day dependence that inflated the
sample size in the first place, so the null distribution comes out far too
narrow and everything looks significant. Shuffling whole *dates* instead keeps
each day's cross-section intact - and shuffling them in contiguous blocks
keeps the neighbouring-day dependence too, which is the part that actually
bites.

The permutation is nearly free: the daily covariance between every pair of
dates is one Gram matrix ``G``, so a permuted run is just ``G[t, pi[t]]``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np

MIN_CROSS_SECTION = 20
MIN_IC_DAYS = 60
DEFAULT_BLOCK_SIZE = 20
DEFAULT_PERMUTATIONS = 200
# 块置换能给出的最小 p 值约等于 1/k!（k 是块数，另加 (1+m)/(1+count) 里那个 1）。
# k=2 时下界是 0.5，k=3 是 0.17——信号再强也压不到 alpha 下面，检验等于没有功效，
# 而页面会把它读成「无法排除偶然」。k=5 的下界约 0.01，才留得出余量。
# 所以样本不够长时宁可把块切短（仍保持连续），也不给一个注定不显著的 p 值。
MIN_PERMUTATION_BLOCKS = 5
DEFAULT_TOP_QUANTILE = 0.2
WEAK_IC = 0.02
STRONG_IC = 0.05
STABLE_ICIR = 0.3
DEFAULT_SEED = 20240918

ScoreOrder = Literal["high", "low"]


@dataclass(frozen=True)
class IndicatorScore:
    """One indicator, measured against one label."""

    value_id: str
    display_name: str
    label: str
    order: ScoreOrder
    ic_mean: float
    icir: float | None
    ic_days: int
    ic_threshold: float | None
    #: 头桶命中率 / 全样本命中率，是**倍数**（1.0 = 与全样本持平）。
    #: 条件研究里那个 `top_n_lift` 是差值、不是倍数，两者别共用格式化。
    top_lift: float | None
    threshold_hint: float | None
    pvalue: float | None
    qvalue: float | None
    significant: bool
    note: str

    def to_dict(self) -> dict[str, object]:
        return {
            "value_id": self.value_id,
            "display_name": self.display_name,
            "label": self.label,
            "order": self.order,
            # JSON has no NaN/Infinity values. Empty samples keep NaN internally
            # for ranking/diagnostics, but the wire format must use null.
            "ic_mean": float(self.ic_mean) if np.isfinite(self.ic_mean) else None,
            "icir": self.icir,
            "ic_days": self.ic_days,
            "ic_threshold": self.ic_threshold,
            "top_lift": self.top_lift,
            "threshold_hint": self.threshold_hint,
            "pvalue": self.pvalue,
            "qvalue": self.qvalue,
            "significant": self.significant,
            "note": self.note,
        }


def tie_average_rank(values: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Cross-sectional ranks per column, averaged over ties.

    Masked cells are excluded from the ranking and given rank 0. Ties matter
    more than they look: a binary label has exactly two distinct values, so
    without averaging, its "ranks" would be an arbitrary ordering of thousands
    of zeros and ones rather than the two levels it actually has.
    """

    rows, cols = values.shape
    if rows == 0 or cols == 0:
        return np.zeros_like(values, dtype=float)
    filled = np.where(mask, values, np.inf)
    order = np.argsort(filled, axis=0, kind="mergesort")
    ordered = np.take_along_axis(filled, order, axis=0)
    positions = np.arange(rows, dtype=float)[:, None]

    is_first = np.empty((rows, cols), dtype=bool)
    is_first[0, :] = True
    if rows > 1:
        is_first[1:, :] = ordered[1:, :] != ordered[:-1, :]
    is_last = np.empty((rows, cols), dtype=bool)
    is_last[-1, :] = True
    if rows > 1:
        is_last[:-1, :] = ordered[:-1, :] != ordered[1:, :]

    # A group's span is "nearest start at or before me" and "nearest end at or
    # after me", which is a forward running maximum and a backward running
    # minimum respectively. Getting either direction wrong quietly merges every
    # tie in the column into one giant group.
    starts = np.maximum.accumulate(np.where(is_first, positions, -np.inf), axis=0)
    ends = np.minimum.accumulate((np.where(is_last, positions, np.inf))[::-1], axis=0)[::-1]
    averaged = (starts + ends) / 2.0

    ranks = np.empty((rows, cols), dtype=float)
    np.put_along_axis(ranks, order, averaged, axis=0)
    return np.where(mask, ranks, 0.0)


def _centered(ranks: np.ndarray, mask: np.ndarray, counts: np.ndarray) -> np.ndarray:
    """Subtract each column's mean rank, zeroing cells with no data."""

    safe = np.where(counts > 0, counts, 1)
    means = (ranks * mask).sum(axis=0) / safe
    return np.where(mask, ranks - means, 0.0)


def effective_block_size(day_count: int, block_size: int) -> int:
    """块置换真正会用的块长。

    样本不够长时把块切短，好让块数达到 :data:`MIN_PERMUTATION_BLOCKS`。单独抽出来
    是因为调用方（因子归因要把它报给用户）和 :func:`block_permutations` 必须用同一个
    数——各算一遍迟早会长出两个结果。
    """

    size = max(1, int(block_size))
    if day_count <= 0:
        return size
    if day_count // size < MIN_PERMUTATION_BLOCKS:
        return max(1, day_count // MIN_PERMUTATION_BLOCKS)
    return size


def block_permutations(
    day_count: int,
    *,
    block_size: int = DEFAULT_BLOCK_SIZE,
    count: int = DEFAULT_PERMUTATIONS,
    seed: int = DEFAULT_SEED,
) -> list[np.ndarray]:
    """Shuffle the calendar in contiguous blocks, not day by day.

    Reordering individual days would leave neighbouring-day dependence in the
    *labels* intact while breaking the part that makes the test honest; moving
    whole months keeps that dependence and asks only whether the indicator
    lines up with the labels at all.
    """

    if day_count <= 0 or count <= 0:
        return []
    size = effective_block_size(day_count, block_size)
    blocks = [np.arange(start, min(start + size, day_count)) for start in range(0, day_count, size)]
    rng = np.random.default_rng(seed)
    permutations: list[np.ndarray] = []
    for _ in range(count):
        order = rng.permutation(len(blocks))
        permutations.append(np.concatenate([blocks[index] for index in order]))
    return permutations


def benjamini_hochberg(pvalues: Sequence[float | None]) -> list[float | None]:
    """Benjamini-Hochberg FDR adjustment; ``None`` in, ``None`` out.

    With 31 indicators times 4 labels the best p-value is essentially
    guaranteed to look impressive, so a raw threshold would select noise.
    """

    known = [(index, float(item)) for index, item in enumerate(pvalues) if item is not None]
    result: list[float | None] = [None] * len(pvalues)
    if not known:
        return result
    total = len(known)
    ordered = sorted(known, key=lambda pair: pair[1])
    adjusted: list[float] = []
    running = 1.0
    for position in range(total - 1, -1, -1):
        _, value = ordered[position]
        running = min(running, value * total / (position + 1))
        adjusted.append(running)
    adjusted.reverse()
    for (index, _), value in zip(ordered, adjusted):
        result[index] = min(1.0, max(0.0, value))
    return result


def score_indicator(
    values: np.ndarray,
    labels: np.ndarray,
    *,
    value_id: str,
    display_name: str,
    label: str,
    min_cross_section: int = MIN_CROSS_SECTION,
    block_size: int = DEFAULT_BLOCK_SIZE,
    permutations: int = DEFAULT_PERMUTATIONS,
    top_quantile: float = DEFAULT_TOP_QUANTILE,
    seed: int = DEFAULT_SEED,
    alpha: float = 0.05,
) -> IndicatorScore:
    """Measure one indicator panel against one label panel.

    ``values`` and ``labels`` are ``(stocks, days)`` with NaN where a cell is
    unusable - a stock not listed yet, an indicator still warming up, or a
    label whose future window does not exist.
    """

    matrix = np.asarray(values, dtype=float)
    target = np.asarray(labels, dtype=float)
    mask = np.isfinite(matrix) & np.isfinite(target)
    empty = IndicatorScore(
        value_id=value_id,
        display_name=display_name,
        label=label,
        order="high",
        ic_mean=float("nan"),
        icir=None,
        ic_days=0,
        ic_threshold=None,
        top_lift=None,
        threshold_hint=None,
        pvalue=None,
        qvalue=None,
        significant=False,
        note="没有任何可用的横截面样本",
    )
    if matrix.size == 0 or not mask.any():
        return empty

    counts = mask.sum(axis=0).astype(float)
    usable = counts >= max(2, min_cross_section)
    value_rank = tie_average_rank(matrix, mask)
    label_rank = tie_average_rank(target, mask)
    left = _centered(value_rank, mask, counts)
    right = _centered(label_rank, mask, counts)

    row_norms = (left * left).sum(axis=0)
    col_norms = (right * right).sum(axis=0)
    overlap = mask.astype(float).T @ mask.astype(float)
    gram = left.T @ right

    # r[t, u] is the rank correlation between day t's indicators and day u's
    # labels, so the observed series is the diagonal and a permuted run reads
    # off the permutation instead.
    safe = np.where(counts > 0, counts, 1)
    # Variances, not sums of squares: a day with twice the stocks is not twice
    # as correlated, and leaving the counts out silently shrinks every IC by
    # roughly the cross-section size.
    row_scale = row_norms / safe
    col_scale = col_norms / safe
    with np.errstate(divide="ignore", invalid="ignore"):
        shared = np.where(overlap > 0, overlap, np.nan)
        scale = np.sqrt(np.outer(row_scale, col_scale))
        scale = np.where(scale > 0, scale, np.nan)
        correlation = (gram / shared) / scale
    diagonal = np.where(usable, np.diag(correlation), np.nan)
    finite = np.isfinite(diagonal)
    if not finite.any():
        return empty

    ic_mean = float(np.nanmean(diagonal))
    ic_std = float(np.nanstd(diagonal, ddof=1)) if int(finite.sum()) > 1 else 0.0
    icir = float(ic_mean / ic_std) if ic_std > 1e-12 else None

    pvalue: float | None = None
    threshold: float | None = None
    if permutations > 0:
        observed = abs(ic_mean)
        exceed = 0
        shuffled: list[float] = []
        for permutation in block_permutations(
            diagonal.size, block_size=block_size, count=permutations, seed=seed
        ):
            shifted = np.where(usable, correlation[np.arange(diagonal.size), permutation], np.nan)
            if not np.isfinite(shifted).any():
                continue
            magnitude = abs(float(np.nanmean(shifted)))
            shuffled.append(magnitude)
            if magnitude >= observed:
                exceed += 1
        if shuffled:
            # Invalid date pairings are skipped above. Use the number that
            # actually formed a null statistic, or the p-value is biased low.
            pvalue = (1 + exceed) / (1 + len(shuffled))
            threshold = float(np.quantile(np.asarray(shuffled), 0.95))

    order: ScoreOrder = "high" if ic_mean >= 0 else "low"
    lift, hint = _top_bucket_evidence(
        matrix,
        target,
        mask,
        counts,
        usable,
        high=order == "high",
        top_quantile=top_quantile,
    )

    note = _note(
        ic_days=int(finite.sum()),
        ic_mean=abs(ic_mean),
        icir=abs(icir) if icir is not None else None,
        ic_threshold=threshold,
        lift=lift,
        pvalue=pvalue,
        alpha=alpha,
    )
    return IndicatorScore(
        value_id=value_id,
        display_name=display_name,
        label=label,
        order=order,
        ic_mean=abs(ic_mean),
        icir=abs(icir) if icir is not None else None,
        ic_days=int(finite.sum()),
        ic_threshold=threshold,
        top_lift=lift,
        threshold_hint=hint,
        pvalue=pvalue,
        qvalue=None,
        significant=False,
        note=note,
    )


def _top_bucket_evidence(
    values: np.ndarray,
    labels: np.ndarray,
    mask: np.ndarray,
    counts: np.ndarray,
    usable: np.ndarray,
    *,
    high: bool,
    top_quantile: float,
) -> tuple[float | None, float | None]:
    """Hit-rate ratio of the extreme bucket, plus the value that defines it."""

    share = min(max(top_quantile, 0.01), 0.5)
    ordered = np.sort(np.where(mask, values, np.nan), axis=0)
    rows, days = ordered.shape
    # 分位点必须按"当天有效样本数"取，不能按总行数：np.sort 把缺失值排到列尾，
    # 用总行数算下标时，稀疏日（100 行里只有 20 个有效值）会取到 NaN，于是
    # threshold_hint 变 None，conditions.py 直接跳过这个指标、条件计划生成不出来。
    # 下面入选用的 boundary 本来就是按 counts 算的，两边必须一致。
    cuts = np.clip(np.rint(np.asarray(counts, dtype=float) * share), 1, counts)
    edge = np.where(high, counts - cuts, cuts - 1)
    edge = np.clip(edge, 0, max(rows - 1, 0)).astype(int)
    hints = ordered[edge, np.arange(days)]

    rank = tie_average_rank(values, mask)
    if high:
        boundary = counts * (1.0 - share)
        selected = mask & (rank >= boundary - 1e-9)
    else:
        boundary = counts * share
        selected = mask & (rank <= boundary + 1e-9)

    selected_hits = np.where(selected, labels, 0.0).sum(axis=0)
    selected_count = selected.sum(axis=0)
    all_hits = np.where(mask, labels, 0.0).sum(axis=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        selected_rate = selected_hits / np.where(selected_count > 0, selected_count, np.nan)
        base_rate = all_hits / np.where(counts > 0, counts, np.nan)
        daily = selected_rate / np.where(base_rate > 1e-12, base_rate, np.nan)
    daily = np.where(usable, daily, np.nan)
    if not np.isfinite(daily).any():
        return None, None
    hint_values = np.where(usable, hints, np.nan)
    hint = float(np.nanmedian(hint_values)) if np.isfinite(hint_values).any() else None
    return float(np.nanmean(daily)), hint


def _note(
    *,
    ic_days: int,
    ic_mean: float,
    icir: float | None,
    ic_threshold: float | None,
    lift: float | None,
    pvalue: float | None,
    alpha: float,
) -> str:
    parts = [f"平均信息系数 {ic_mean:.3f}"]
    parts.append(f"信息比率 {icir:.2f}" if icir is not None else "信息系数几乎无波动")
    if lift is not None:
        parts.append(f"首尾 20% 命中率 {lift:.2f} 倍")
    text = "、".join(parts) + f"，基于 {ic_days} 个交易日"
    # The p-value alone is easy to over-trust: with a long series almost
    # anything clears it. Saying how big the noise floor is lets the reader see
    # whether the effect is ten times the noise or merely twice.
    if ic_threshold is not None:
        ratio = ic_mean / ic_threshold if ic_threshold > 1e-12 else float("inf")
        text += f"；噪声门槛 {ic_threshold:.3f}（实测为其 {ratio:.1f} 倍）"
    if ic_days < MIN_IC_DAYS:
        return f"{text}；有效交易日不足 {MIN_IC_DAYS} 天，结论不可靠"
    if pvalue is None:
        return f"{text}；未做噪声门槛检验"
    if pvalue > alpha:
        return f"{text}；噪声门槛检验 p={pvalue:.3f}，无法排除偶然"
    if ic_mean < WEAK_IC:
        return f"{text}；虽然通过检验，但系数低于 {WEAK_IC:.2f}，实战意义有限"
    if icir is not None and icir < STABLE_ICIR:
        return f"{text}；通过检验但稳定性偏弱"
    if ic_mean >= STRONG_IC:
        return f"{text}；检验通过（p={pvalue:.3f}）且强度可观"
    return f"{text}；检验通过（p={pvalue:.3f}）"


def annotate_significance(
    scores: list[IndicatorScore],
    *,
    alpha: float = 0.05,
) -> list[IndicatorScore]:
    """Apply FDR control across the whole study and mark what survives.

    Called once for every indicator/label pair together, never per pair: the
    multiplicity comes from having looked at all of them, so the correction has
    to see all of them.
    """

    qvalues = benjamini_hochberg([item.pvalue for item in scores])
    adjusted: list[IndicatorScore] = []
    for score, qvalue in zip(scores, qvalues):
        significant = qvalue is not None and qvalue <= alpha
        note = score.note
        if significant and "检验通过" in note:
            note = f"{note}，多重检验校正后 q={qvalue:.3f}"
        elif qvalue is not None and score.pvalue is not None and score.pvalue <= alpha:
            note = f"{note}；多重检验校正后 q={qvalue:.3f}，未通过"
        adjusted.append(
            IndicatorScore(
                value_id=score.value_id,
                display_name=score.display_name,
                label=score.label,
                order=score.order,
                ic_mean=score.ic_mean,
                icir=score.icir,
                ic_days=score.ic_days,
                ic_threshold=score.ic_threshold,
                top_lift=score.top_lift,
                threshold_hint=score.threshold_hint,
                pvalue=score.pvalue,
                qvalue=qvalue,
                significant=significant,
                note=note,
            )
        )
    return adjusted


__all__ = [
    "DEFAULT_BLOCK_SIZE",
    "DEFAULT_PERMUTATIONS",
    "DEFAULT_TOP_QUANTILE",
    "IndicatorScore",
    "ScoreOrder",
    "annotate_significance",
    "benjamini_hochberg",
    "block_permutations",
    "effective_block_size",
    "score_indicator",
    "tie_average_rank",
]
