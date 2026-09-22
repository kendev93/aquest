"""Trade attribution: pairing, the group comparison, and the service report."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from selector_app.factor_attribution.metrics import apply_fdr, evaluate_factor
from selector_app.factor_attribution.models import (
    CONSTANT_NOTE,
    LOW_SAMPLE_NOTE,
    NO_SPLIT_NOTE,
    AttributionFactor,
    FactorAttributionConfig,
)
from selector_app.factor_attribution.service import FactorAttributionService
from selector_app.factor_attribution.trades import RoundTrip, pair_round_trips
from selector_app.factor_attribution.values import collect_signal_values
from selector_app.market_data.models import StockRef
from selector_app.swing_research.scoring import block_permutations

DAYS = 400
START = date(2024, 1, 1)


def _day(offset: int) -> date:
    return START + timedelta(days=offset)


def _buy(
    code: str = "600000",
    *,
    offset: int = 10,
    market: str = "SH",
    cost_basis: float | None = 1000.0,
) -> dict[str, object]:
    return {
        "market": market,
        "code": code,
        "name": code,
        "direction": "BUY",
        "date": _day(offset).isoformat(),
        "signal_date": _day(offset).isoformat(),
        "pnl": 0.0,
        "cost_basis": cost_basis,
        "rejected": False,
    }


def _sell(code: str = "600000", *, offset: int = 20, pnl: float = 50.0) -> dict[str, object]:
    return {
        "market": "SH",
        "code": code,
        "name": code,
        "direction": "SELL",
        "date": _day(offset).isoformat(),
        "signal_date": _day(offset).isoformat(),
        "pnl": pnl,
        "cost_basis": 1000.0,
        "rejected": False,
    }


def test_fifo_closes_the_earliest_open_position_first() -> None:
    """Two buys, two sells: the pairing must not invent a long and a short."""

    sample = pair_round_trips(
        [
            _buy(offset=10),
            _buy(offset=11),
            _sell(offset=30, pnl=80.0),
            _sell(offset=31, pnl=-40.0),
        ]
    )
    assert sample.total == 2
    assert [trip.entry_date for trip in sample.trips] == [_day(10), _day(11)]
    assert [trip.win for trip in sample.trips] == [True, False]
    assert sample.open_positions == 0


def test_positions_still_open_on_the_last_day_are_not_trades() -> None:
    """An unclosed entry has no realised P&L; marking it would invent a win."""

    sample = pair_round_trips([_buy(offset=10), _sell(offset=20, pnl=10.0), _buy(offset=30)])
    assert sample.total == 1
    assert sample.open_positions == 1
    assert sample.trips[0].win is True


def test_orphan_sells_are_dropped_and_counted() -> None:
    sample = pair_round_trips([_sell(offset=20, pnl=10.0)])
    assert sample.total == 0
    assert sample.orphan_sells == 1


def test_rejected_and_unusable_rows_are_skipped() -> None:
    rejected = _buy(offset=10)
    rejected |= {"rejected": True}
    headless = {"direction": "BUY", "code": "600000", "market": "SH", "date": ""}
    sample = pair_round_trips([rejected, headless, _buy(offset=12), _sell(offset=30, pnl=5.0)])
    assert sample.total == 1
    assert sample.rejected == 1
    assert sample.unparsable == 1


def test_a_breakeven_trade_counts_as_a_loss() -> None:
    sample = pair_round_trips([_buy(offset=10), _sell(offset=20, pnl=0.0)])
    assert sample.total == 1
    assert sample.trips[0].net_return == 0.0
    assert sample.trips[0].win is False


def test_return_is_net_and_trips_are_ordered_by_signal_day() -> None:
    sample = pair_round_trips(
        [
            _buy(code="600001", offset=40),
            _sell(code="600001", offset=50, pnl=-120.0),
            _buy(code="600000", offset=10),
            _sell(code="600000", offset=20, pnl=250.0),
        ]
    )
    assert [trip.signal_date for trip in sample.trips] == [_day(10), _day(40)]
    assert sample.trips[0].net_return == pytest.approx(0.25)
    assert sample.trips[1].net_return == pytest.approx(-0.12)


def _factor(
    values: list[float],
    wins: list[bool],
    *,
    orders: list[np.ndarray] | None = None,
    min_group_trades: int = 2,
) -> AttributionFactor:
    return evaluate_factor(
        np.asarray(values, dtype=float),
        np.asarray(wins, dtype=bool),
        value_id="technical.rsi14",
        display_name="RSI14",
        orders=[] if orders is None else orders,
        min_group_trades=min_group_trades,
    )


def test_a_value_that_separates_the_groups_is_flagged() -> None:
    rng = np.random.default_rng(11)
    win_values = rng.normal(loc=62.0, scale=4.0, size=40)
    loss_values = rng.normal(loc=44.0, scale=4.0, size=40)
    values = np.concatenate([win_values, loss_values])
    wins = np.concatenate([np.ones(40, dtype=bool), np.zeros(40, dtype=bool)])
    orders = block_permutations(values.size, block_size=8, count=200, seed=3)

    factor = _factor(list(values), list(wins), orders=orders, min_group_trades=20)

    assert factor.win_mean is not None and factor.loss_mean is not None
    assert factor.difference == pytest.approx(factor.win_mean - factor.loss_mean)
    assert factor.difference and factor.difference > 10.0
    assert factor.standardized and factor.standardized > 2.0
    assert factor.p_value is not None and factor.p_value <= 0.01


def test_a_group_too_small_is_described_but_never_judged() -> None:
    rng = np.random.default_rng(5)
    values = list(rng.normal(size=25))
    wins = [True] * 5 + [False] * 20
    orders = block_permutations(25, block_size=8, count=50, seed=3)

    factor = _factor(values, wins, orders=orders, min_group_trades=20)

    # The difference is still worth showing; what it is not worth is a verdict.
    assert factor.difference is not None
    assert factor.note == LOW_SAMPLE_NOTE
    assert factor.p_value is None
    assert factor.significant is None


def test_missing_values_are_dropped_instead_of_filled_with_zero() -> None:
    """Filling a gap with 0 would invent "winners sat at zero" out of nothing."""

    values = [float("nan"), 10.0, 20.0, 30.0, float("nan"), 40.0]
    wins = [True, True, True, False, False, False]
    orders = block_permutations(6, block_size=2, count=20, seed=3)

    factor = _factor(values, wins, orders=orders)

    assert factor.missing == 2
    assert factor.win_count == 2
    assert factor.loss_count == 2
    assert factor.win_mean == pytest.approx(15.0)
    assert factor.loss_mean == pytest.approx(35.0)


def test_a_value_with_no_readings_has_nothing_to_say() -> None:
    factor = _factor([float("nan")] * 6, [True, True, True, False, False, False])
    assert factor.missing == 6
    assert factor.difference is None
    assert factor.p_value is None


def test_a_one_sided_sample_cannot_be_compared() -> None:
    factor = _factor([1.0, 2.0, 3.0, 4.0], [True, True, True, True])
    assert factor.note == NO_SPLIT_NOTE
    assert factor.difference is None


def test_a_constant_value_cannot_separate_anything() -> None:
    factor = _factor([5.0] * 20, [True] * 10 + [False] * 10)
    assert factor.note == CONSTANT_NOTE
    assert factor.difference == pytest.approx(0.0)


def test_fdr_is_applied_once_over_the_whole_family() -> None:
    def make(pvalue: float | None, note: str | None) -> AttributionFactor:
        return AttributionFactor(
            value_id="x",
            display_name="X",
            win_mean=1.0,
            loss_mean=0.0,
            difference=1.0,
            standardized=1.0,
            win_count=30,
            loss_count=30,
            missing=0,
            p_value=pvalue,
            note=note,
        )

    adjusted = apply_fdr(
        [
            make(0.001, None),
            make(0.04, None),
            make(0.9, None),
            make(None, LOW_SAMPLE_NOTE),
        ],
        alpha=0.05,
    )

    assert adjusted[0].significant is True
    assert adjusted[0].adjusted_p_value == pytest.approx(0.004, abs=1e-3)
    # 0.04 looks publishable on its own and stops being one once the family of
    # three is counted; the last was never tested, so it must not be called
    # insignificant either - "no verdict" is not "no effect".
    assert adjusted[1].adjusted_p_value == pytest.approx(0.06, abs=1e-3)
    assert adjusted[1].significant is False
    assert adjusted[3].significant is None
    assert adjusted[3].adjusted_p_value is None


class _FakeAdapter:
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
        return [StockRef(market=market, code=code, path=Path(".")) for market, code in self._frames]

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
            frame = self._frames.get((ref.market, ref.code))
            if frame is None:
                continue  # An instrument the repository cannot serve at all.
            frame = frame.copy()
            frame["market"] = ref.market
            frame["code"] = ref.code
            frames.append(frame)
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


class _FakeStore:
    def __init__(self, record: Mapping[str, object] | None) -> None:
        self._record = record

    def read_backtest_history(self, history_id: str) -> Mapping[str, object] | None:
        return self._record if history_id == "h1" else None


def _frames(count: int = 8, *, seed: int = 7) -> dict[tuple[str, str], pd.DataFrame]:
    """Prices that fall for half the window and rise for the other half.

    The design is the point: entries taken in the falling half lose and entries
    taken in the rising half win, so any indicator that tracks the recent move
    really does separate the two groups. A flat random walk would leave the test
    asserting nothing.
    """

    frames: dict[tuple[str, str], pd.DataFrame] = {}
    for index in range(count):
        # Every stock gets the same draw on purpose: cross-stock variation is
        # not what this test is about, and leaving it in buries the difference
        # between the two groups under differences between the eight names.
        rng = np.random.default_rng(seed)
        trend = np.linspace(-0.005, 0.005, DAYS)
        closes = 10.0 * np.cumprod(1.0 + trend + rng.normal(scale=0.004, size=DAYS))
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


def _trending_trades(count: int = 8, *, per_stock: int = 12) -> list[dict[str, object]]:
    """Half the entries fire in the falling half (they lose), half in the rise."""

    trades: list[dict[str, object]] = []
    stocks = [("SH", f"{600000 + index:06d}") for index in range(count)]
    half = per_stock // 2
    for stock_index, (market, code) in enumerate(stocks):
        for slot in range(per_stock):
            losing = slot < half
            # Losers fire deep in the falling half, winners deep in the rising
            # half. Overlapping the two would blunt the signal the test asserts.
            # One day per stock on purpose: a real backtest spreads its entries
            # over many signal days, and packing them onto a handful would leave
            # the permutation with almost nothing to shuffle.
            offset = 20 + (slot % half) * 15 + (0 if losing else 250) + stock_index
            trades.append(_buy(code, offset=offset, market=market))
            trades.append(_sell(code, offset=offset + 6, pnl=-120.0 if losing else 180.0))
    return trades


def _history(trades: list[dict[str, object]]) -> dict[str, object]:
    return {
        "history_id": "h1",
        "backtest_type": "portfolio",
        "finished_at": "2024-06-01T00:00:00Z",
        "request": {"start_date": "2024-01-01", "end_date": "2024-12-31"},
        "result": {"trades": trades},
    }


def _service(
    trades: list[dict[str, object]] | None,
    *,
    frames: dict[tuple[str, str], pd.DataFrame] | None = None,
) -> FactorAttributionService:
    record = None if trades is None else _history(trades)
    return FactorAttributionService(
        adapter=_FakeAdapter(frames or _frames()),
        store=_FakeStore(record),  # type: ignore[arg-type]
    )


def _config(**kwargs: object) -> FactorAttributionConfig:
    defaults: dict[str, object] = {
        "source_history_id": "h1",
        "value_ids": ("technical.rsi14",),
        "include_custom": False,
        "permutations": 100,
    }
    defaults.update(kwargs)
    return FactorAttributionConfig(**defaults)  # type: ignore[arg-type]


def _factor_by_id(report: Mapping[str, object], value_id: str) -> Mapping[str, object]:
    factors = report["factors"]
    assert isinstance(factors, list)
    for item in factors:
        if item["value_id"] == value_id:
            return item
    raise AssertionError(f"{value_id} 不在报告里: {[item['value_id'] for item in factors]}")


def test_a_missing_history_is_rejected_before_anything_runs() -> None:
    with pytest.raises(ValueError, match="找不到这条回测记录"):
        _service(_trending_trades()).run(_config(source_history_id="other"))


def test_history_without_trades_is_rejected() -> None:
    with pytest.raises(ValueError, match="没有成交明细"):
        _service([]).run(_config())


def test_too_few_closed_trades_stops_before_any_statistics() -> None:
    trades = _trending_trades(count=2, per_stock=2)
    report = _service(trades).run(_config())

    assert report["samples"].__class__ is dict
    samples = report["samples"]
    assert isinstance(samples, dict)
    assert samples["trades"] == 4
    assert report["factors"] == []
    reason = report["reason"]
    assert isinstance(reason, str) and "不足 10 笔" in reason


def test_the_report_names_the_value_that_separated_the_groups() -> None:
    report = _service(_trending_trades()).run(_config())

    samples = report["samples"]
    assert isinstance(samples, dict)
    assert samples["trades"] == 96
    assert samples["wins"] == 48
    assert samples["losses"] == 48
    assert samples["signal_days"] >= 10

    rsi = _factor_by_id(report, "technical.rsi14")
    assert rsi["win_count"] == 48 and rsi["loss_count"] == 48
    assert rsi["win_mean"] is not None and rsi["loss_mean"] is not None
    assert rsi["win_mean"] > rsi["loss_mean"]
    assert rsi["difference"] and rsi["difference"] > 0
    # Winners were entered while the move was already up, so the gap is large
    # enough to clear the noise floor even after the family-wide correction.
    assert rsi["p_value"] is not None and rsi["p_value"] <= 0.05
    assert rsi["significant"] is True
    assert isinstance(report["significant_count"], int) and report["significant_count"] >= 1


def test_a_run_without_permutations_says_so_instead_of_reporting_none() -> None:
    report = _service(_trending_trades()).run(_config(permutations=0))
    reason = report["reason"]
    assert isinstance(reason, str) and "未做噪声门槛检验" in reason
    assert _factor_by_id(report, "technical.rsi14")["significant"] is None


def test_an_alpha_the_test_cannot_reach_is_explained() -> None:
    report = _service(_trending_trades()).run(_config(permutations=20, alpha=0.01))
    reason = report["reason"]
    assert isinstance(reason, str) and "最小可能的 p 值" in reason


def test_signal_days_outside_the_stored_bars_are_dropped_not_filled() -> None:
    """An entry whose signal day has no bar must leave the sample, not enter it
    at zero - otherwise a missing reading becomes the strongest "finding"."""

    trips = (
        RoundTrip(
            key="SH600000",
            market="SH",
            code="600000",
            name="600000",
            signal_date=_day(30),
            entry_date=_day(30),
            exit_date=_day(36),
            net_return=0.1,
            win=True,
        ),
        RoundTrip(
            key="SH600000",
            market="SH",
            code="600000",
            name="600000",
            # Far past the stored window: no bar, so no value.
            signal_date=START + timedelta(days=DAYS + 50),
            entry_date=START + timedelta(days=DAYS + 50),
            exit_date=START + timedelta(days=DAYS + 56),
            net_return=-0.1,
            win=False,
        ),
    )
    refs = [StockRef(market="SH", code="600000", path=Path("."))]
    signal = collect_signal_values(
        _FakeAdapter(_frames(1)),  # type: ignore[arg-type]
        refs,
        trips,
        ("technical.rsi14",),
    )

    assert np.isfinite(signal.values[0]).all()
    assert not np.isfinite(signal.values[1]).any()
    # The in-window trade still knows how long it was held; the other one does not.
    assert signal.holding_days[0] == 6
    assert signal.holding_days[1] == 0


def test_a_stock_with_no_bars_at_all_is_reported_as_missing() -> None:
    trips = (
        RoundTrip(
            key="SZ000001",
            market="SZ",
            code="000001",
            name="000001",
            signal_date=_day(30),
            entry_date=_day(30),
            exit_date=_day(36),
            net_return=0.1,
            win=True,
        ),
    )
    refs = [StockRef(market="SZ", code="000001", path=Path("."))]
    signal = collect_signal_values(
        _FakeAdapter(_frames(1)),  # type: ignore[arg-type]
        refs,
        trips,
        ("technical.rsi14",),
    )

    assert signal.missing_stocks == ("SZ000001",)
    assert not np.isfinite(signal.values).any()


def test_a_really_unbalanced_holding_period_is_called_out() -> None:
    """If the winners were simply held longer, the indicator gap may be an
    artefact of the holding period - the report has to say so."""

    from selector_app.factor_attribution import service as service_module

    trades: list[dict[str, object]] = []
    for index in range(8):
        for slot in range(3):
            # Slots are 40 days apart so that one slot's exit cannot be paired
            # with the next slot's entry by mistake.
            offset = 20 + slot * 40
            code = f"{600000 + index:06d}"
            trades.append(_buy(code, offset=offset))
            # Losers exit in two days, winners sit for a month.
            trades.append(_sell(code, offset=offset + 2, pnl=-50.0))
            trades.append(_buy(code, offset=offset + 5))
            trades.append(_sell(code, offset=offset + 35, pnl=90.0))

    report = _service(trades).run(_config(permutations=20))
    notes = report["notes"]
    assert isinstance(notes, list)
    assert any("持有" in note for note in notes), notes
    samples = report["samples"]
    assert isinstance(samples, dict)
    assert samples["win_holding_days"] and samples["loss_holding_days"]
    assert samples["win_holding_days"] > samples["loss_holding_days"]
    assert service_module.MIN_SIGNAL_DAYS >= 10
