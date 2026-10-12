"""累计试验次数校正的单元测试。"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from aquest.condition_research.trial_ledger import (
    TrialRecord,
    cumulative_tested_conditions,
    effective_trial_count,
    overlap_ratio,
    sidak_adjust,
)


def _record(
    history_id: str,
    start: str,
    end: str,
    *,
    tested: int = 1000,
    fingerprint: str = "fp",
    created: str = "2026-09-20T10:00:00",
) -> TrialRecord:
    return TrialRecord(
        history_id=history_id,
        created_at=datetime.fromisoformat(created),
        data_fingerprint=fingerprint,
        start_date=date.fromisoformat(start),
        end_date=date.fromisoformat(end),
        tested_conditions=tested,
    )


def test_overlap_ratio_basic() -> None:
    a = _record("a", "2021-01-01", "2021-12-31")
    b = _record("b", "2021-01-01", "2021-12-31")
    assert overlap_ratio(a, b) == pytest.approx(1.0)

    disjoint = _record("c", "2023-01-01", "2023-12-31")
    assert overlap_ratio(a, disjoint) == 0.0

    # 一半重叠：2021-01-01~06-30 与 2021-01-01~12-31
    half = _record("d", "2021-01-01", "2021-06-30")
    assert overlap_ratio(a, half) == pytest.approx(0.5, abs=0.01)


def test_empty_ledger_counts_as_one_round() -> None:
    """空台账 = 本次就是第一轮，校正系数必须是 1（不能改 p）。"""

    assert effective_trial_count([]) == 1
    assert sidak_adjust(0.04, 1) == pytest.approx(0.04)


def test_repeated_runs_on_same_window_are_one_round() -> None:
    """同一段数据反复跑不算新试验——这是整个记账设计的立足点。

    本地库实证：9 次研究的区间全是 2021-09-11~2026-09-11，其中 4 个指纹里
    3 个是重复跑。统计上它们是 1 轮，不是 9 轮。
    """

    records = [_record(f"h{index}", "2021-09-11", "2026-09-11", tested=2400) for index in range(9)]
    assert effective_trial_count(records) == 1
    # 同一轮里重复挖掘不累加条件数，取该轮最大值。
    assert cumulative_tested_conditions(records) == 2400


def test_disjoint_windows_are_separate_rounds() -> None:
    records = [
        _record("a", "2019-01-01", "2020-12-31"),
        _record("b", "2021-01-01", "2022-12-31"),
        _record("c", "2023-01-01", "2024-12-31"),
    ]
    assert effective_trial_count(records) == 3
    assert cumulative_tested_conditions(records) == 3000


def test_partial_overlap_still_one_round() -> None:
    """滚动推进的区间（每次往后挪一年）重叠过半，仍是同一轮。"""

    records = [
        _record("a", "2020-01-01", "2024-12-31"),
        _record("b", "2021-01-01", "2025-12-31"),
    ]
    assert overlap_ratio(records[0], records[1]) > 0.5
    assert effective_trial_count(records) == 1


def test_overlap_is_transitive() -> None:
    """A~B、B~C 但 A 与 C 几乎不挨着：三者仍算一轮。

    因为 B 那段样本已经被挖过一遍了，A 和 C 撞出来的好运共用了 B 的搜索。
    """

    records = [
        _record("a", "2020-01-01", "2023-12-31"),
        _record("b", "2021-01-01", "2024-12-31"),
        _record("c", "2022-01-01", "2025-12-31"),
    ]
    # 相邻对重叠 0.6（过半），首尾对只有 0.33（不过半）。
    assert overlap_ratio(records[0], records[1]) >= 0.5
    assert overlap_ratio(records[1], records[2]) >= 0.5
    assert overlap_ratio(records[0], records[2]) < 0.5
    assert effective_trial_count(records) == 1


def test_sidak_adjust_monotonic_and_bounded() -> None:
    """轮数越多，同一个 p 越不值钱；且永远落在 [0,1]。"""

    previous = 0.04
    for trials in (2, 3, 5, 10, 50):
        adjusted = sidak_adjust(0.04, trials)
        assert adjusted > previous
        assert 0.0 <= adjusted <= 1.0
        previous = adjusted
    # 10 轮下 p=0.04 已经远超 0.05，校正后必须判不显著。
    assert sidak_adjust(0.04, 10) > 0.3


def test_sidak_matches_closed_form() -> None:
    assert sidak_adjust(0.05, 3) == pytest.approx(1 - 0.95**3)


def test_sidak_leaves_malformed_pvalue_alone() -> None:
    """越界的 p 值原样返回：不许把它"修"成一个看起来显著的数字。"""

    assert sidak_adjust(-0.1, 5) == pytest.approx(-0.1)
    assert sidak_adjust(1.5, 5) == pytest.approx(1.5)
    assert sidak_adjust(1.0, 5) == pytest.approx(1.0)


def test_round_count_drives_verdict_flip() -> None:
    """端到端语义：一次研究判显著，同样的数据第十次跑出来就不显著了。"""

    pvalue = 0.03
    assert sidak_adjust(pvalue, 1) <= 0.05
    assert sidak_adjust(pvalue, 10) > 0.05
