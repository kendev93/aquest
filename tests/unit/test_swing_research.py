"""Swing segmentation and its calibration overview."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import NamedTuple

import numpy as np
import pandas as pd
import pytest

from selector_app.market_data.models import StockRef
from selector_app.swing_research import (
    SwingAccumulator,
    SwingOverviewConfig,
    SwingResearchService,
    SwingSegment,
    atr_percent,
    split_swings,
)
from selector_app.swing_research import service as swing_service


def _path(returns: Sequence[float], start_price: float = 10.0) -> list[float]:
    closes = [start_price]
    for step in returns:
        closes.append(closes[-1] * (1 + step))
    return closes


def _frame(
    closes: Sequence[float],
    *,
    spread: float = 0.002,
    start: str = "2024-01-01",
) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.date_range(start, periods=len(closes), freq="D"),
            "open": list(closes),
            "high": [price * (1 + spread) for price in closes],
            "low": [price * (1 - spread) for price in closes],
            "close": list(closes),
            "volume": [1_000.0] * len(closes),
        }
    )


def _leg(
    direction: str,
    start_index: int,
    end_index: int,
    *,
    start_price: float = 10.0,
    end_price: float = 11.0,
) -> SwingSegment:
    return SwingSegment(
        direction=direction,  # type: ignore[arg-type]
        start_index=start_index,
        end_index=end_index,
        start_price=start_price,
        end_price=end_price,
        amplitude=(end_price - start_price) / start_price,
        threshold=0.03,
        pending=False,
    )


class _FakeAdapter:
    """Serves prepared frames and records how they were requested."""

    def __init__(self, frames: dict[tuple[str, str], pd.DataFrame]) -> None:
        self._frames = frames
        self.batch_sizes: list[int] = []

    def list_stock_refs(
        self,
        vipdoc_path: str | Path,
        universe: str,
        universe_file: str | Path | None = None,
        instrument_types: tuple[str, ...] | None = None,
        boards: tuple[str, ...] | None = None,
    ) -> list[StockRef]:
        return [StockRef(market=market, code=code, path=Path(".")) for market, code in self._frames]

    def read_many_stocks(
        self,
        refs: list[StockRef],
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        warmup_bars: int = 0,
    ) -> pd.DataFrame:
        self.batch_sizes.append(len(refs))
        frames = []
        for ref in refs:
            frame = self._frames[(ref.market, ref.code)].copy()
            frame["market"] = ref.market
            frame["code"] = ref.code
            frames.append(frame)
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def test_monotonic_rise_is_one_pending_leg() -> None:
    segments = split_swings(_frame(_path([0.01] * 40)), reversal_multiple=1.0, include_pending=True)

    assert len(segments) == 1
    assert segments[0].direction == "up"
    assert segments[0].pending is True
    assert segments[0].duration == 40
    assert segments[0].amplitude > 0


def test_pending_leg_is_hidden_by_default() -> None:
    frame = _frame(_path([0.01] * 40))

    assert split_swings(frame, reversal_multiple=1.0) == []


def test_reversal_closes_the_leg_at_its_extreme() -> None:
    frame = _frame(_path([0.01] * 20 + [-0.01] * 20))

    segments = split_swings(frame, reversal_multiple=1.0, include_pending=False)

    assert len(segments) == 1
    leg = segments[0]
    assert leg.direction == "up"
    # The leg ends where price actually topped, not where the drop confirmed it.
    assert (leg.start_index, leg.end_index) == (0, 20)
    assert leg.pending is False


def test_small_pullback_does_not_end_the_leg() -> None:
    frame = _frame(_path([0.01] * 10 + [-0.003] * 3 + [0.01] * 10))

    segments = split_swings(frame, reversal_multiple=3.0, include_pending=True)

    assert len(segments) == 1
    assert segments[0].direction == "up"


def test_threshold_scales_with_volatility() -> None:
    """A shape scaled up 10x in returns must cut into the same legs.

    This is the whole point of measuring the threshold in ATR units: the same
    percentage move is noise for one stock and a reversal for another, so the
    segmentation has to be invariant to how volatile the series is.
    """

    shape = [0.002] * 10 + [-0.001] * 4 + [0.002] * 10 + [-0.003] * 6 + [0.001] * 8
    quiet = split_swings(
        _frame(_path(shape), spread=0.0005),
        reversal_multiple=3.0,
        include_pending=True,
    )
    wild = split_swings(
        _frame(_path([step * 10 for step in shape]), spread=0.005),
        reversal_multiple=3.0,
        include_pending=True,
    )

    # Cut points must match; amplitudes cannot, because compounding a 10x
    # return series is not the same as scaling its percentage result.
    described = [(item.direction, item.start_index, item.end_index) for item in quiet]
    assert described
    assert described == [(item.direction, item.start_index, item.end_index) for item in wild]
    # The wilder series must also have travelled roughly ten times as far.
    assert wild[0].amplitude > quiet[0].amplitude * 5


def test_short_and_empty_frames_produce_no_legs() -> None:
    assert split_swings(_frame([10.0]), reversal_multiple=1.0) == []
    assert split_swings(pd.DataFrame(columns=["high", "low", "close"])) == []


def test_invalid_parameters_are_rejected() -> None:
    frame = _frame(_path([0.01] * 20))

    with pytest.raises(ValueError, match="反转阈值倍数"):
        split_swings(frame, reversal_multiple=0.0)
    with pytest.raises(ValueError, match="ATR 窗口"):
        split_swings(frame, atr_window=0)
    with pytest.raises(ValueError, match="缺少必要列"):
        split_swings(pd.DataFrame({"close": [1.0, 2.0]}), reversal_multiple=1.0)


def test_atr_percent_converges_on_a_constant_range() -> None:
    count = 40
    frame = pd.DataFrame(
        {
            "high": [10.1] * count,
            "low": [9.9] * count,
            "close": [10.0] * count,
        }
    )

    atr = atr_percent(frame)

    assert np.isnan(atr[:13]).all()
    # A constant 0.2 true range on a 10.0 close is 2%.
    assert atr[13] == pytest.approx(2.0)
    assert atr[-1] == pytest.approx(2.0)


def test_accumulator_filters_legs_shorter_than_min_bars() -> None:
    accumulator = SwingAccumulator(3.0, min_bars=5)
    accumulator.add([_leg("up", 0, 2), _leg("up", 0, 10), _leg("down", 0, 20)])

    summary = accumulator.summary(years=1.0)

    assert summary.segment_count == 3
    assert summary.long_segment_count == 2
    assert summary.up.count == 1
    assert summary.down.count == 1
    assert summary.segments_per_stock_per_year == pytest.approx(3.0)


def test_histogram_uses_fixed_buckets() -> None:
    accumulator = SwingAccumulator(3.0, min_bars=0)
    accumulator.add([_leg("up", 0, 30, start_price=10.0, end_price=20.0)])

    summary = accumulator.summary()

    assert [item["bucket"] for item in summary.up.duration_histogram] == [
        "0-3",
        "3-5",
        "5-10",
        "10-20",
        "20-40",
        "40-80",
        "80+",
    ]
    counts = {item["bucket"]: item["count"] for item in summary.up.duration_histogram}
    assert counts["20-40"] == 1
    amplitude = {item["bucket"]: item["count"] for item in summary.up.amplitude_histogram}
    assert amplitude["80+"] == 1


def test_empty_accumulator_reports_nulls_instead_of_zeroes() -> None:
    summary = SwingAccumulator(3.0).summary(years=1.0)

    assert summary.up.count == 0
    assert summary.up.duration["p50"] is None
    assert summary.up.amplitude["mean"] is None
    assert summary.segments_per_stock_per_year is None


def test_service_reports_one_scenario_per_threshold() -> None:
    frame = _frame(_path([0.01] * 30 + [-0.01] * 10))
    service = SwingResearchService(_FakeAdapter({("SH", "600000"): frame}))

    result = service.overview(SwingOverviewConfig(reversal_multiples=(2.0, 3.0)))

    assert result["scanned_stocks"] == 1
    scenarios = result["scenarios"]
    assert [item["reversal_multiple"] for item in scenarios] == [2.0, 3.0]
    assert all(item["stock_count"] == 1 for item in scenarios)
    assert result["date_range"]["start"] == "2024-01-01"


def test_service_keeps_only_legs_starting_inside_the_window() -> None:
    frame = _frame(_path([0.01] * 20 + [-0.01] * 20))
    service = SwingResearchService(_FakeAdapter({("SH", "600000"): frame}))

    inside = service.overview(
        SwingOverviewConfig(reversal_multiples=(1.0,), min_bars=0, start_date=date(2024, 1, 1))
    )
    outside = service.overview(
        SwingOverviewConfig(reversal_multiples=(1.0,), min_bars=0, start_date=date(2024, 1, 15))
    )

    assert inside["scenarios"][0]["segment_count"] >= 1
    assert outside["scenarios"][0]["segment_count"] == 0


def test_service_reads_in_batches_and_reports_progress() -> None:
    frame = _frame(_path([0.01] * 30))
    adapter = _FakeAdapter({("SH", "600000"): frame, ("SH", "600001"): frame})
    progress: list[tuple[int, int]] = []

    result = SwingResearchService(adapter).overview(
        SwingOverviewConfig(batch_size=1),
        lambda done, total: progress.append((done, total)),
    )

    assert adapter.batch_sizes == [1, 1]
    assert result["scanned_stocks"] == 2
    assert progress == [(1, 2), (2, 2)]


def test_service_respects_stock_limit() -> None:
    frame = _frame(_path([0.01] * 30))
    adapter = _FakeAdapter({("SH", "600000"): frame, ("SH", "600001"): frame})

    result = SwingResearchService(adapter).overview(SwingOverviewConfig(limit_stocks=1))

    assert result["stock_count"] == 1
    assert result["scanned_stocks"] == 1


def test_config_normalization_rejects_unusable_thresholds() -> None:
    with pytest.raises(ValueError, match="反转阈值倍数"):
        SwingOverviewConfig(reversal_multiples=(0.0,)).normalized()
    with pytest.raises(ValueError, match="开始日期"):
        SwingOverviewConfig(
            start_date=date(2024, 6, 1),
            end_date=date(2024, 1, 1),
        ).normalized()

    normalized = SwingOverviewConfig(reversal_multiples=(3.0, 3.0, 2.0)).normalized()

    assert normalized.reversal_multiples == (3.0, 2.0)


_GIB = 1024**3


class _Usage(NamedTuple):
    """Stand-in for ``shutil.disk_usage`` - only ``free`` is ever read."""

    total: int
    used: int
    free: int


@pytest.fixture
def ample_resources(monkeypatch: pytest.MonkeyPatch):
    """A machine with no shortage of anything, so the ceiling is the only wall."""

    monkeypatch.setattr(
        swing_service.shutil, "disk_usage", lambda _path: _Usage(500 * _GIB, 0, 500 * _GIB)
    )
    monkeypatch.setattr(swing_service, "_available_memory_bytes", lambda: 32 * _GIB)


def test_panel_budget_allows_a_full_market_full_history_run(ample_resources) -> None:
    """The case that used to be refused: ~5.5 GB of panel plus the labels."""

    swing_service._check_panel_budget(6 * _GIB)


def test_panel_budget_rejects_a_run_past_the_ceiling(
    ample_resources, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(swing_service, "MAX_PANEL_BYTES", _GIB)

    with pytest.raises(ValueError, match="超出上限"):
        swing_service._check_panel_budget(2 * _GIB)


def test_panel_budget_rejects_a_run_that_exceeds_free_ram(
    ample_resources, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disk is not the binding constraint - an 8 GB box cannot hold a 6 GB run."""

    monkeypatch.setattr(swing_service, "_available_memory_bytes", lambda: 8 * _GIB)

    with pytest.raises(ValueError, match="可用内存"):
        swing_service._check_panel_budget(6 * _GIB)


def test_panel_budget_honours_an_explicit_memory_override(
    ample_resources, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(swing_service, "_available_memory_bytes", lambda: 8 * _GIB)
    monkeypatch.setenv("SWING_PANEL_MAX_MEMORY_BYTES", str(20 * _GIB))

    swing_service._check_panel_budget(6 * _GIB)


def test_panel_budget_rejects_a_run_that_will_not_fit_on_disk(
    ample_resources, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The panel is a real file: hearing it up front beats dying on ENOSPC."""

    monkeypatch.setattr(
        swing_service.shutil, "disk_usage", lambda _path: _Usage(total=0, used=0, free=_GIB)
    )

    with pytest.raises(ValueError, match="临时目录只剩"):
        swing_service._check_panel_budget(2 * _GIB)


def test_panel_budget_ignores_a_temp_root_it_cannot_stat(
    ample_resources, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_path: str) -> _Usage:
        raise OSError("no such temp root")

    monkeypatch.setattr(swing_service.shutil, "disk_usage", boom)

    swing_service._check_panel_budget(_GIB)
