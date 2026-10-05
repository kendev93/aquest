"""Score one value panel against forward returns.

The statistic is built the same way the swing study builds its own, for the
same reason: a panel of five thousand stocks over a thousand days looks like
five million observations, but the stocks share one market clock. If every
stock is about to bounce on Tuesday then Wednesday's labels are mostly the
same story, so the independent unit is the *trading day*, not the cell. The
value is ranked against the return within each day and only then aggregated.

What differs from the swing study is the evidence around the correlation.
There the label is an event, so the natural summary is a hit-rate ratio; here
the label is a return that can be negative, and a ratio of two negative means
reads as "three times worse" when it is in fact "loses less". So the evidence
is a quantile breakdown plus the long/short spread it implies - both are
differences, never ratios.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np

from selector_app.swing_research.scoring import (
    benjamini_hochberg,
    block_permutations,
    tie_average_rank,
)

from .models import (
    CLEAR_IC_RATIO,
    COINFLIP_WIN_RATE,
    DEFAULT_SEED,
    LAYER_INVERSION_TOLERANCE,
    MIN_IC_DAYS,
    STABLE_ICIR,
    STRONG_IC,
    WEAK_IC,
    FactorTestConfig,
)

Order = Literal["high", "low"]


@dataclass(frozen=True)
class LayerStat:
    """One quantile bucket of the cross-section."""

    #: 1-based, and already oriented: layer 1 is the group you would buy.
    index: int
    #: Average number of stocks per day in this bucket.
    size: float | None
    mean_return: float | None
    #: Share of days this bucket beat the whole cross-section's own mean.
    beat_market_rate: float | None

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "size": self.size,
            "mean_return": self.mean_return,
            "beat_market_rate": self.beat_market_rate,
        }


@dataclass(frozen=True)
class LongShortStat:
    """Daily top-layer minus bottom-layer spread, as a series and as facts."""

    mean_spread: float | None
    std: float | None
    t_stat: float | None
    win_rate: float | None
    final: float | None
    max_drawdown: float | None
    days: int
    cumulative: tuple[float, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "mean_spread": self.mean_spread,
            "std": self.std,
            "t_stat": self.t_stat,
            "win_rate": self.win_rate,
            "final": self.final,
            "max_drawdown": self.max_drawdown,
            "days": self.days,
            "cumulative": list(self.cumulative),
        }


@dataclass(frozen=True)
class FactorEvidence:
    """One value, measured against forward returns."""

    value_id: str
    display_name: str
    order: Order
    ic_mean: float
    icir: float | None
    ic_days: int
    ic_positive_rate: float | None
    ic_series: tuple[float | None, ...]
    ic_cumulative: tuple[float | None, ...]
    ic_first_half: float | None
    ic_second_half: float | None
    layers: tuple[LayerStat, ...]
    layer_inversions: int
    long_short: LongShortStat
    ic_threshold: float | None
    pvalue: float | None
    qvalue: float | None
    significant: bool
    note: str

    def to_dict(self) -> dict[str, object]:
        return {
            "value_id": self.value_id,
            "display_name": self.display_name,
            "order": self.order,
            # JSON has no NaN/Infinity values. Empty samples keep NaN internally
            # for sorting, but the wire format must use null.
            "ic_mean": float(self.ic_mean) if np.isfinite(self.ic_mean) else None,
            "icir": self.icir,
            "ic_days": self.ic_days,
            "ic_positive_rate": self.ic_positive_rate,
            "ic_series": list(self.ic_series),
            "ic_cumulative": list(self.ic_cumulative),
            "ic_first_half": self.ic_first_half,
            "ic_second_half": self.ic_second_half,
            "layers": [layer.to_dict() for layer in self.layers],
            "layer_inversions": self.layer_inversions,
            "long_short": self.long_short.to_dict(),
            "ic_threshold": self.ic_threshold,
            "pvalue": self.pvalue,
            "qvalue": self.qvalue,
            "significant": self.significant,
            "note": self.note,
        }


def _centered(ranks: np.ndarray, mask: np.ndarray, counts: np.ndarray) -> np.ndarray:
    """Subtract each column's mean rank, zeroing cells with no data."""

    safe = np.where(counts > 0, counts, 1)
    means = (ranks * mask).sum(axis=0) / safe
    return np.where(mask, ranks - means, 0.0)


def correlation_gram(
    values: np.ndarray,
    labels: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Every day-to-day rank correlation, plus which days are usable.

    ``gram[t, u]`` is the rank correlation between day ``t``'s values and day
    ``u``'s returns. The observed IC series is its diagonal, and a permutation
    of the calendar is just another set of cells - which is what makes a
    block-shuffled null affordable: without this matrix, two hundred
    permutations would be two hundred thousand separate correlations.
    """

    matrix = np.asarray(values, dtype=float)
    target = np.asarray(labels, dtype=float)
    mask = np.isfinite(matrix) & np.isfinite(target)
    counts = mask.sum(axis=0).astype(float)
    value_rank = tie_average_rank(matrix, mask)
    label_rank = tie_average_rank(target, mask)
    left = _centered(value_rank, mask, counts)
    right = _centered(label_rank, mask, counts)
    row_norms = (left * left).sum(axis=0)
    col_norms = (right * right).sum(axis=0)
    overlap = mask.astype(float).T @ mask.astype(float)
    gram = left.T @ right
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
    return correlation, counts, mask


def _permutation_test(
    correlation: np.ndarray,
    usable: np.ndarray,
    *,
    observed: float,
    block_size: int,
    permutations: int,
    seed: int,
) -> tuple[float | None, float | None]:
    """Block-shuffled null for the mean IC; returns (p-value, noise floor).

    Whole contiguous blocks of days move together. Shuffling single days would
    leave the neighbouring-day dependence in the labels intact while breaking
    the part that makes the test honest, and shuffling cells - as a per-sample
    test would - destroys the dependence that inflated the sample size in the
    first place, so the null comes out far too narrow and everything looks
    significant.
    """

    if permutations <= 0:
        return None, None
    size = correlation.shape[0]
    magnitudes: list[float] = []
    exceed = 0
    target = abs(observed)
    indices = np.arange(size)
    for permutation in block_permutations(
        size, block_size=block_size, count=permutations, seed=seed
    ):
        shifted = np.where(usable, correlation[indices, permutation], np.nan)
        if not np.isfinite(shifted).any():
            continue
        magnitude = abs(float(np.nanmean(shifted)))
        magnitudes.append(magnitude)
        if magnitude >= target:
            exceed += 1
    if not magnitudes:
        return None, None
    # Invalid calendar pairings are omitted from magnitudes; don't count them
    # as non-extreme observations in the Monte Carlo denominator.
    pvalue = (1 + exceed) / (1 + len(magnitudes))
    threshold = float(np.quantile(np.asarray(magnitudes), 0.95))
    return pvalue, threshold


def _daily_layer_returns(
    values: np.ndarray,
    labels: np.ndarray,
    mask: np.ndarray,
    counts: np.ndarray,
    usable: np.ndarray,
    *,
    order: Order,
    quantile_count: int,
) -> np.ndarray:
    """Mean forward return per quantile bucket, one row per bucket per day."""

    rows, days = values.shape
    # tie_average_rank is 0-based, so the largest value in a column sits at
    # ``counts - 1``. Layer 1 is the group the IC says to buy, which is the
    # largest end when the IC is positive - getting this backwards produces a
    # perfectly monotonic ladder pointing the wrong way, which is worse than no
    # ladder at all because it still looks like a result.
    rank = tie_average_rank(values, mask)
    safe = np.where(counts > 1, counts - 1.0, 1.0)
    position = np.clip(rank / safe, 0.0, 1.0)
    bucket = np.clip(np.floor(position * quantile_count), 0, quantile_count - 1).astype(int)
    if order == "high":
        bucket = quantile_count - 1 - bucket

    result = np.full((quantile_count, days), np.nan, dtype=float)
    for index in range(quantile_count):
        selected = mask & (bucket == index)
        totals = np.where(selected, labels, 0.0).sum(axis=0)
        members = selected.sum(axis=0)
        with np.errstate(divide="ignore", invalid="ignore"):
            means = totals / np.where(members > 0, members, np.nan)
        result[index] = np.where(usable, means, np.nan)
    return result


def _layers(
    daily: np.ndarray,
    market_means: np.ndarray,
) -> tuple[tuple[LayerStat, ...], int]:
    """Aggregate the per-day bucket returns, and count monotonicity breaks."""

    stats: list[LayerStat] = []
    for index in range(daily.shape[0]):
        series = daily[index]
        finite = np.isfinite(series)
        mean_return = float(np.mean(series[finite])) if finite.any() else None
        beat = None
        comparable = finite & np.isfinite(market_means)
        if comparable.any():
            beat = float(np.mean(series[comparable] > market_means[comparable]))
        stats.append(
            LayerStat(
                index=index + 1,
                size=None,
                mean_return=mean_return,
                beat_market_rate=beat,
            )
        )
    ordered = [item.mean_return for item in stats if item.mean_return is not None]
    # Tolerance is a fraction of the ladder's own span. Counting every upward
    # tick flags a flat ladder as "收益挤在极端档" - on real data neighbouring
    # layers differ by less than a basis point, and a warning that fires on
    # noise is worse than no warning because it still looks like a finding.
    span = (max(ordered) - min(ordered)) if len(ordered) > 1 else 0.0
    tolerance = max(1e-12, span * LAYER_INVERSION_TOLERANCE)
    inversions = sum(1 for first, second in zip(ordered, ordered[1:]) if first + tolerance < second)
    return tuple(stats), inversions


def _long_short(daily: np.ndarray, horizon: int) -> LongShortStat:
    """Top bucket minus bottom bucket, summed rather than compounded.

    Compounding would invent a track record: this is a daily-rebalanced spread
    between two buckets of overlapping h-day returns, not an equity curve. The
    t-statistic divides the sample size by h for the same reason - overlapping
    windows are not independent observations, and pretending otherwise inflates
    every conclusion drawn from them.
    """

    spread = daily[0] - daily[-1] if daily.shape[0] > 1 else np.array([], dtype=float)
    finite = np.isfinite(spread)
    days = int(finite.sum())
    empty = LongShortStat(
        mean_spread=None,
        std=None,
        t_stat=None,
        win_rate=None,
        final=None,
        max_drawdown=None,
        days=0,
        cumulative=(),
    )
    if days == 0:
        return empty
    series = spread[finite]
    mean_spread = float(np.mean(series))
    std = float(np.std(series, ddof=1)) if days > 1 else 0.0
    effective = max(1, days // max(1, horizon))
    t_stat = float(mean_spread / (std / np.sqrt(effective))) if std > 1e-12 else None
    cumulative = np.cumsum(np.where(finite, spread, 0.0))
    running_max = np.maximum.accumulate(cumulative)
    drawdown = float(np.max(running_max - cumulative)) if cumulative.size else None
    return LongShortStat(
        mean_spread=mean_spread,
        std=std,
        t_stat=t_stat,
        win_rate=float(np.mean(series > 0.0)),
        final=float(cumulative[-1]) if cumulative.size else None,
        max_drawdown=drawdown,
        days=days,
        cumulative=tuple(float(item) for item in cumulative),
    )


def _halves(ic_series: np.ndarray, usable: np.ndarray) -> tuple[float | None, float | None]:
    """Mean IC over the first and second half of the calendar.

    Stability evidence, not an out-of-sample test: the direction was chosen on
    the whole sample, so agreement between halves only says one stretch was not
    carrying the result alone.

    Signed, because "the halves disagree" is a statement about signs and cannot
    be recovered from magnitudes. The caller takes the absolute value for
    display, where the sign has already been spent choosing ``order``.
    """

    size = ic_series.size
    if size < 2:
        return None, None
    middle = size // 2
    first = ic_series[:middle][usable[:middle]]
    second = ic_series[middle:][usable[middle:]]
    first_mean = float(np.mean(first)) if np.isfinite(first).any() else None
    second_mean = float(np.mean(second)) if np.isfinite(second).any() else None
    return first_mean, second_mean


def _note(
    *,
    ic_days: int,
    ic_mean: float,
    icir: float | None,
    ic_threshold: float | None,
    spread: float | None,
    win_rate: float | None,
    inversions: int,
    first_half: float | None,
    second_half: float | None,
    pvalue: float | None,
    alpha: float,
) -> str:
    parts = [f"平均信息系数 {ic_mean:.3f}"]
    parts.append(f"信息比率 {icir:.2f}" if icir is not None else "信息系数几乎无波动")
    if spread is not None:
        parts.append(f"首尾档日均价差 {spread * 100:+.3f}%")
    if win_rate is not None:
        parts.append(f"价差为正的天数占 {win_rate * 100:.0f}%")
    text = "、".join(parts) + f"，基于 {ic_days} 个交易日"
    if ic_threshold is not None:
        ratio = ic_mean / ic_threshold if ic_threshold > 1e-12 else float("inf")
        text += f"；噪声门槛 {ic_threshold:.3f}（实测为其 {ratio:.1f} 倍）"
    if inversions:
        text += f"；分层收益有 {inversions} 处倒挂，收益可能只挤在极端那一档"
    if first_half is not None and second_half is not None and first_half * second_half > 0:
        text += "；前后半段同向，不是某一段独撑"
    elif first_half is not None and second_half is not None and first_half * second_half < 0:
        text += "；前后半段方向相反，结论依赖区间选择"
    # A ranking can be real and still be untradeable: on real data the daily
    # top-minus-bottom spread wins barely more often than a coin even when the
    # IC clears its noise floor, because the spread is a difference of two noisy
    # bucket means. Without this line the "显著" badge reads as money.
    if win_rate is not None and win_rate < COINFLIP_WIN_RATE:
        text += f"；价差为正的天数仅占 {win_rate * 100:.0f}%，接近抛硬币，扣掉成本未必可执行"
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
    # "强度可观" is reserved for an effect that clears the noise floor by a
    # margin. A p-value just under alpha with a ratio near 1.0 is the same fact
    # seen from the other side: it passed, but barely - and saying "strength"
    # there is exactly the overclaim this whole page exists to avoid.
    if ic_mean >= STRONG_IC and (ic_threshold is None or ic_mean >= CLEAR_IC_RATIO * ic_threshold):
        return f"{text}；检验通过（p={pvalue:.3f}）且强度可观"
    if ic_threshold is not None and ic_mean < CLEAR_IC_RATIO * ic_threshold:
        return f"{text}；检验通过（p={pvalue:.3f}）但仅略高于噪声门槛"
    return f"{text}；检验通过（p={pvalue:.3f}）"


def evaluate_factor(
    values: np.ndarray,
    labels: np.ndarray,
    *,
    value_id: str,
    display_name: str,
    config: FactorTestConfig,
) -> FactorEvidence:
    """Measure one value panel against one forward-return panel."""

    matrix = np.asarray(values, dtype=float)
    target = np.asarray(labels, dtype=float)
    correlation, counts, mask = correlation_gram(matrix, target)
    usable = counts >= max(2, config.min_cross_section)
    diagonal = np.where(usable, np.diag(correlation), np.nan)
    finite = np.isfinite(diagonal)
    empty_layers = tuple(
        LayerStat(index=index + 1, size=None, mean_return=None, beat_market_rate=None)
        for index in range(config.quantile_count)
    )
    if not finite.any():
        return FactorEvidence(
            value_id=value_id,
            display_name=display_name,
            order="high",
            ic_mean=float("nan"),
            icir=None,
            ic_days=0,
            ic_positive_rate=None,
            ic_series=(),
            ic_cumulative=(),
            ic_first_half=None,
            ic_second_half=None,
            layers=empty_layers,
            layer_inversions=0,
            long_short=_long_short(np.array([], dtype=float), config.horizon),
            ic_threshold=None,
            pvalue=None,
            qvalue=None,
            significant=False,
            note="没有任何可用的横截面样本（有效股票数不足或未来收益缺失）",
        )

    ic_mean = float(np.nanmean(diagonal))
    ic_std = float(np.nanstd(diagonal, ddof=1)) if int(finite.sum()) > 1 else 0.0
    icir = float(ic_mean / ic_std) if ic_std > 1e-12 else None
    positive_rate = (
        float(np.mean(diagonal[finite] > 0))
        if ic_mean >= 0
        else float(np.mean(diagonal[finite] < 0))
    )
    order: Order = "high" if ic_mean >= 0 else "low"
    pvalue, threshold = _permutation_test(
        correlation,
        usable,
        observed=ic_mean,
        block_size=config.block_size,
        permutations=config.permutations,
        seed=DEFAULT_SEED,
    )

    daily = _daily_layer_returns(
        matrix,
        target,
        mask,
        counts,
        usable,
        order=order,
        quantile_count=config.quantile_count,
    )
    with np.errstate(divide="ignore", invalid="ignore"):
        market_totals = np.where(mask, target, 0.0).sum(axis=0)
        market_means = np.where(
            usable, market_totals / np.where(counts > 0, counts, np.nan), np.nan
        )
    layers, inversions = _layers(daily, market_means)
    # Bucket sizes are reported per day and averaged: a layer of three stocks is
    # not evidence, and the reader deserves to see that it was three.
    sized: list[LayerStat] = []
    for index in range(daily.shape[0]):
        series = daily[index]
        finite_days = np.isfinite(series)
        size = (
            float(np.mean(counts[finite_days] / config.quantile_count))
            if finite_days.any()
            else None
        )
        sized.append(
            LayerStat(
                index=layers[index].index,
                size=size,
                mean_return=layers[index].mean_return,
                beat_market_rate=layers[index].beat_market_rate,
            )
        )
    long_short = _long_short(daily, config.horizon)
    first_half, second_half = _halves(diagonal, usable)
    cumulative = np.nancumsum(np.where(finite, diagonal, 0.0))
    note = _note(
        ic_days=int(finite.sum()),
        ic_mean=abs(ic_mean),
        icir=abs(icir) if icir is not None else None,
        ic_threshold=threshold,
        spread=long_short.mean_spread,
        win_rate=long_short.win_rate,
        inversions=inversions,
        first_half=first_half,
        second_half=second_half,
        pvalue=pvalue,
        alpha=config.alpha,
    )
    return FactorEvidence(
        value_id=value_id,
        display_name=display_name,
        order=order,
        ic_mean=abs(ic_mean),
        icir=abs(icir) if icir is not None else None,
        ic_days=int(finite.sum()),
        ic_positive_rate=positive_rate,
        ic_series=tuple(None if not np.isfinite(item) else float(item) for item in diagonal),
        ic_cumulative=tuple(float(item) for item in cumulative),
        ic_first_half=abs(first_half) if first_half is not None else None,
        ic_second_half=abs(second_half) if second_half is not None else None,
        layers=tuple(sized),
        layer_inversions=inversions,
        long_short=long_short,
        ic_threshold=threshold,
        pvalue=pvalue,
        qvalue=None,
        significant=False,
        note=note,
    )


def annotate_significance(
    evidence: list[FactorEvidence],
    *,
    alpha: float,
) -> list[FactorEvidence]:
    """Apply FDR control across the whole study and mark what survives.

    Called once for every value together, never per value: the multiplicity
    comes from having looked at all of them, so the correction has to see all
    of them.
    """

    qvalues = benjamini_hochberg([item.pvalue for item in evidence])
    adjusted: list[FactorEvidence] = []
    for item, qvalue in zip(evidence, qvalues):
        # 有效交易日不足时 note 已经写明「结论不可靠」，这时 q 值再小也不能挂徽章：
        # 置换次数越多，p 的下界越小（200 次时最小 p≈0.005），样本量越小越容易靠
        # 运气穿过 BH 校正。归因侧（factor_attribution/metrics.py）一直有这道闸，
        # 因子侧补上。
        significant = qvalue is not None and qvalue <= alpha and item.ic_days >= MIN_IC_DAYS
        note = item.note
        if qvalue is not None:
            note = f"{note}，多重检验校正后 q={qvalue:.3f}"
        adjusted.append(
            FactorEvidence(
                value_id=item.value_id,
                display_name=item.display_name,
                order=item.order,
                ic_mean=item.ic_mean,
                icir=item.icir,
                ic_days=item.ic_days,
                ic_positive_rate=item.ic_positive_rate,
                ic_series=item.ic_series,
                ic_cumulative=item.ic_cumulative,
                ic_first_half=item.ic_first_half,
                ic_second_half=item.ic_second_half,
                layers=item.layers,
                layer_inversions=item.layer_inversions,
                long_short=item.long_short,
                ic_threshold=item.ic_threshold,
                pvalue=item.pvalue,
                qvalue=qvalue,
                significant=significant,
                note=note,
            )
        )
    return adjusted


__all__ = [
    "FactorEvidence",
    "LayerStat",
    "LongShortStat",
    "annotate_significance",
    "benjamini_hochberg",
    "correlation_gram",
    "evaluate_factor",
]
