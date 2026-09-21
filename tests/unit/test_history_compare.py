"""History comparison: curve extraction, metric honesty, and comparability warnings."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from selector_app.history_compare import HistoryCompareService
from selector_app.history_compare.extract import extract_curve, extract_metrics
from selector_app.market_data.store import DuckDbMarketDataStore


def _portfolio_result(
    *,
    start_cash: float | None = 100_000.0,
    dates: tuple[str, ...] = ("2024-01-02", "2024-01-03", "2024-01-04"),
    totals: tuple[float, ...] = (100_000.0, 110_000.0, 90_000.0),
) -> dict[str, object]:
    performance: dict[str, object] = {
        "total_return": -0.1,
        "annual_return": -0.05,
        "max_drawdown": 0.18,
        "sharpe": -0.4,
        "win_rate": 0.5,
        "total_trades": 6,
        "end_value": 90_000.0,
        "turnover": 1.5,
        "max_dd_duration": 12,
    }
    if start_cash is not None:
        performance["start_cash"] = start_cash
    return {
        "universe": "all",
        "bars": len(dates),
        "start_date": dates[0],
        "end_date": dates[-1],
        "processed": 40,
        "performance": performance,
        "equity_curve": [
            {"date": date, "total": total} for date, total in zip(dates, totals, strict=True)
        ],
        "benchmark": {"performance": {"total_return": -0.12, "max_drawdown": 0.21}},
    }


def _factor_result(*, long_short: bool = True) -> dict[str, object]:
    factor: dict[str, object] = {
        "value_id": "technical.rsi14",
        "display_name": "RSI14",
        "ic_mean": 0.031,
        "icir": 0.42,
        "significant": True,
        "ic_cumulative": [0.01, 0.02, 0.031],
        "long_short": {},
    }
    if long_short:
        factor["long_short"] = {
            "mean_spread": 0.001,
            "win_rate": 0.58,
            "cumulative": [0.001, 0.002, 0.003],
        }
    return {
        "dates": ["2024-01-02", "2024-01-03", "2024-01-04"],
        "date_range": {"start": "2024-01-02", "end": "2024-01-04", "days": 3},
        "scanned_stocks": 300,
        "significant_count": 1,
        "factors": [factor],
    }


def _condition_result() -> dict[str, object]:
    return {
        "universe": "sh",
        "start_date": "2024-01-02",
        "end_date": "2024-06-30",
        "processed": 120,
        "candidates": [
            {
                "side": "buy",
                "condition": {
                    "left_value": "technical.rsi14",
                    "operator": "<",
                    "right_constant": 30.0,
                },
                "mining": {
                    "lift": 1.42,
                    "baseline_rate": 0.31,
                    "support_count": 40,
                    "significant": True,
                },
                "passed_constraints": True,
                "holdout": {
                    "pooled_trades": {"closed_trades": 88, "win_rate": 0.61},
                    "per_stock": {"median_total_return": 0.07},
                },
            }
        ],
    }


# ---------------------------------------------------------------------------
# 曲线
# ---------------------------------------------------------------------------


def test_equity_curve_defaults_to_capital_normalization() -> None:
    curve, reason = extract_curve("portfolio", _portfolio_result(), normalize="capital")

    assert reason is None
    assert curve is not None
    assert curve.kind == "equity"
    assert curve.normalize == "capital"
    assert [point["value"] for point in curve.points] == [1.0, 1.1, 0.9]


def test_equity_curve_falls_back_to_first_value_and_says_so() -> None:
    curve, reason = extract_curve(
        "portfolio", _portfolio_result(start_cash=None), normalize="capital"
    )

    assert reason is None
    assert curve is not None
    assert curve.normalize == "first"
    assert curve.note and "按首值归一" in curve.note
    assert curve.points[0]["value"] == 1.0


def test_equity_curve_reports_why_it_cannot_be_drawn() -> None:
    curve, reason = extract_curve(
        "portfolio", _portfolio_result(totals=(0.0, 0.0, 0.0), start_cash=None), normalize="capital"
    )

    assert curve is None
    assert reason and "无法归一化" in reason


def test_types_without_curves_say_so_instead_of_returning_empty_points() -> None:
    for backtest_type in ("strategy_fitness", "condition_research", "swing_research"):
        curve, reason = extract_curve(backtest_type, {}, normalize="capital")
        assert curve is None
        assert reason and "曲线" in reason


def test_factor_curve_prefers_long_short_and_is_never_normalized() -> None:
    curve, reason = extract_curve("factor_test", _factor_result(), normalize="capital")

    assert reason is None
    assert curve is not None
    assert curve.kind == "factor_long_short"
    assert curve.normalize == "none"
    assert curve.label == "RSI14"
    assert curve.note and "不是资金曲线" in curve.note


def test_factor_curve_falls_back_to_cumulative_ic() -> None:
    curve, reason = extract_curve(
        "factor_test", _factor_result(long_short=False), normalize="capital"
    )

    assert reason is None
    assert curve is not None
    assert curve.kind == "factor_ic"
    assert [point["value"] for point in curve.points] == [0.01, 0.02, 0.031]


def test_factor_curve_needs_at_least_two_points() -> None:
    payload = _factor_result()
    payload["factors"] = [
        {
            "value_id": "technical.rsi14",
            "display_name": "RSI14",
            "long_short": {},
            "ic_cumulative": [0.1],
        }
    ]
    curve, reason = extract_curve("factor_test", payload, normalize="capital")

    assert curve is None
    assert reason and "累计序列" in reason


# ---------------------------------------------------------------------------
# 指标：拿不出来就说拿不出来，不填 0
# ---------------------------------------------------------------------------


def test_research_types_do_not_invent_returns() -> None:
    for backtest_type, payload in (
        ("condition_research", _condition_result()),
        ("swing_research", {"scanned_stocks": 200, "significant_count": 3}),
        ("factor_test", _factor_result()),
    ):
        metrics, _ = extract_metrics(backtest_type, payload)
        by_key = {metric.key: metric for metric in metrics}
        assert by_key["total_return"].value is None
        assert by_key["total_return"].note and "资金曲线" in by_key["total_return"].note


def test_portfolio_metrics_carry_their_source() -> None:
    metrics, native = extract_metrics("portfolio", _portfolio_result())

    by_key = {metric.key: metric for metric in metrics}
    assert by_key["total_return"].value == -0.1
    assert by_key["total_return"].source == "组合资金曲线"
    assert by_key["sample_days"].value == 3
    assert by_key["instrument_count"].value == 40
    assert any(metric.key == "benchmark_return" and metric.value == -0.12 for metric in native)


def test_condition_metrics_read_the_holdout_segment() -> None:
    metrics, native = extract_metrics("condition_research", _condition_result())

    by_key = {metric.key: metric for metric in metrics}
    assert by_key["win_rate"].value == 0.61
    assert by_key["trade_count"].value == 88
    assert by_key["significant_count"].value == 1
    assert any(metric.key == "top_lift" and metric.value == 1.42 for metric in native)
    assert any(
        metric.key == "top_condition" and metric.text == "technical.rsi14 < 30" for metric in native
    )


def test_fitness_medians_skip_missing_values() -> None:
    payload = {
        "bars": 240,
        "results": [
            {"passed": True, "test": {"total_return": 0.2, "sharpe": 1.1}},
            {"passed": True, "test": {"total_return": 0.1, "sharpe": 0.9}},
            {"passed": False, "test": {}},
        ],
    }
    metrics, native = extract_metrics("strategy_fitness", payload)

    by_key = {metric.key: metric for metric in metrics}
    # 缺失的那条不能当 0 参与中位数，否则中位收益会掉到 0.1 以下。
    assert by_key["total_return"].value is not None
    assert abs(by_key["total_return"].value - 0.15) < 1e-9
    assert by_key["significant_count"].value == 2
    assert by_key["instrument_count"].value == 3
    assert any(metric.key == "pass_rate" and abs(metric.value - 2 / 3) < 1e-9 for metric in native)


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------


def _store_with(
    tmp_path, records: list[tuple[str, str, dict[str, object]]]
) -> DuckDbMarketDataStore:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index, (history_id, backtest_type, result) in enumerate(records):
        store.save_backtest_history(
            history_id,
            backtest_type=backtest_type,
            request={},
            result=result,
            started_at=base + timedelta(minutes=index),
            finished_at=base + timedelta(minutes=index, seconds=30),
        )
    return store


def test_compare_rejects_too_few_and_too_many(tmp_path) -> None:
    store = _store_with(tmp_path, [("p1", "portfolio", _portfolio_result())])
    service = HistoryCompareService(store)

    try:
        service.compare(["p1"])
    except ValueError as exc:
        assert "至少" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected a ValueError")

    try:
        service.compare([f"p{index}" for index in range(6)])
    except ValueError as exc:
        assert "最多" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected a ValueError")


def test_compare_rejects_unknown_ids(tmp_path) -> None:
    store = _store_with(tmp_path, [("p1", "portfolio", _portfolio_result())])
    service = HistoryCompareService(store)

    try:
        service.compare(["p1", "nope"])
    except ValueError as exc:
        assert "nope" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected a ValueError")


def test_two_portfolios_are_plotted_together(tmp_path) -> None:
    store = _store_with(
        tmp_path,
        [
            ("p1", "portfolio", _portfolio_result()),
            ("p2", "portfolio", _portfolio_result(totals=(100_000.0, 105_000.0, 120_000.0))),
        ],
    )
    report = HistoryCompareService(store).compare(["p1", "p2"])

    assert report.curve_group["kind"] == "equity"
    assert report.curve_group["plotted"] is True
    assert report.curve_group["history_ids"] == ["p1", "p2"]
    assert report.warnings == ()


def test_mixed_curve_kinds_are_never_plotted_together(tmp_path) -> None:
    store = _store_with(
        tmp_path,
        [("p1", "portfolio", _portfolio_result()), ("f1", "factor_test", _factor_result())],
    )
    report = HistoryCompareService(store).compare(["p1", "f1"])

    assert report.curve_group["plotted"] is False
    assert report.curve_group["excluded"]
    assert any("多种曲线口径" in warning for warning in report.warnings)
    assert any("跨类型对比" in warning for warning in report.warnings)


def test_cross_type_warning_names_the_types(tmp_path) -> None:
    store = _store_with(
        tmp_path,
        [
            ("p1", "portfolio", _portfolio_result()),
            ("c1", "condition_research", _condition_result()),
        ],
    )
    report = HistoryCompareService(store).compare(["p1", "c1"])

    assert any("条件研究" in warning and "定性参照" in warning for warning in report.warnings)


def test_non_overlapping_ranges_are_called_out(tmp_path) -> None:
    old = _portfolio_result(dates=("2020-01-02", "2020-01-03"), totals=(100_000.0, 110_000.0))
    old["start_date"] = "2020-01-02"
    old["end_date"] = "2020-01-03"
    new = _portfolio_result()
    store = _store_with(tmp_path, [("old", "portfolio", old), ("new", "portfolio", new)])

    report = HistoryCompareService(store).compare(["old", "new"])

    assert any("没有重叠" in warning for warning in report.warnings)


def test_sample_length_gap_is_called_out(tmp_path) -> None:
    short = _portfolio_result()
    long_result = _portfolio_result()
    long_result["bars"] = 900
    store = _store_with(
        tmp_path, [("short", "portfolio", short), ("long", "portfolio", long_result)]
    )

    report = HistoryCompareService(store).compare(["short", "long"])

    assert any("样本长度相差" in warning for warning in report.warnings)


def test_long_curves_are_downsampled_but_keep_both_ends(tmp_path, monkeypatch) -> None:
    dates = tuple(f"2024-{month:02d}-01" for month in range(1, 13))
    totals = tuple(100_000.0 + index * 1_000 for index in range(12))
    result = _portfolio_result(dates=dates, totals=totals)
    monkeypatch.setattr("selector_app.history_compare.service.MAX_CURVE_POINTS", 5)
    store = _store_with(tmp_path, [("p1", "portfolio", result), ("p2", "portfolio", result)])

    report = HistoryCompareService(store).compare(["p1", "p2"])

    curve = report.items[0].curve
    assert curve is not None
    assert curve.truncated is True
    assert len(curve.points) == 5
    assert curve.points[0]["index"] == 0
    assert curve.points[-1]["index"] == 11


def test_candidates_list_every_type_with_expected_curve_kind(tmp_path) -> None:
    store = _store_with(
        tmp_path,
        [
            ("p1", "portfolio", _portfolio_result()),
            ("f1", "factor_test", _factor_result()),
            ("c1", "condition_research", _condition_result()),
        ],
    )
    payload = HistoryCompareService(store).candidates()

    kinds = {item["backtest_type"]: item["curve_kind"] for item in payload["items"]}
    assert kinds["portfolio"] == "equity"
    assert kinds["factor_test"] == "factor_long_short"
    assert kinds["condition_research"] is None
    assert {item["backtest_type"] for item in payload["types"]} == {
        "portfolio",
        "factor_test",
        "condition_research",
    }
    assert payload["max_items"] == 5


def test_records_without_a_benchmark_do_not_show_empty_benchmark_rows() -> None:
    """基准对照是后加的，老记录里没有这个块；两行恒为「—」只是噪音。"""

    result = _portfolio_result()
    result.pop("benchmark")

    _, native = extract_metrics("portfolio", result)

    assert not [metric for metric in native if metric.key.startswith("benchmark_")]


def test_condition_without_qualified_candidates_says_where_the_best_came_from() -> None:
    payload = _condition_result()
    payload["candidates"][0]["passed_constraints"] = False

    _, native = extract_metrics("condition_research", payload)

    lift = [metric for metric in native if metric.key == "top_lift"][0]
    assert [metric for metric in native if metric.key == "qualified_count"][0].value == 0
    assert lift.note and "全部候选" in lift.note


def test_items_carry_a_short_id_so_lookalike_records_can_be_told_apart(tmp_path) -> None:
    store = _store_with(
        tmp_path,
        [("p1", "portfolio", _portfolio_result()), ("p2", "portfolio", _portfolio_result())],
    )

    report = HistoryCompareService(store).compare(["p1", "p2"])

    # 同一个区间、同一批标的的两次回测，标题会一字不差。
    assert report.items[0].label == report.items[1].label
    assert report.items[0].short_id == "p1"
    assert report.items[1].short_id == "p2"
    assert report.to_dict()["items"][0]["short_id"] == "p1"
