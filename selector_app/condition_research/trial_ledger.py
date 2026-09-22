"""累计试验次数校正：跨运行「取最好」那一次的偏差，需要记账才能校正。

## 为什么要有它

单次研究里的显著性已经由 ``mining._permutation_max_lifts`` 处理好了：它把
标签打乱、重新给**同一个候选池**打分，看纯运气能把最好的那个 lift 推到多高。
这就是 max-T 置换检验，它天然覆盖任意相关结构——所以「本次挖了几千个条件、
它们之间怎么相关」这件事**不需要再折算**，置换已经算进去了。

（这一点是实测之后才确认的，走过弯路：我原本打算按相关结构估「有效独立
试验数」再折算。用真实日线构造候选池算完整相关矩阵后发现，平均相关接近 0
且正负相抵，``N_eff = N²/ΣΣρ`` 给出的数比 N 还大——因为那个公式是**均值方差**
的等效样本量，而我们校正的对象是**最大值**。两者不是一回事。结论：单次运行
内的相关结构交给置换，不要另估 N_eff。）

真正没被覆盖的是**跨运行**：你跑了 K 轮研究，每轮都取了最好的那个，最后记住
的是 K 轮里的最大值。哪怕每轮的 p 值都诚实，K 轮里至少有一轮撞出 p ≤ 0.05 的
概率也远高于 0.05。这就是候选池 #20 要补的洞。

## 记账单位为什么是「轮」而不是「条件数」

关键在于：**换参数在同一段数据上重跑，不会增加撞好运的机会**。候选池一样、
样本一样、置换用固定种子，挖出来的东西一模一样——重复 9 次和跑 1 次，统计上
是同一轮试验。

实测本地库里的 9 次条件研究（2026-09-11 ~ 09-19）印证了这一点：区间全部是
2021-09-11~2026-09-11，4 个不同的数据指纹里有 3 个是重复跑。也就是说这 9 次
研究在统计上只相当于 1~2 轮独立试验，而不是 9 轮。

所以这里的记账单位是**独立数据段（轮）**：日期区间重叠过半就视为同一轮。

## 校正公式

K 轮独立试验，每轮撞出 p 的概率为 p，至少一轮撞出的概率是

    p_adj = 1 − (1 − p)^K          （Šidák）

K = 1 时恒等，所以只跑过一次研究的用户结果不变——这是对的：没有跨运行的选择
偏差需要校正。Šidák 假设各轮独立；各轮若共用样本（区间重叠）我们已把它们归
为同一轮，独立性由这一步保证。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

#: 日期区间 Jaccard 重叠到这个比例就算同一轮试验。
#: 0.5 = 一半以上的交易日是共用的，此时两轮研究撞好运的机会高度重合。
DEFAULT_OVERLAP_THRESHOLD = 0.5


@dataclass(frozen=True)
class TrialRecord:
    """One completed condition-research study, as far as the ledger cares.

    ``tested_conditions`` is how many threshold rules the study actually scored
    (both sides combined). It is kept for disclosure - see
    :func:`cumulative_tested_conditions` - but it is **not** what the
    correction keys off, for the reason spelled out in the module docstring.
    """

    history_id: str
    created_at: datetime
    data_fingerprint: str
    start_date: date
    end_date: date
    tested_conditions: int


def overlap_ratio(left: TrialRecord, right: TrialRecord) -> float:
    """Jaccard overlap of two studies' date windows, in [0, 1].

    Calendar days, not trading days: the two differ by a constant factor that
    cancels in the ratio, and using calendar days keeps the helper pure.
    """

    latest_start = max(left.start_date, right.start_date)
    earliest_end = min(left.end_date, right.end_date)
    if latest_start > earliest_end:
        return 0.0
    intersection = (earliest_end - latest_start).days + 1
    union = (
        max(left.end_date, right.end_date).toordinal()
        - min(left.start_date, right.start_date).toordinal()
        + 1
    )
    return intersection / union if union > 0 else 0.0


def effective_trial_count(
    records: list[TrialRecord],
    *,
    threshold: float = DEFAULT_OVERLAP_THRESHOLD,
) -> int:
    """Number of independent trial rounds the ledger represents.

    Studies whose windows overlap by ``threshold`` or more are one round: they
    share most of their samples, so a lucky find in one is near-certain to be
    findable in the other. Overlap is transitive here - A~B and B~C puts all
    three in one round even if A and C barely touch, because the shared samples
    were already searched in B.

    An empty ledger still counts as one round: the study about to be reported
    is itself a trial, and a correction factor of 1 must leave p unchanged.
    """

    if not records:
        return 1
    parent = list(range(len(records)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    for i in range(len(records)):
        for j in range(i + 1, len(records)):
            if overlap_ratio(records[i], records[j]) >= threshold:
                union(i, j)
    return len({find(index) for index in range(len(records))})


def cumulative_tested_conditions(
    records: list[TrialRecord],
    *,
    threshold: float = DEFAULT_OVERLAP_THRESHOLD,
) -> int:
    """How many distinct conditions all recorded studies have scored.

    Within one round the studies re-mine nearly the same pool, so that round
    contributes its largest count instead of the sum - otherwise re-running a
    study would inflate the number without doing any new searching.
    """

    if not records:
        return 0
    parent = list(range(len(records)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    for i in range(len(records)):
        for j in range(i + 1, len(records)):
            if overlap_ratio(records[i], records[j]) >= threshold:
                union(i, j)
    per_round: dict[int, int] = {}
    for index, record in enumerate(records):
        root = find(index)
        per_round[root] = max(per_round.get(root, 0), record.tested_conditions)
    return sum(per_round.values())


def sidak_adjust(pvalue: float, trials: int) -> float:
    """Probability that at least one of ``trials`` studies would reach ``pvalue``.

    Returns ``pvalue`` unchanged for ``trials <= 1`` so an unadjusted result
    stays exactly what the permutation test produced. Values outside [0, 1]
    are passed through: callers must not silently "fix" a malformed p-value
    into something that looks significant.
    """

    if trials <= 1:
        return pvalue
    if not 0.0 <= pvalue <= 1.0:
        return pvalue
    if pvalue >= 1.0:
        return 1.0
    return 1.0 - (1.0 - pvalue) ** trials
