"""Swing labels and the indicator study built on them.

The statistics here are the part that can silently lie, so the tests are
mostly about behaving correctly on data with a known answer: a perfectly
monotone panel must score 1.0, and a panel of pure noise must not come back
calling itself significant.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from selector_app.market_data.models import StockRef
from selector_app.swing_research.labels import bar_labels, segment_onset
from selector_app.swing_research.models import SwingIndicatorConfig
from selector_app.swing_research.scoring import (
    DEFAULT_BLOCK_SIZE,
    benjamini_hochberg,
    block_permutations,
    effective_block_size,
    score_indicator,
    tie_average_rank,
)
from selector_app.swing_research.segments import split_swings
from selector_app.swing_research.service import SwingResearchService


def _path(returns: Sequence[float], start_price: float = 10.0) -> list[float]:
    closes = [start_price]
    for step in returns:
        closes.append(closes[-1] * (1 + step))
    return closes


def _frame(closes: Sequence[float], *, spread: float = 0.004) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.bdate_range("2022-01-03", periods=len(closes)),
            "open": list(closes),
            "high": [price * (1 + spread) for price in closes],
            "low": [price * (1 - spread) for price in closes],
            "close": list(closes),
            "volume": [1_000.0] * len(closes),
            "amount": [price * 1_000.0 for price in closes],
        }
    )


class _FakeAdapter:
    """Serves prepared frames, ignoring date windows like a warm-up read does."""

    def __init__(self, frames: dict[tuple[str, str], pd.DataFrame]) -> None:
        self._frames = frames

    def list_stock_refs(
        self,
        vipdoc_path: str | Path,
        universe: str,
        universe_file: str | Path | None = None,
        instrument_types: tuple[str, ...] | None = None,
        boards: tuple[str, ...] | None = None,
    ) -> list[StockRef]:
        return [
            StockRef(market=market, code=code, path=Path("."))
            for market, code in sorted(self._frames)
        ]

    def read_many_stocks(
        self,
        refs: list[StockRef],
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        warmup_bars: int = 0,
    ) -> pd.DataFrame:
        frames = []
        for ref in refs:
            frame = self._frames[(ref.market, ref.code)].copy()
            frame = self._window(frame, start_date, end_date, warmup_bars)
            if frame.empty:
                continue
            frame["market"] = ref.market
            frame["code"] = ref.code
            frames.append(frame)
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    @staticmethod
    def _window(
        frame: pd.DataFrame,
        start_date: date | None,
        end_date: date | None,
        warmup_bars: int,
    ) -> pd.DataFrame:
        """Honour the date window, then extend it back by the warm-up bars.

        Mirrors the real store: the caller gets the window it asked for plus
        enough history before it for indicators to be defined on day one.
        """

        if start_date is None and end_date is None:
            return frame
        keep = np.ones(len(frame), dtype=bool)
        if start_date is not None:
            keep &= (frame["date"] >= pd.Timestamp(start_date)).to_numpy()
        if end_date is not None:
            keep &= (frame["date"] <= pd.Timestamp(end_date)).to_numpy()
        inside = np.nonzero(keep)[0]
        if inside.size and warmup_bars > 0:
            first = max(0, int(inside[0]) - warmup_bars)
            keep[first : int(inside[0])] = True
        return frame[keep]


def test_tie_average_rank_splits_ties_evenly() -> None:
    values = np.array([[1.0], [0.0], [1.0], [0.0]])
    mask = np.ones((4, 1), dtype=bool)

    ranks = tie_average_rank(values, mask)

    # Two zeros share ranks 0 and 1, two ones share ranks 2 and 3.
    assert ranks.ravel().tolist() == [2.5, 0.5, 2.5, 0.5]


def test_tie_average_rank_ignores_masked_cells() -> None:
    values = np.array([[5.0], [np.nan], [1.0]])
    mask = np.isfinite(values)

    ranks = tie_average_rank(values, mask)

    assert ranks[1] == 0.0
    assert ranks[0] > ranks[2]


def test_perfect_monotone_panel_scores_one() -> None:
    rng = np.random.default_rng(2)
    stocks, days = 12, 80
    # Same ordering every day (so the IC is 1) but different values every day
    # (so shuffling the calendar actually changes something).
    values = rng.normal(size=(stocks, days))
    labels = np.argsort(np.argsort(values, axis=0), axis=0).astype(float)

    score = score_indicator(
        values,
        labels,
        value_id="t",
        display_name="t",
        label="l",
        min_cross_section=5,
        block_size=5,
    )

    assert score.ic_mean == pytest.approx(1.0, abs=1e-9)
    assert score.order == "high"
    assert score.pvalue is not None and score.pvalue <= 0.05


def test_pure_noise_is_not_significant() -> None:
    rng = np.random.default_rng(11)
    values = rng.normal(size=(80, 260))
    labels = (rng.random((80, 260)) < 0.12).astype(float)

    score = score_indicator(
        values,
        labels,
        value_id="noise",
        display_name="noise",
        label="l",
        min_cross_section=20,
        permutations=100,
    )

    assert abs(score.ic_mean) < 0.05
    assert score.pvalue is not None and score.pvalue > 0.05
    assert not score.significant


def test_injected_edge_is_significant() -> None:
    rng = np.random.default_rng(5)
    stocks, days = 80, 260
    values = rng.normal(size=(stocks, days))
    # The top of each day's cross-section is far more likely to be flagged.
    rank = np.argsort(np.argsort(values, axis=0), axis=0) / (stocks - 1)
    labels = (rng.random((stocks, days)) < 0.1 + 0.5 * rank**3).astype(float)

    score = score_indicator(
        values,
        labels,
        value_id="edge",
        display_name="edge",
        label="l",
        min_cross_section=20,
        permutations=100,
    )

    assert score.ic_mean > 0.05
    assert score.order == "high"
    assert score.pvalue is not None and score.pvalue <= 0.05
    assert score.top_lift is not None and score.top_lift > 1.0


def test_threshold_hint_survives_a_sparse_cross_section() -> None:
    """横截面稀疏时阈值提示不能退化成 NaN。

    面板里每天只有一小部分标的有值（未上市、指标在预热、标签的未来窗口不存
    在）。分位点若按总行数取，np.sort 把缺失值排到列尾，稀疏日取到的就是
    NaN —— threshold_hint 变 None，conditions.py 会直接跳过这个指标，条件
    计划连一条都生成不出来。
    """

    rng = np.random.default_rng(7)
    stocks, days, valid = 100, 60, 20
    values = np.full((stocks, days), np.nan)
    labels = np.full((stocks, days), np.nan)
    for day in range(days):
        rows = rng.choice(stocks, size=valid, replace=False)
        column = rng.normal(size=valid)
        values[rows, day] = column
        # 标签就是当天的排名，信息系数接近 1，order 必定是 high。
        labels[rows, day] = np.argsort(np.argsort(column)) / (valid - 1)

    score = score_indicator(
        values,
        labels,
        value_id="sparse",
        display_name="稀疏",
        label="l",
        min_cross_section=10,
        permutations=0,
    )

    assert score.order == "high"
    assert score.threshold_hint is not None
    # 提示值必须落在真实数据的取值范围里，而不是排到缺失值那一段。
    assert min(np.nanmin(values), 0.0) <= score.threshold_hint <= np.nanmax(values)


def test_block_permutations_keep_days_inside_a_block_together() -> None:
    permutations = block_permutations(25, block_size=5, count=4, seed=3)

    assert len(permutations) == 4
    for permutation in permutations:
        assert sorted(permutation.tolist()) == list(range(25))
        # Each fifth must appear as a contiguous run, in its original order.
        text = ",".join(str(item) for item in permutation)
        for start in range(0, 25, 5):
            run = ",".join(str(item) for item in range(start, start + 5))
            assert run in text


def test_block_permutations_never_fall_below_the_resolution_floor() -> None:
    """块数太少时检验根本没有功效，宁可把块切短。

    k 个块只有 k! 种排法，p 值的下界约 1/k!：2 块是 0.5、3 块是 0.17，都压不到
    alpha 下面。曾经只在「只剩一个块」时才切，于是 60 天 / 块长 20（3 块）的强信号
    实测 p=0.21——信号已强到 IC=0.79，页面却读成「无法排除偶然」。这不是保守，
    是哑了，而它和「没证据」长得一模一样。
    """

    # 60 天、块长 20 只够 3 块 → 切到 12（5 块）。
    assert effective_block_size(60, 20) == 12
    assert effective_block_size(80, 45) == 16
    # 块本来就够多时不要动它。
    assert effective_block_size(200, 10) == 10
    assert effective_block_size(250, DEFAULT_BLOCK_SIZE) == DEFAULT_BLOCK_SIZE
    # 切短之后每一块内部仍然连续：块长不能掉到 1，否则又变回逐日打乱。
    assert effective_block_size(30, 20) == 6


def test_a_short_series_can_still_call_an_overwhelming_signal() -> None:
    """60 天的研究窗口配默认块长 20 只有 3 块——修复前这里 p≈0.21。"""

    rng = np.random.default_rng(20260924)
    stocks, days = 60, 60
    values = rng.normal(0, 1, size=(stocks, days))
    # 标签几乎完全由指标决定，这是能造出来的最强信号。
    latent = 3.0 * values + 0.1 * rng.normal(0, 1, size=(stocks, days))
    labels = (latent > np.quantile(latent, 0.7, axis=0)).astype(float)

    score = score_indicator(
        values,
        labels,
        value_id="strong",
        display_name="strong",
        label="l",
        min_cross_section=20,
        block_size=DEFAULT_BLOCK_SIZE,
        permutations=200,
    )

    assert score.ic_mean > 0.5
    assert score.pvalue is not None and score.pvalue <= 0.05


def test_shortening_blocks_does_not_inflate_the_false_positive_rate() -> None:
    """切短块换来功效，代价可能是误报率——必须确认它仍贴着 alpha。

    只看「纯噪声不显著」抓不到这个：块数不足时检验恒不显著，噪声当然也全过。
    要看的是**比例**。
    """

    rng = np.random.default_rng(20260924)
    stocks, days = 60, 60
    flagged = 0
    trials = 0
    for _ in range(120):
        values = rng.normal(0, 1, size=(stocks, days))
        labels = rng.normal(0, 1, size=(stocks, days))
        score = score_indicator(
            values,
            labels,
            value_id="noise",
            display_name="noise",
            label="l",
            min_cross_section=20,
            block_size=DEFAULT_BLOCK_SIZE,
            permutations=200,
            seed=int(rng.integers(1, 1 << 30)),
        )
        if score.pvalue is None:
            continue
        trials += 1
        if score.pvalue <= 0.05:
            flagged += 1

    rate = flagged / trials
    # 120 轮 × p=0.05 的标准差约 2 个百分点，上限取 10% 留两倍余量。
    assert rate < 0.10, f"误报率 {rate:.3f} 偏离 alpha=0.05 太远"


def test_benjamini_hochberg_is_monotone_and_capped() -> None:
    adjusted = benjamini_hochberg([0.01, 0.02, 0.9, None])

    assert adjusted[3] is None
    assert adjusted[0] is not None and adjusted[1] is not None
    assert adjusted[0] <= adjusted[1] <= 1.0
    assert adjusted[2] is not None and adjusted[2] <= 1.0


def test_onset_is_the_first_bar_that_travelled_the_threshold() -> None:
    closes = _path([0.02] * 30)
    frame = _frame(closes)
    segments = split_swings(frame, reversal_multiple=1.0, include_pending=True)

    onset = segment_onset(frame, segments[0])

    assert onset is not None
    # Around 1% a bar against a 1-ATR threshold, so roughly the 100th bar - the
    # point is that it is well after the start and well before the end.
    assert 0 < onset < len(closes) - 1


def test_anticipation_labels_leave_the_tail_unknown() -> None:
    closes = _path([0.015] * 20 + [-0.015] * 20)
    frame = _frame(closes)
    segments = split_swings(frame, reversal_multiple=1.0)

    labels = bar_labels(frame, segments, min_bars=3, horizon=5)

    ahead = labels["up_onset_ahead"]
    assert np.isnan(ahead[-1])
    assert np.isnan(ahead[-5])
    assert not np.isnan(ahead[0])


def test_short_legs_are_excluded_from_labels() -> None:
    # 四段行情（涨跌涨跌）。不能用两段：第一段起点不是转折点、不打标，两段序列里
    # 就一个合法标签都没有，这条测试会退化成「什么都没验证」。
    closes = _path([0.02] * 10 + [-0.02] * 10 + [0.02] * 10 + [-0.02] * 10)
    frame = _frame(closes)
    segments = split_swings(frame, reversal_multiple=1.0)

    strict = bar_labels(frame, segments, min_bars=12)
    loose = bar_labels(frame, segments, min_bars=0)

    assert strict["up_leg_start"].sum() == 0
    assert loose["up_leg_start"].sum() >= 1


def test_first_leg_start_is_not_a_pivot() -> None:
    """序列的第一根 bar 不是转折点，不能当「波段最低点」用。

    ``split_swings`` 从 ``seg_start = 0`` 起步，第一段没有更早的行情来确认那是
    个底——它只是数据窗口的边界。把它算进去，等于往「最低点」里掺一个随手取的
    坐标，它不含任何关于转折的信息。
    """

    closes = _path([0.02] * 10 + [-0.02] * 10)
    frame = _frame(closes)
    segments = split_swings(frame, reversal_multiple=1.0)

    labels = bar_labels(frame, segments, min_bars=0)

    # 这段序列只有第一段完成了反转确认，跳过它就没有标签剩下。
    assert segments and segments[0].start_index == 0
    assert labels["up_leg_start"].sum() == 0


def test_attribution_labels_sit_on_the_previous_extreme() -> None:
    """归因标签打在**前一波的极值点**上，不是随便哪根 bar。

    ZigZag 的相邻两段共用转折点：一段在极值 bar 收尾，下一段就从那里起算。所以
    「上涨段起点」正是前一波下跌的最低点，「下跌段起点」正是前一波上涨的最高点
    ——这也是为什么这两个标签研究的是顶和底，尽管名字叫「段起点」。
    """

    closes = _path([0.02] * 10 + [-0.02] * 10 + [0.02] * 10 + [-0.02] * 10)
    frame = _frame(closes)
    segments = split_swings(frame, reversal_multiple=1.0)

    labels = bar_labels(frame, segments, min_bars=0)

    up_starts = set(np.nonzero(labels["up_leg_start"])[0].tolist())
    down_starts = set(np.nonzero(labels["down_leg_start"])[0].tolist())
    up_ends = {seg.end_index for seg in segments if seg.direction == "up"}
    down_ends = {seg.end_index for seg in segments if seg.direction == "down"}

    assert up_starts, "至少要有一个上涨段起点"
    assert down_starts, "至少要有一个下跌段起点"
    assert up_starts <= down_ends
    assert down_starts <= up_ends


def _random_walk(rng: np.random.Generator, length: int) -> list[float]:
    return _path(rng.normal(0.0004, 0.02, size=length - 1))


def _market(
    rng: np.random.Generator, count: int, length: int
) -> dict[tuple[str, str], pd.DataFrame]:
    return {
        ("SH", f"{600000 + index:06d}"): _frame(_random_walk(rng, length)) for index in range(count)
    }


def test_indicator_study_returns_one_score_per_pair() -> None:
    rng = np.random.default_rng(21)
    frames = _market(rng, count=24, length=520)
    service = SwingResearchService(_FakeAdapter(frames))

    payload = service.indicator_study(
        SwingIndicatorConfig(
            start_date=date(2023, 6, 1),
            min_cross_section=10,
            permutations=20,
            batch_size=12,
        )
    )

    assert payload["scanned_stocks"] == 24
    assert payload["value_count"] > 0
    scores = payload["scores"]
    assert len(scores) == payload["value_count"] * 4
    assert {item["label"] for item in scores} == {
        "up_leg_start",
        "down_leg_start",
        "up_onset_ahead",
        "down_onset_ahead",
    }
    for item in scores:
        assert item["ic_mean"] is None or isinstance(item["ic_mean"], float)
        if item["ic_mean"] is None:
            assert item["ic_days"] == 0
            assert item["pvalue"] is None
            assert item["significant"] is False
        assert item["qvalue"] is None or 0.0 <= item["qvalue"] <= 1.0


def test_indicator_study_on_random_walks_stays_mostly_quiet() -> None:
    rng = np.random.default_rng(33)
    frames = _market(rng, count=24, length=520)
    service = SwingResearchService(_FakeAdapter(frames))

    payload = service.indicator_study(
        SwingIndicatorConfig(
            start_date=date(2023, 6, 1),
            min_cross_section=10,
            permutations=20,
            labels=("up_onset_ahead",),
        )
    )

    scores = payload["scores"]
    assert scores
    # Random walks carry real momentum structure, so a few findings are
    # legitimate; what must not happen is a board full of "significant" hits.
    assert payload["significant_count"] <= len(scores) // 4


def test_indicator_study_rejects_non_technical_values() -> None:
    service = SwingResearchService(_FakeAdapter({}))

    with pytest.raises(ValueError, match="不能用于波段研究"):
        service.indicator_study(SwingIndicatorConfig(value_ids=("indicator_one.var1",)))


def test_indicator_study_validates_the_study_window() -> None:
    service = SwingResearchService(_FakeAdapter({}))

    with pytest.raises(ValueError, match="预警窗口"):
        service.indicator_study(SwingIndicatorConfig(horizon=0))


def _studied_value_ids(**overrides: object) -> tuple[str, ...]:
    rng = np.random.default_rng(21)
    frames = _market(rng, count=24, length=520)
    service = SwingResearchService(_FakeAdapter(frames))
    payload = service.indicator_study(
        SwingIndicatorConfig(
            start_date=date(2023, 6, 1),
            min_cross_section=10,
            permutations=0,
            batch_size=12,
            labels=("up_onset_ahead",),
            **overrides,  # type: ignore[arg-type]
        )
    )
    return tuple(dict.fromkeys(item["value_id"] for item in payload["scores"]))


def test_indicator_study_scores_custom_formula_outputs_too() -> None:
    """The custom formulas are in by default - that was the whole question."""

    value_ids = _studied_value_ids()

    assert "indicator_three.var1" in value_ids
    assert "indicator_two.mid_line" in value_ids
    assert "indicator_one.main_force_entry" in value_ids
    assert any(item.startswith("technical.") for item in value_ids)


def test_indicator_study_can_stick_to_the_technical_family() -> None:
    value_ids = _studied_value_ids(include_custom=False)

    assert value_ids
    assert all(item.startswith("technical.") for item in value_ids)


def test_indicator_study_rejects_constants_pulses_and_prices() -> None:
    """The allow-list is the point: these three all measure as unrankable."""

    service = SwingResearchService(_FakeAdapter({}))
    for value_id in (
        "indicator_one.z",  # literally zeros
        "indicator_three.var2",  # literally 8.0
        "indicator_three.prepare_rally",  # 0 or 80, 98% ties
        "indicator_three.varo5",  # LLV(low, 27): the price
    ):
        with pytest.raises(ValueError, match="不能用于波段研究"):
            service.indicator_study(SwingIndicatorConfig(value_ids=(value_id,)))


def test_custom_outputs_are_named_with_their_formula() -> None:
    rng = np.random.default_rng(21)
    frames = _market(rng, count=24, length=520)
    service = SwingResearchService(_FakeAdapter(frames))

    payload = service.indicator_study(
        SwingIndicatorConfig(
            start_date=date(2023, 6, 1),
            min_cross_section=10,
            permutations=0,
            batch_size=12,
            labels=("up_onset_ahead",),
        )
    )

    names = {item["value_id"]: item["display_name"] for item in payload["scores"]}
    # A table mixing four formulas cannot just say "短期线".
    assert names["indicator_two.short_line"] == "庄家顶底 · 短期线"
    assert names["indicator_three.varo7"] == "准备拉升 · 建仓强度"


def test_indicator_study_reports_when_nothing_was_scanned() -> None:
    service = SwingResearchService(_FakeAdapter({}))

    payload = service.indicator_study(SwingIndicatorConfig())

    assert payload["scanned_stocks"] == 0
    assert payload["scores"] == []
    assert payload["reason"]


def test_indicator_study_window_moves_with_start_date() -> None:
    rng = np.random.default_rng(7)
    frames = _market(rng, count=12, length=520)
    service = SwingResearchService(_FakeAdapter(frames))

    early = service.indicator_study(
        SwingIndicatorConfig(start_date=date(2023, 1, 1), min_cross_section=5, permutations=0)
    )
    late = service.indicator_study(
        SwingIndicatorConfig(
            start_date=date(2023, 1, 1),
            end_date=date(2023, 7, 1),
            min_cross_section=5,
            permutations=0,
        )
    )

    assert early["date_range"]["days"] > late["date_range"]["days"]
    assert late["date_range"]["end"] <= "2023-07-01"


def test_indicator_study_progress_covers_every_stage() -> None:
    """The UI draws one bar across three passes, so none may be silent.

    The scoring pass is the one that matters: it runs the permutations, and a
    bar that stopped when the panel was full would sit at 100% for the second
    half of the wait.
    """

    rng = np.random.default_rng(21)
    frames = _market(rng, count=24, length=520)
    service = SwingResearchService(_FakeAdapter(frames))
    events: list[tuple[int, int, str]] = []

    service.indicator_study(
        SwingIndicatorConfig(
            start_date=date(2023, 6, 1),
            min_cross_section=10,
            permutations=20,
            batch_size=12,
        ),
        progress_callback=lambda current, total, stage: events.append((current, total, stage)),
    )

    assert events
    totals = {total for _, total, _ in events}
    assert len(totals) == 1, "进度总量中途变了，进度条会跳回去"
    total = events[0][1]

    steps = [current for current, _, _ in events]
    assert steps == sorted(steps), "进度必须单调不减"
    assert steps[0] < total
    assert steps[-1] == total, "结束时必须报到满格"

    stages = [stage for _, _, stage in events]
    assert "正在扫描交易日" in stages
    assert "正在计算指标与切分波段" in stages
    assert "正在逐日横截面打分" in stages
    # 打分必须在填面板之后开始，否则两个阶段会互相穿插。
    first_score = stages.index("正在逐日横截面打分")
    assert "正在计算指标与切分波段" in stages[:first_score]
