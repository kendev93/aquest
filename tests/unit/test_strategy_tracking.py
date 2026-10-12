"""策略追踪：重算、幂等、失败不动日期。

这里测的是最容易被写错、写错了又最难发现的口径：

1. 新追踪在上线日以原始资金和空仓建立初始点，既有追踪维持旧口径。
2. 同一天重算几次结果都一样；漏了几天能一次补齐。
3. **失败只停���旧日期**——静默改成"今天"会让用户以为数据已经更新了，
   而实际那几天什么都没算。
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from aquest.market_data.store import DuckDbMarketDataStore
from aquest.portfolio_backtest import tracking as tracking_module
from aquest.portfolio_backtest.auto_tracking import StrategyTrackingScheduler
from aquest.portfolio_backtest.tracking import (
    MIN_OBSERVATION_BARS,
    StrategyTrackingService,
    build_observations,
    build_segments,
)

_LAUNCH_DAY = 20260302
_REQUEST: dict[str, object] = {
    "universe": "all",
    "ranking_value": "indicator_one.wash",
    "rank_order": "desc",
    "selected_signals": ["indicator_one.main_force_entry"],
    "stop_loss_pct": 0.1,
    "max_positions": 5,
    "date_mode": "custom",
    "start_date": "2026-02-02",
    "end_date": "2026-03-02",
    "initial_cash": 100_000.0,
}

# 十个交易日，上线日落在第 6 个。
_DATES = (
    20260202,
    20260203,
    20260204,
    20260205,
    20260206,
    _LAUNCH_DAY,
    20260303,
    20260304,
    20260305,
    20260306,
)


def _business_dates(count: int) -> tuple[int, ...]:
    """``count`` 个工作日，从 2026-01-02 起。整数 ``YYYYMMDD``."""

    day = datetime.date(2026, 1, 2)
    out: list[int] = []
    while len(out) < count:
        if day.weekday() < 5:
            out.append(int(day.strftime("%Y%m%d")))
        day += datetime.timedelta(days=1)
    return tuple(out)


def _report(
    totals: tuple[float, ...],
    *,
    dates: tuple[int, ...] = _DATES,
    benchmark_totals: tuple[float, ...] | None = None,
    trades: tuple[dict[str, Any], ...] = (),
    next_plan: dict[str, Any] | None = None,
    current_state: dict[str, Any] | None = None,
    today_candidates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Shape one portfolio report the way the service sees it."""

    texts = [f"{str(value)[:4]}-{str(value)[4:6]}-{str(value)[6:]}" for value in dates]
    return {
        "equity_curve": [
            {
                "date": date,
                "cash": 1000.0,
                "position_value": total - 1000.0,
                "total": total,
                "positions_count": 3,
            }
            for date, total in zip(texts, totals, strict=True)
        ],
        "benchmark": (
            {
                "dates": list(dates),
                "totals": list(benchmark_totals),
            }
            if benchmark_totals is not None
            else None
        ),
        "trades": [dict(trade) for trade in trades],
        "next_plan": dict(next_plan) if next_plan is not None else {},
        "current_state": dict(current_state) if current_state is not None else None,
        "today_candidates": dict(today_candidates) if today_candidates is not None else None,
    }


class _FakeRunner:
    """Stand-in for the portfolio service: records every replay it was asked for."""

    def __init__(self, report: dict[str, Any]) -> None:
        self.report = report
        self.replayed: list[Any] = []
        self.configs: list[Any] = []

    def run(self, config: Any, progress_callback: Any = None) -> dict[str, Any]:
        del progress_callback
        self.replayed.append(config.end_date)
        self.configs.append(config)
        start = int(config.start_date.strftime("%Y%m%d"))
        end = int(config.end_date.strftime("%Y%m%d"))
        if start <= int(_REQUEST["start_date"].replace("-", "")):
            result = {
                **self.report,
                "equity_curve": [
                    row
                    for row in self.report.get("equity_curve", [])
                    if start <= int(str(row["date"]).replace("-", "")) <= end
                ],
                "trades": [
                    trade
                    for trade in self.report.get("trades", [])
                    if start <= int(str(trade["date"]).replace("-", "")) <= end
                ],
            }
            benchmark = self.report.get("benchmark")
            if isinstance(benchmark, dict):
                selected = [
                    (date, total)
                    for date, total in zip(
                        benchmark.get("dates", []), benchmark.get("totals", []), strict=True
                    )
                    if start <= int(date) <= end
                ]
                result["benchmark"] = {
                    "dates": [date for date, _ in selected],
                    "totals": [total for _, total in selected],
                }
            return result
        if config.start_date > datetime.date.fromisoformat(_REQUEST["start_date"]):
            rows = [
                row
                for row in self.report.get("equity_curve", [])
                if start <= int(str(row["date"]).replace("-", "")) <= end
            ]
            if not rows:
                return {**self.report, "equity_curve": [], "trades": []}
            initial = float(config.initial_cash)
            baseline = float(rows[0]["total"])
            equity_curve = []
            for index, row in enumerate(rows):
                total = initial + float(row["total"]) - baseline
                cash = initial if index == 0 else float(row["cash"])
                equity_curve.append(
                    {
                        **row,
                        "cash": cash,
                        "position_value": total - cash,
                        "total": total,
                        "positions_count": 0 if index == 0 else int(row["positions_count"]),
                    }
                )
            trades = [
                trade
                for trade in self.report.get("trades", [])
                if start <= int(str(trade["date"]).replace("-", "")) <= end
            ]
            result = {**self.report, "equity_curve": equity_curve, "trades": trades}
            benchmark = self.report.get("benchmark")
            if isinstance(benchmark, dict):
                selected = [
                    (date, total)
                    for date, total in zip(
                        benchmark.get("dates", []), benchmark.get("totals", []), strict=True
                    )
                    if start <= int(date) <= end
                ]
                result["benchmark"] = {
                    "dates": [date for date, _ in selected],
                    "totals": [total for _, total in selected],
                }
            return result
        return self.report


def _explode(config: Any, progress_callback: Any = None) -> dict[str, Any]:
    del config, progress_callback
    raise RuntimeError("行情缺失")


def _service(
    tmp_path: Any,
    runner: _FakeRunner,
    *,
    available_until: Any = "2026-03-02",
) -> StrategyTrackingService:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    latest_date = available_until if callable(available_until) else lambda: available_until
    return StrategyTrackingService(
        store=store,
        runner=runner,
        available_until=latest_date,
    )


def _save(store: DuckDbMarketDataStore) -> None:
    store.save_strategy("strategy-1", name="上线策略", request=dict(_REQUEST), summary={})


def test_observations_start_on_the_launch_day() -> None:
    """The generic full-report slicer includes the launch-day observation row."""

    report = _report(tuple(100000.0 + index * 1000 for index in range(10)))

    rows = build_observations(report, launched_at=_LAUNCH_DAY)

    assert [row.trade_date for row in rows] == list(_DATES[5:])
    assert rows[0].equity == 105000.0


def test_individual_strategy_tracking_replays_one_code_without_ranking(tmp_path: Any) -> None:
    request = {
        "universe": "codes",
        "codes": ["SH:600000"],
        "single_security_mode": True,
        "ranking_value": None,
        "max_positions": 1,
        "buy_rule": {"signals": ["indicator_one.main_force_entry"]},
        "sell_rule": {"signals": ["indicator_one.wash"]},
        "stop_loss_pct": 0.1,
        "date_mode": "custom",
        "start_date": "2026-02-02",
        "end_date": "2026-03-02",
    }
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    store.save_strategy("single-1", name="SH 600000 个股策略", request=request, summary={})
    runner = _FakeRunner(_report(tuple(100_000.0 + index * 1000 for index in range(10))))
    service = _service(tmp_path, runner)

    service.launch("single-1")

    assert runner.configs
    assert all(config.single_security_mode is True for config in runner.configs)
    assert all(config.codes == ("SH:600000",) for config in runner.configs)
    assert all(config.max_positions == 1 for config in runner.configs)
    assert all(config.ranking_value == "__single_security__" for config in runner.configs)


def test_observation_drawdown_is_measured_from_the_launch_day() -> None:
    """回撤要从观察段自己起算，不能用全历史的——那会把研究段的坑算进来。"""

    report = _report(
        (104000.0, 95000.0, 98000.0, 90000.0),
        dates=(20260302, 20260303, 20260304, 20260305),
    )

    rows = build_observations(report, launched_at=20260302)

    # peak 从上线日的 104000 起算，所以中间反弹不会重置，只有创新高才会。
    assert rows[0].drawdown == 0.0
    assert rows[1].drawdown == pytest.approx((104000 - 95000) / 104000)


def test_benchmark_is_normalised_to_the_launch_day() -> None:
    """Dividing by its launch-day value gives the same benchmark from launch."""

    report = _report(
        tuple(100000.0 for _ in range(10)),
        benchmark_totals=(1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.65, 1.8, 1.95, 2.1),
    )

    rows = build_observations(report, launched_at=_LAUNCH_DAY)

    assert rows[0].benchmark_value == pytest.approx(100000.0)  # 1.5/1.5
    assert rows[-1].benchmark_value == pytest.approx(140000.0)  # 2.1/1.5


def test_missing_benchmark_leaves_the_column_empty() -> None:
    report = _report(tuple(100000.0 for _ in range(10)))

    rows = build_observations(report, launched_at=_LAUNCH_DAY)

    assert all(row.benchmark_value is None for row in rows)


def test_segments_split_at_the_launch_day() -> None:
    report = _report(tuple(100000.0 + index * 500 for index in range(10)))

    research, observation = build_segments(report, launched_at=_LAUNCH_DAY)

    assert research is not None
    assert research.bars == 6
    assert observation is not None
    assert observation.bars == 5
    # 上线日那一行两边都算，这是刻意的接力点。
    assert research.end_date == observation.start_date == "2026-03-02"


def test_decay_refuses_to_invent_a_number() -> None:
    """分母为零就给 None。编一个 0.0 或 999 都比不给更糟——它会变成结论。"""

    report = _report(tuple(100000.0 for _ in range(10)))

    research, observation = build_segments(report, launched_at=_LAUNCH_DAY)

    decay = tracking_module._decay(research, observation)
    assert decay["annual_return"] is None
    assert decay["sharpe"] is None


def test_decay_refuses_a_negative_denominator() -> None:
    """研究段亏钱时不能除。

    真实数据里踩到过：研究段年化 -35%、观察段 -1.8%，相除得 +0.05 倍，
    页面标红显示成"只剩 5%，彻底失效"，而事实是亏损大幅收窄、变好了。
    这种会让人读出相反结论的数字，宁可不给。
    """

    totals = (
        100000.0,
        99000.0,
        98000.0,
        97000.0,
        96000.0,
        95000.0,  # 上线日：研究段全程亏
        95100.0,
        95200.0,
        95300.0,
        95400.0,  # 观察段小幅回升
    )
    research, observation = build_segments(_report(totals), launched_at=_LAUNCH_DAY)

    assert research is not None and observation is not None
    assert research.performance["annual_return"] < 0
    assert observation.performance["annual_return"] > 0

    decay = tracking_module._decay(research, observation)
    assert decay["annual_return"] is None
    assert decay["sharpe"] is None


def test_launch_pins_the_research_performance(tmp_path) -> None:
    runner = _FakeRunner(_report(tuple(100000.0 + index * 1000 for index in range(10))))
    service = _service(tmp_path, runner)
    store = service._store  # noqa: SLF001 - 测试要直接读库
    _save(store)

    snapshot = service.launch("strategy-1")

    assert snapshot.tracking_status == "active"
    assert snapshot.launched_at == _LAUNCH_DAY
    # 上线跑的是策略自己固化的 end_date，不是"最新数据"。
    assert runner.replayed[-1].isoformat() == "2026-03-02"
    assert snapshot.research is not None
    assert snapshot.research.bars == 6
    # 研究段绩效就此冻结，存在策略摘要里，之后每次重算都不会再动分母。
    stored = store.read_strategy("strategy-1")
    assert stored is not None
    assert stored["summary"]["tracking_research"]["bars"] == 6
    assert [row.trade_date for row in snapshot.observations] == [_LAUNCH_DAY]


def test_launch_starts_a_flat_tracking_account_on_the_latest_market_day(tmp_path) -> None:
    runner = _FakeRunner(_report(tuple(100000.0 + index * 1000 for index in range(10))))
    service = _service(tmp_path, runner, available_until="2026-03-06")
    _save(service._store)  # noqa: SLF001

    snapshot = service.launch("strategy-1")

    assert snapshot.launched_at == 20260306
    assert snapshot.research is not None
    assert snapshot.research.end_date == "2026-03-02"
    assert len(snapshot.observations) == 1
    launch_point = snapshot.observations[0]
    assert launch_point.trade_date == 20260306
    assert launch_point.equity == 100_000.0
    assert launch_point.cash == 100_000.0
    assert launch_point.positions_count == 0
    assert runner.configs[0].start_date == datetime.date(2026, 2, 2)
    assert runner.configs[0].end_date == datetime.date(2026, 3, 2)
    assert runner.configs[1].start_date == datetime.date(2026, 3, 6)
    assert runner.configs[1].end_date == datetime.date(2026, 3, 6)


def test_refresh_replays_only_from_the_flat_launch_account(tmp_path) -> None:
    dates = (*_DATES, 20260309, 20260310)
    runner = _FakeRunner(
        _report(tuple(100000.0 + index * 1000 for index in range(len(dates))), dates=dates)
    )
    latest = ["2026-03-06"]
    service = _service(tmp_path, runner, available_until=lambda: latest[0])
    _save(service._store)  # noqa: SLF001

    launched = service.launch("strategy-1")
    latest[0] = "2026-03-10"
    refreshed = service.refresh("strategy-1")

    assert launched.research is not None
    assert launched.research.end_date == "2026-03-02"
    assert [row.trade_date for row in refreshed.observations] == [
        20260306,
        20260309,
        20260310,
    ]
    assert refreshed.observations[0].equity == 100_000.0
    assert refreshed.observations[0].cash == 100_000.0
    assert refreshed.observations[0].positions_count == 0
    assert runner.configs[-1].start_date == datetime.date(2026, 3, 6)
    assert runner.configs[-1].end_date == datetime.date(2026, 3, 10)


def test_refresh_preserves_the_replay_mode_of_an_existing_tracking_record(tmp_path) -> None:
    runner = _FakeRunner(_report(tuple(100000.0 + index * 1000 for index in range(10))))
    service = _service(tmp_path, runner, available_until="2026-03-06")
    store = service._store  # noqa: SLF001
    _save(store)
    store.start_strategy_tracking("strategy-1", launched_at=_LAUNCH_DAY)

    snapshot = service.refresh("strategy-1")

    assert runner.configs[-1].start_date == datetime.date(2026, 2, 2)
    assert snapshot.observations[0].equity == 105_000.0
    assert snapshot.observations[0].positions_count == 3


def test_refresh_appends_every_day_since_launch(tmp_path) -> None:
    runner = _FakeRunner(_report(tuple(100000.0 + index * 1000 for index in range(10))))
    latest = ["2026-03-02"]
    service = _service(tmp_path, runner, available_until=lambda: latest[0])
    _save(service._store)  # noqa: SLF001
    service.launch("strategy-1")
    latest[0] = "2026-03-06"

    snapshot = service.refresh("strategy-1")

    assert runner.replayed[-1].isoformat() == "2026-03-06"  # 跑到注入的最新交易日
    assert [row.trade_date for row in snapshot.observations] == list(_DATES[5:])
    assert snapshot.last_tracked_date == 20260306
    # 衰减比：分子分母都算出来了。
    assert snapshot.decay["annual_return"] is not None


def test_refresh_is_idempotent(tmp_path) -> None:
    """同一天跑几次都一样。这是调度的前提——重复触发不能越跑越多。"""

    runner = _FakeRunner(_report(tuple(100000.0 + index * 1000 for index in range(10))))
    latest = ["2026-03-02"]
    service = _service(tmp_path, runner, available_until=lambda: latest[0])
    _save(service._store)  # noqa: SLF001
    service.launch("strategy-1")
    latest[0] = "2026-03-06"

    service.refresh("strategy-1")
    first = service.snapshot("strategy-1").to_dict()
    service.refresh("strategy-1")
    second = service.snapshot("strategy-1").to_dict()

    assert first == second


def test_refresh_backfills_days_the_machine_missed(tmp_path) -> None:
    """关机漏了几天，开机跑一次就补齐——因为每次都是从头跑到最新。"""

    runner = _FakeRunner(_report(tuple(100000.0 + index * 1000 for index in range(10))))
    latest = ["2026-03-02"]
    service = _service(tmp_path, runner, available_until=lambda: latest[0])
    _save(service._store)  # noqa: SLF001
    service.launch("strategy-1")  # 只写到上线日
    latest[0] = "2026-03-06"

    snapshot = service.refresh("strategy-1")

    assert len(snapshot.observations) == 5
    assert [row.trade_date for row in snapshot.observations] == list(_DATES[5:])


def test_refresh_without_new_market_days_does_not_replay(tmp_path) -> None:
    runner = _FakeRunner(_report(tuple(100000.0 + index * 1000 for index in range(10))))
    # 数据还停在上线日当天——这不是失败，也不该白跑一次全量回测。
    service = _service(tmp_path, runner, available_until="2026-03-02")
    _save(service._store)  # noqa: SLF001
    service.launch("strategy-1")
    before = len(runner.replayed)

    snapshot = service.refresh("strategy-1")

    assert len(runner.replayed) == before
    assert snapshot.last_error == ""
    assert [row.trade_date for row in snapshot.observations] == [_LAUNCH_DAY]


def test_failed_refresh_keeps_the_old_date(tmp_path) -> None:
    """最要紧的一条：跑挂了就停在旧日期，绝不静默假装成功。"""

    # 先正常推进到 03-06，再把 runner 弄坏，验证日期既不被回退也不被改新。
    healthy = _FakeRunner(_report(tuple(100000.0 + index * 1000 for index in range(10))))
    latest = ["2026-03-02"]
    service = _service(tmp_path, healthy, available_until=lambda: latest[0])
    _save(service._store)  # noqa: SLF001
    service.launch("strategy-1")
    latest[0] = "2026-03-06"
    service.refresh("strategy-1")
    assert service.snapshot("strategy-1").last_tracked_date == 20260306

    broken = _FakeRunner(_report((), dates=()))
    broken.run = _explode  # type: ignore[method-assign]
    service = _service(tmp_path, broken, available_until="2026-03-10")

    snapshot = service.refresh("strategy-1")

    assert snapshot.last_tracked_date == 20260306  # 停在旧日期
    assert "行情缺失" in snapshot.last_error


def test_failed_write_keeps_the_date_and_records_the_error(tmp_path) -> None:
    """写盘失败也必须留下痕迹：只上抛的话库里还是「正常、只是没更新」。"""

    runner = _FakeRunner(_report(tuple(100000.0 + index * 1000 for index in range(10))))
    latest = ["2026-03-02"]
    service = _service(tmp_path, runner, available_until=lambda: latest[0])
    store = service._store  # noqa: SLF001
    _save(store)
    service.launch("strategy-1")
    latest[0] = "2026-03-06"

    def explode(*_args: Any, **_kwargs: Any) -> int:
        raise OSError("磁盘写满了")

    store.save_tracking_points = explode  # type: ignore[method-assign]

    with pytest.raises(OSError, match="磁盘写满了"):
        service.refresh("strategy-1")

    record = store.read_strategy("strategy-1")
    assert record is not None
    assert "磁盘写满了" in str(record["last_error"])
    # 日期没有被推进——停在旧值，下次成功重跑会把整段补齐。
    assert record["last_tracked_date"] == _LAUNCH_DAY


def test_short_observations_are_flagged(tmp_path) -> None:
    """刚上线几天就谈"衰减"是噪声，前端必须标注，不能当结论用。"""

    runner = _FakeRunner(_report(tuple(100000.0 + index * 1000 for index in range(10))))
    service = _service(tmp_path, runner)
    _save(service._store)  # noqa: SLF001

    snapshot = service.launch("strategy-1")

    assert snapshot.observation_is_short is True
    assert len(snapshot.observations) - 1 < MIN_OBSERVATION_BARS
    # 二十个收益率都凑不出来，偏度峰度估不准——此时 MinTRL 必须不给，
    # 给一个数会让人以为它算准了。
    assert snapshot.min_track_record_length is None


def test_min_track_record_length_is_reported_for_long_windows(tmp_path) -> None:
    """观察段够长时给 MinTRL：它通常远大于观察段，那是进度不是错误。"""

    dates = _business_dates(60)
    launch_day = dates[9]
    # 交替 +1% / −0.9%：确定性序列，日夏普约 0.053。用它是因为随机序列下
    # 样本均值本身不稳——而那正是 MinTRL 存在的理由，会让断言跟着抖。
    step = np.array([0.01 if index % 2 == 0 else -0.009 for index in range(len(dates))])
    totals = tuple(float(value) for value in 100000.0 * np.cumprod(1.0 + step))
    runner = _FakeRunner(_report(totals, dates=dates))
    latest = [f"{str(launch_day)[:4]}-{str(launch_day)[4:6]}-{str(launch_day)[6:]}"]
    service = _service(
        tmp_path,
        runner,
        available_until=lambda: latest[0],
    )
    service._store.save_strategy(  # noqa: SLF001
        "strategy-1",
        name="上线策略",
        request={
            **_REQUEST,
            "start_date": "2025-12-01",
            "end_date": f"{str(launch_day)[:4]}-{str(launch_day)[4:6]}-{str(launch_day)[6:]}",
        },
        summary={},
    )
    # launch 只种下上线日一行，观察段要靠 refresh 补齐——这也是真实用法。
    service.launch("strategy-1")
    latest[0] = f"{str(dates[-1])[:4]}-{str(dates[-1])[4:6]}-{str(dates[-1])[6:]}"
    snapshot = service.refresh("strategy-1")

    assert snapshot.observation_is_short is False
    assert snapshot.min_track_record_length is not None
    # 要的远比已有的多——这正是它要传达的信息：别急。
    assert snapshot.min_track_record_length > len(snapshot.observations)


def test_refresh_requires_an_online_strategy(tmp_path) -> None:
    runner = _FakeRunner(_report(tuple(100000.0 for _ in range(10))))
    service = _service(tmp_path, runner)
    _save(service._store)  # noqa: SLF001

    with pytest.raises(ValueError, match="尚未上线"):
        service.refresh("strategy-1")


def test_stop_keeps_what_was_observed(tmp_path) -> None:
    runner = _FakeRunner(_report(tuple(100000.0 + index * 1000 for index in range(10))))
    latest = ["2026-03-02"]
    service = _service(tmp_path, runner, available_until=lambda: latest[0])
    _save(service._store)  # noqa: SLF001
    service.launch("strategy-1")
    latest[0] = "2026-03-06"
    service.refresh("strategy-1")

    snapshot = service.stop("strategy-1")

    assert snapshot.tracking_status == "off"
    assert len(snapshot.observations) == 5
    # 研究段绩效还在——它是历史事实，停不停都一样。
    assert snapshot.research is not None


def test_stop_keeps_the_last_successful_account_and_candidate_snapshots(tmp_path) -> None:
    current_state = {
        "as_of": "2026-03-06",
        "available": True,
        "equity": 100_500.0,
        "cash": 40_500.0,
        "position_value": 60_000.0,
        "positions_count": 1,
        "holdings": [{"market": "SH", "code": "600000", "size": 100.0}],
    }
    candidates = {
        "as_of": "2026-03-06",
        "ranking_value": "indicator_one.wash",
        "total": 1,
        "rows": [{"rank": 1, "code": "600001", "score": 88.0}],
    }
    runner = _FakeRunner(
        _report(
            tuple(100000.0 + index * 1000 for index in range(10)),
            current_state=current_state,
            today_candidates=candidates,
        )
    )
    service = _service(tmp_path, runner, available_until="2026-03-06")
    _save(service._store)  # noqa: SLF001

    launched = service.launch("strategy-1")
    stopped = service.stop("strategy-1")

    assert launched.current_state == current_state
    assert stopped.tracking_status == "off"
    assert stopped.current_state == current_state
    assert stopped.today_candidates == candidates


def _traded_report() -> dict[str, Any]:
    return _report(
        tuple(100000.0 + index * 1000 for index in range(10)),
        trades=(
            {
                "date": "2026-02-03",
                "market": "SH",
                "code": "600000",
                "name": "甲",
                "direction": "BUY",
                "size": 100.0,
                "price": 10.0,
                "pnl": 0.0,
                "reason": "买入条件",
                "signal_date": "2026-02-02",
                "commission": 1.0,
                "stamp_tax": 0.0,
                "slippage": 2.0,
                "cost_basis": 1003.0,
            },
            {
                "date": "2026-03-04",
                "market": "SZ",
                "code": "000001",
                "name": "乙",
                "direction": "SELL",
                "size": 200.0,
                "price": 12.0,
                "pnl": 150.0,
                "reason": "卖出条件",
                "signal_date": "2026-03-03",
                "commission": 2.4,
                "stamp_tax": 2.4,
                "slippage": 1.0,
                "cost_basis": 2200.0,
            },
            {
                "date": "2026-03-05",
                "market": "SH",
                "code": "600002",
                "name": "丙",
                "direction": "BUY",
                "size": 300.0,
                "price": 8.0,
                "pnl": 0.0,
                "reason": "买入条件",
            },
        ),
        next_plan={
            "as_of": "2026-03-06",
            "cash": 5000.0,
            "rebalance_due": True,
            "holdings": [],
            "sells": [],
            "buys": [{"code": "600000", "estimated_size": 400.0}],
            "notes": ["买入股数是按今日收盘价与可用现金估算的"],
        },
    )


def test_snapshot_splits_trades_around_the_launch_day(tmp_path) -> None:
    """上线前 / 后的成交必须分开：后者才是"上线之后真的干了什么"。"""

    runner = _FakeRunner(_traded_report())
    latest = ["2026-03-02"]
    service = _service(tmp_path, runner, available_until=lambda: latest[0])
    _save(service._store)  # noqa: SLF001
    service.launch("strategy-1")
    latest[0] = "2026-03-06"

    snapshot = service.refresh("strategy-1")
    trades = snapshot.trades

    # 判据和净值曲线一致：成交日 >= 上线日算上线后。
    assert [row["code"] for row in trades["before"]["rows"]] == ["600000"]
    # 倒序：最近的一笔在最前面。
    assert [row["code"] for row in trades["after"]["rows"]] == ["600002", "000001"]
    assert trades["after"]["total"] == 2
    assert trades["after"]["truncated"] is False
    assert trades["after"]["rows"][0]["pnl"] == 0.0
    assert trades["after"]["rows"][1]["pnl"] == 150.0
    assert trades["after"]["rows"][1]["signal_date"] == 20260303
    assert trades["after"]["rows"][1]["commission"] == 2.4
    assert trades["after"]["rows"][1]["stamp_tax"] == 2.4
    assert trades["after"]["rows"][1]["slippage"] == 1.0
    assert trades["after"]["rows"][1]["cost_basis"] == 2200.0


def test_snapshot_reports_the_next_session_plan(tmp_path) -> None:
    runner = _FakeRunner(_traded_report())
    latest = ["2026-03-02"]
    service = _service(tmp_path, runner, available_until=lambda: latest[0])
    _save(service._store)  # noqa: SLF001
    service.launch("strategy-1")
    latest[0] = "2026-03-06"
    service.refresh("strategy-1")

    plan = service.snapshot("strategy-1").next_plan

    assert plan is not None
    assert plan["as_of"] == "2026-03-06"
    assert plan["buys"][0]["estimated_size"] == 400.0


def test_an_offline_strategy_has_no_pending_plan(tmp_path) -> None:
    """过期的单子挂在页面上比没有更糟——下线就必须清掉。"""

    runner = _FakeRunner(_traded_report())
    latest = ["2026-03-02"]
    service = _service(tmp_path, runner, available_until=lambda: latest[0])
    _save(service._store)  # noqa: SLF001
    service.launch("strategy-1")
    latest[0] = "2026-03-06"
    service.refresh("strategy-1")

    snapshot = service.stop("strategy-1")

    assert snapshot.next_plan is None
    # 成交明细是已经发生的事实，下线不该抹掉。
    assert snapshot.trades["after"]["total"] == 2


def test_trade_sections_report_when_they_are_cut_off(tmp_path, monkeypatch) -> None:
    """一年几百笔成交是常态。截断了必须说出来，不能让人以为看到的就是全部。"""

    monkeypatch.setattr(tracking_module, "MAX_TRADES_PER_SEGMENT", 2)
    many = tuple(
        {
            "date": f"2026-03-0{day}",
            "market": "SH",
            "code": f"60000{day}",
            "name": "丁",
            "direction": "BUY",
            "size": 100.0,
            "price": 10.0,
            "pnl": 0.0,
            "reason": "买入条件",
        }
        for day in (3, 4, 5)
    )
    runner = _FakeRunner(
        _report(tuple(100000.0 + index * 1000 for index in range(10)), trades=many)
    )
    latest = ["2026-03-02"]
    service = _service(tmp_path, runner, available_until=lambda: latest[0])
    _save(service._store)  # noqa: SLF001
    service.launch("strategy-1")
    latest[0] = "2026-03-06"

    section = service.refresh("strategy-1").trades["after"]

    assert len(section["rows"]) == 2
    assert section["total"] == 3
    assert section["truncated"] is True
    assert section["rows"][0]["code"] == "600005"


class _OnlineStore:
    """批处理只问一件事：现在还有哪些策略在线。"""

    def __init__(self, online: set[str]) -> None:
        self.online = online

    def list_tracked_strategies(self) -> list[dict[str, object]]:
        return [{"strategy_id": strategy_id} for strategy_id in sorted(self.online)]


class _RecordingService:
    def __init__(
        self,
        snapshot: object,
        on_refresh: Callable[[str], None] | None = None,
    ) -> None:
        self.refreshed: list[str] = []
        self._snapshot = snapshot
        self._on_refresh = on_refresh

    def refresh(self, strategy_id: str) -> object:
        self.refreshed.append(strategy_id)
        if self._on_refresh is not None:
            self._on_refresh(strategy_id)
        return self._snapshot


def test_batch_skips_a_strategy_stopped_mid_run() -> None:
    """跑的过程中被停掉的策略不能继续被推进日期。

    `refresh` 只拒绝「从未上线」的策略，对已停止的照跑不误；批处理自己不确认
    在线状态的话，停掉之后它还会长出新数据——和 `stop()` 的「已观测即冻结」
    相矛盾。
    """

    store = _OnlineStore({"strategy-1", "strategy-2"})
    scheduler = StrategyTrackingScheduler(
        store=store,  # type: ignore[arg-type]
        runner=object(),  # type: ignore[arg-type]
        runner_service=object(),  # type: ignore[arg-type]
    )
    snapshot = SimpleNamespace(last_error="", last_tracked_date=20260306, observations=())
    # 处理第一个策略的期间，用户停掉了第二个。
    service = _RecordingService(snapshot, lambda _sid: store.online.discard("strategy-2"))
    scheduler._service = service  # type: ignore[assignment] # noqa: SLF001

    report = scheduler._run_batch(lambda done, total: None)

    assert service.refreshed == ["strategy-1"]
    assert report["total"] == 1
