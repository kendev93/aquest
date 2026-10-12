"""The factor test service: panel assembly, value resolution, budget."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from aquest.factor_test import panel as panel_module
from aquest.factor_test.models import FactorTestConfig
from aquest.factor_test.service import FactorTestService
from aquest.market_data.models import StockRef

DAYS = 320


class _FakeAdapter:
    """Serves prepared frames and records how they were requested."""

    def __init__(self, frames: dict[tuple[str, str], pd.DataFrame]) -> None:
        self._frames = frames
        self.requests: list[tuple[date | None, date | None]] = []

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
        self.requests.append((start_date, end_date))
        frames = []
        for ref in refs:
            frame = self._frames[(ref.market, ref.code)].copy()
            frame["market"] = ref.market
            frame["code"] = ref.code
            frames.append(frame)
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _frames(count: int = 24, *, seed: int = 5) -> dict[tuple[str, str], pd.DataFrame]:
    rng = np.random.default_rng(seed)
    frames: dict[tuple[str, str], pd.DataFrame] = {}
    for index in range(count):
        steps = rng.normal(scale=0.012, size=DAYS)
        closes = 10.0 * np.cumprod(1.0 + steps)
        frames[("SH", f"{600000 + index:06d}")] = pd.DataFrame(
            {
                "date": pd.date_range("2024-01-01", periods=DAYS, freq="D"),
                "open": closes,
                "high": closes * 1.01,
                "low": closes * 0.99,
                "close": closes,
                "volume": np.full(DAYS, 1_000.0),
                "amount": closes * 1_000.0,
            }
        )
    return frames


def _service(count: int = 24) -> tuple[FactorTestService, _FakeAdapter]:
    adapter = _FakeAdapter(_frames(count))
    return FactorTestService(adapter), adapter


def test_a_run_reports_every_selected_value() -> None:
    service, _adapter = _service()
    config = FactorTestConfig(
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
        value_ids=("technical.rsi14", "technical.bias20"),
        include_custom=False,
        permutations=0,
        min_cross_section=10,
    ).normalized()

    payload = service.run(config)

    assert payload["scanned_stocks"] == 24
    assert payload["value_count"] == 2
    # Reported strongest first, not in the order they were requested.
    assert {item["value_id"] for item in payload["factors"]} == {
        "technical.rsi14",
        "technical.bias20",
    }
    assert payload["dates"]
    assert payload["date_range"]["start"] == "2024-01-01"
    assert payload["boundaries"]
    # Without permutations there is no p-value, and the page must say so rather
    # than showing an empty column and letting the reader wonder.
    assert payload["reason"]


def test_the_default_selection_covers_the_technical_family() -> None:
    service, _adapter = _service()
    config = FactorTestConfig(
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
        permutations=0,
        min_cross_section=10,
    ).normalized()

    payload = service.run(config)

    ids = [str(item["value_id"]) for item in payload["factors"]]
    assert len(ids) > 5
    assert any(item.startswith("technical.") for item in ids)
    # 自定义公式的连续输出默认一并纳入（其余是常数、脉冲或伪装成价格的 EMA）。
    assert any(item.startswith("indicator_") for item in ids)


def test_an_alpha_the_test_cannot_reach_is_named_as_the_reason() -> None:
    """N permutations cannot produce a p-value below 1/(N+1).

    Left unsaid, tightening alpha turns every factor non-significant and reads
    as "these indicators are junk" when the test simply never could reach.
    """

    service, _adapter = _service()
    config = FactorTestConfig(
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
        value_ids=("technical.rsi14",),
        include_custom=False,
        permutations=99,
        alpha=0.005,
        min_cross_section=10,
    ).normalized()

    payload = service.run(config)

    assert payload["significant_count"] == 0
    assert payload["reason"] is not None
    assert "最小可能的 p 值" in payload["reason"]
    assert "0.010" in payload["reason"]


def test_an_unknown_value_is_rejected_before_any_work() -> None:
    service, _adapter = _service()
    config = FactorTestConfig(
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
        value_ids=("technical.close",),
    ).normalized()

    with pytest.raises(ValueError, match="不能用于因子检验"):
        service.run(config)


def test_an_empty_universe_reports_a_reason_instead_of_crashing() -> None:
    adapter = _FakeAdapter({})
    payload = FactorTestService(adapter).run(
        FactorTestConfig(start_date=date(2024, 1, 1), end_date=date(2024, 12, 31)).normalized()
    )

    assert payload["factors"] == []
    assert payload["reason"] == "所选范围内没有任何可用日线"


def test_the_panel_budget_is_checked_before_allocating() -> None:
    service, _adapter = _service()
    config = FactorTestConfig(
        start_date=date(2024, 1, 1), end_date=date(2024, 12, 31), permutations=0
    ).normalized()
    original = panel_module.MAX_PANEL_BYTES
    panel_module.MAX_PANEL_BYTES = 1
    try:
        with pytest.raises(ValueError, match="超出上限"):
            service.run(config)
    finally:
        panel_module.MAX_PANEL_BYTES = original


def test_the_second_pass_reads_past_the_window_for_labels() -> None:
    """Forward returns need bars that live after the study window ends."""

    service, adapter = _service(count=6)
    FactorTestService(adapter).run(
        FactorTestConfig(
            start_date=date(2024, 1, 1),
            end_date=date(2024, 6, 30),
            horizon=5,
            value_ids=("technical.rsi14",),
            include_custom=False,
            permutations=0,
            min_cross_section=3,
        ).normalized()
    )

    scan_end = adapter.requests[0][1]
    fill_end = adapter.requests[-1][1]
    assert scan_end == date(2024, 6, 30)
    assert fill_end is not None and fill_end > scan_end


def test_forward_returns_look_ahead_and_stop_at_the_end() -> None:
    frame = pd.DataFrame({"close": [10.0, 11.0, 12.0, 13.0]})
    result = panel_module._forward_returns(frame, horizon=2)

    assert result[0] == pytest.approx(0.2)
    assert result[1] == pytest.approx(2.0 / 11.0)
    assert np.isnan(result[2])
    assert np.isnan(result[3])
