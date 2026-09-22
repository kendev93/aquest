"""策略追踪：重算、幂等、失败不动日期。

这里测的是三条最容易被写错、写错了又最难发现的口径：

1. 观察段从上线日那一行开始（两段共用接力点）。
2. 同一天重算几次结果都一样；漏了几天能一次补齐。
3. **失败只停���旧日期**——静默改成"今天"会让用户以为数据已经更新了，
   而实际那几天什么都没算。
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.portfolio_backtest import tracking as tracking_module
from selector_app.portfolio_backtest.auto_tracking import StrategyTrackingScheduler
from selector_app.portfolio_backtest.tracking import (
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


def _report(
    totals: tuple[float, ...],
    *,
    dates: tuple[int, ...] = _DATES,
    benchmark_totals: tuple[float, ...] | None = None,
    trades: tuple[dict[str, Any], ...] = (),
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
    }


class _FakeRunner:
    """Stand-in for the portfolio service: records every replay it was asked for."""

    def __init__(self, report: dict[str, Any]) -> None:
        self.report = report
        self.replayed: list[Any] = []

    def run(self, config: Any, progress_callback: Any = None) -> dict[str, Any]:
        del progress_callback
        self.replayed.append(config.end_date)
        return self.report


def _explode(config: Any, progress_callback: Any = None) -> dict[str, Any]:
    del config, progress_callback
    raise RuntimeError("行情缺失")


def _service(
    tmp_path: Any,
    runner: _FakeRunner,
    *,
    available_until: str | None = "2026-03-06",
) -> StrategyTrackingService:
    store = DuckDbMarketDataStore(tmp_path / "market.duckdb")
    return StrategyTrackingService(
        store=store,
        runner=runner,
        available_until=lambda: available_until,
    )


def _save(store: DuckDbMarketDataStore) -> None:
    store.save_strategy("strategy-1", name="上线策略", request=dict(_REQUEST), summary={})


def test_observations_start_on_the_launch_day() -> None:
    """上线日属于两段：研究段的终点、观察段的起点。留着它曲线才接得上。"""

    report = _report(tuple(100000.0 + index * 1000 for index in range(10)))

    rows = build_observations(report, launched_at=_LAUNCH_DAY)

    assert [row.trade_date for row in rows] == list(_DATES[5:])
    assert rows[0].equity == 105000.0


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
    """报告里的基准从研究段起点买入持有；除以上线日的值就等于"那天起买入"。"""

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


def test_refresh_appends_every_day_since_launch(tmp_path) -> None:
    runner = _FakeRunner(_report(tuple(100000.0 + index * 1000 for index in range(10))))
    service = _service(tmp_path, runner)
    _save(service._store)  # noqa: SLF001
    service.launch("strategy-1")

    snapshot = service.refresh("strategy-1")

    assert runner.replayed[-1].isoformat() == "2026-03-06"  # 跑到注入的最新交易日
    assert [row.trade_date for row in snapshot.observations] == list(_DATES[5:])
    assert snapshot.last_tracked_date == 20260306
    # 衰减比：分子分母都算出来了。
    assert snapshot.decay["annual_return"] is not None


def test_refresh_is_idempotent(tmp_path) -> None:
    """同一天跑几次都一样。这是调度的前提——重复触发不能越跑越多。"""

    runner = _FakeRunner(_report(tuple(100000.0 + index * 1000 for index in range(10))))
    service = _service(tmp_path, runner)
    _save(service._store)  # noqa: SLF001
    service.launch("strategy-1")

    service.refresh("strategy-1")
    first = service.snapshot("strategy-1").to_dict()
    service.refresh("strategy-1")
    second = service.snapshot("strategy-1").to_dict()

    assert first == second


def test_refresh_backfills_days_the_machine_missed(tmp_path) -> None:
    """关机漏了几天，开机跑一次就补齐——因为每次都是从头跑到最新。"""

    runner = _FakeRunner(_report(tuple(100000.0 + index * 1000 for index in range(10))))
    service = _service(tmp_path, runner)
    _save(service._store)  # noqa: SLF001
    service.launch("strategy-1")  # 只写到上线日

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
    service = _service(tmp_path, healthy)
    _save(service._store)  # noqa: SLF001
    service.launch("strategy-1")
    service.refresh("strategy-1")
    assert service.snapshot("strategy-1").last_tracked_date == 20260306

    broken = _FakeRunner(_report((), dates=()))
    broken.run = _explode  # type: ignore[method-assign]
    service = _service(tmp_path, broken)

    snapshot = service.refresh("strategy-1")

    assert snapshot.last_tracked_date == 20260306  # 停在旧日期
    assert "行情缺失" in snapshot.last_error


def test_failed_write_keeps_the_date_and_records_the_error(tmp_path) -> None:
    """写盘失败也必须留下痕迹：只上抛的话库里还是「正常、只是没更新」。"""

    runner = _FakeRunner(_report(tuple(100000.0 + index * 1000 for index in range(10))))
    service = _service(tmp_path, runner)
    store = service._store  # noqa: SLF001
    _save(store)
    service.launch("strategy-1")

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


def test_refresh_requires_an_online_strategy(tmp_path) -> None:
    runner = _FakeRunner(_report(tuple(100000.0 for _ in range(10))))
    service = _service(tmp_path, runner)
    _save(service._store)  # noqa: SLF001

    with pytest.raises(ValueError, match="尚未上线"):
        service.refresh("strategy-1")


def test_stop_keeps_what_was_observed(tmp_path) -> None:
    runner = _FakeRunner(_report(tuple(100000.0 + index * 1000 for index in range(10))))
    service = _service(tmp_path, runner)
    _save(service._store)  # noqa: SLF001
    service.launch("strategy-1")
    service.refresh("strategy-1")

    snapshot = service.stop("strategy-1")

    assert snapshot.tracking_status == "off"
    assert len(snapshot.observations) == 5
    # 研究段绩效还在——它是历史事实，停不停都一样。
    assert snapshot.research is not None


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
