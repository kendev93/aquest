"""策略追踪：把「上线前的研究段」和「上线后的观察段」摆在一起。

我们所有其它能力回答的都是"历史上这套东西好不好"，只有它回答**"从今天起
还灵不灵"**。置换门槛、BH-FDR、因果无前视适配全是为了提高"结论在未来也成立"
的概率，但如果没有人去记录实际发生了什么，那些功夫就永远只是自我声明。

核心看点只有两个，都是可证伪的：

1. **衰减比** = 观察段指标 / 研究段指标。
2. **观察段相对基准** = 观察段策略收益 − 同期标的池等权买入持有。

关于重算方式：**这里是全量重跑，不是增量。** 引擎其实带了
``SimulationCheckpoint`` 续跑，但它的 ``data_fingerprint`` 是行情帧的哈希，
每天收盘后行情都会变，指纹必然对不上（``docs/strategy-tracking-plan.md``
§2.1 有完整论证）。全量重跑换来的是——观察段与研究段出自**同一次模拟**，
不存在两段拼不上的可能，这正是"衰减比可复核"所要求的。
"""

from __future__ import annotations

import logging
from bisect import bisect_left
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

import numpy as np
import pandas as pd

from selector_app.backtest.performance import PerformanceAnalyzer
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.portfolio_backtest.models import (
    PortfolioBacktestConfig,
    PortfolioBacktestReport,
)
from selector_app.portfolio_backtest.service import PortfolioBacktestProgressCallback
from selector_app.web.schemas import PortfolioBacktestRequest

logger = logging.getLogger(__name__)

#: 观察段不到这么多交易日，衰减比基本是噪声——页面要标注而不是当成结论。
MIN_OBSERVATION_BARS = 20

_PERFORMANCE_KEYS = (
    "total_return",
    "annual_return",
    "max_drawdown",
    "sharpe",
    "volatility",
    "calmar",
    "start_cash",
    "end_value",
)
#: 算衰减比的指标。回撤是"越小越好"，比值会反过来误导（0.5 倍回撤看起来像
#: 变差其实是变好），所以不进这个列表，页面上单独给绝对值。
_DECAY_KEYS = ("annual_return", "sharpe")

_EMPTY_TRADES = pd.DataFrame(
    columns=[
        "datetime",
        "direction",
        "size",
        "price",
        "commission",
        "slippage",
        "pnl",
        "cost_basis",
        "rejected",
    ]
)


class TrackingRunner(Protocol):
    """The slice of the portfolio service tracking needs."""

    def run(
        self,
        config: PortfolioBacktestConfig,
        progress_callback: PortfolioBacktestProgressCallback | None = None,
    ) -> PortfolioBacktestReport | dict[str, object]: ...


@dataclass(frozen=True)
class TrackedSegment:
    """一段窗口（研究段或观察段）的绩效摘要。"""

    start_date: str
    end_date: str
    bars: int
    performance: Mapping[str, float | None]

    def to_dict(self) -> dict[str, object]:
        return {
            "start_date": self.start_date,
            "end_date": self.end_date,
            "bars": self.bars,
            "performance": dict(self.performance),
        }


@dataclass(frozen=True)
class TrackedObservation:
    """One trading day of the observation window."""

    trade_date: int
    equity: float
    cash: float
    positions_count: int
    benchmark_value: float | None
    drawdown: float | None

    def to_dict(self) -> dict[str, object]:
        return {
            "trade_date": self.trade_date,
            "equity": self.equity,
            "cash": self.cash,
            "positions_count": self.positions_count,
            "benchmark_value": self.benchmark_value,
            "drawdown": self.drawdown,
        }


@dataclass(frozen=True)
class TrackingSnapshot:
    """Everything one strategy's tracking page needs."""

    strategy_id: str
    tracking_status: str
    launched_at: int | None
    last_tracked_date: int | None
    last_error: str
    observations: tuple[TrackedObservation, ...]
    research: TrackedSegment | None
    observation: TrackedSegment | None
    decay: Mapping[str, float | None]
    benchmark_performance: Mapping[str, float | None] | None
    excess_annual_return: float | None
    #: 观察段够不够长到可以谈"衰减"。前端必须据此标注，不能装作是结论。
    observation_is_short: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "strategy_id": self.strategy_id,
            "tracking_status": self.tracking_status,
            "launched_at": self.launched_at,
            "last_tracked_date": self.last_tracked_date,
            "last_error": self.last_error,
            "observations": [row.to_dict() for row in self.observations],
            "research": self.research.to_dict() if self.research else None,
            "observation": self.observation.to_dict() if self.observation else None,
            "decay": dict(self.decay),
            "benchmark_performance": (
                dict(self.benchmark_performance) if self.benchmark_performance else None
            ),
            "excess_annual_return": self.excess_annual_return,
            "observation_is_short": self.observation_is_short,
        }


def _date_key(value: object) -> int:
    """``"2026-09-01"`` or ``20260901`` -> ``20260901``."""

    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        return int(text)
    return int(text.replace("-", ""))


def _date_text(trade_date: int) -> str:
    text = str(int(trade_date))
    return f"{text[:4]}-{text[4:6]}-{text[6:]}"


def _finite_or_none(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if np.isfinite(number) else None


def _number(value: object) -> float:
    """Coerce one decoded row field to a finite float, defaulting to zero.

    这些值来自我们自己刚写进库的行，理论上都干净。但一个 NaN 混进曲线就会一路
    画到页面上，而持久化的东西事后很难挽回，所以还是逐个兜住。
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    number = float(value)
    return number if np.isfinite(number) else 0.0


def _equity_rows(
    report: Mapping[str, object],
) -> tuple[tuple[int, float, float, int], ...]:
    """``(date, total, cash, positions_count)`` per row, oldest first."""

    curve = report.get("equity_curve")
    if not isinstance(curve, (list, tuple)):
        return ()
    rows: list[tuple[int, float, float, int]] = []
    for row in curve:
        if not isinstance(row, Mapping):
            continue
        try:
            trade_date = _date_key(row.get("date"))
        except (TypeError, ValueError):
            continue
        rows.append(
            (
                trade_date,
                _finite_or_none(row.get("total")) or 0.0,
                _finite_or_none(row.get("cash")) or 0.0,
                int(_finite_or_none(row.get("positions_count")) or 0),
            )
        )
    return tuple(rows)


def _benchmark_ratio(report: Mapping[str, object], launched_at: int) -> dict[int, float]:
    """基准按上线日归一化。

    报告里的基准从研究段起点买入持有；持有期间没有任何交易，所以把它在上线
    日之后的这一段**除以上线日的值**，就等于"从上线日起买入持有"。
    """

    benchmark = report.get("benchmark")
    if not isinstance(benchmark, Mapping):
        return {}
    dates_raw = benchmark.get("dates")
    totals_raw = benchmark.get("totals")
    if not isinstance(dates_raw, (list, tuple)) or not isinstance(totals_raw, (list, tuple)):
        return {}
    try:
        dates = [int(value) for value in dates_raw]
        totals = [float(value) for value in totals_raw]
    except (TypeError, ValueError):
        return {}
    if len(dates) != len(totals) or not dates:
        return {}
    index = bisect_left(dates, launched_at)
    if index >= len(dates):
        return {}
    base = totals[index]
    if not np.isfinite(base) or base <= 0:
        return {}
    return {dates[i]: totals[i] / base for i in range(index, len(dates))}


def _trades_frame(
    report: Mapping[str, object],
    *,
    since: int,
) -> pd.DataFrame:
    """Trades inside one window, shaped for ``PerformanceAnalyzer``."""

    trades = report.get("trades")
    if not isinstance(trades, (list, tuple)):
        return _EMPTY_TRADES
    rows: list[dict[str, object]] = []
    for trade in trades:
        if not isinstance(trade, Mapping):
            continue
        try:
            trade_date = _date_key(trade.get("date"))
        except (TypeError, ValueError):
            continue
        if trade_date < since:
            continue
        rows.append(
            {
                "datetime": trade_date,
                "direction": str(trade.get("direction") or ""),
                "size": _finite_or_none(trade.get("size")) or 0.0,
                "price": _finite_or_none(trade.get("price")) or 0.0,
                "commission": (
                    (_finite_or_none(trade.get("commission")) or 0.0)
                    + (_finite_or_none(trade.get("stamp_tax")) or 0.0)
                ),
                "slippage": _finite_or_none(trade.get("slippage")) or 0.0,
                "pnl": _finite_or_none(trade.get("pnl")) or 0.0,
                "cost_basis": _finite_or_none(trade.get("cost_basis")) or 0.0,
                "rejected": bool(trade.get("rejected", False)),
            }
        )
    if not rows:
        return _EMPTY_TRADES
    return pd.DataFrame(rows)


def _performance(totals: Sequence[float], trades: pd.DataFrame) -> Mapping[str, float | None]:
    if len(totals) < 2:
        return {key: None for key in _PERFORMANCE_KEYS}
    frame = pd.DataFrame({"total": np.asarray(totals, dtype=float)})
    computed = PerformanceAnalyzer(frame, trades).compute()
    return {key: _finite_or_none(computed.get(key)) for key in _PERFORMANCE_KEYS}


def _decay(
    research: TrackedSegment | None,
    observation: TrackedSegment | None,
) -> Mapping[str, float | None]:
    """观察段 / 研究段。只在这个比值真能说明问题时才给数。

    分母为 0 不用说了。分母为**负**同样不能除：研究段年化 -35%、观察段
    -1.8%，相除得到 +0.05 倍，页面上会读成"只剩 5%，彻底失效"，而事实是
    亏损大幅收窄、变好了。一个会让人得出相反结论的数字比不给更糟，所以
    分母 ≤ 0 时一律给 None（前端显示"—"，两段数值并列摆着，自己看）。
    """

    ratios: dict[str, float | None] = {}
    for key in _DECAY_KEYS:
        base = research.performance.get(key) if research else None
        current = observation.performance.get(key) if observation else None
        if base is None or current is None or base <= 1e-12:
            ratios[key] = None
        else:
            ratios[key] = float(current) / float(base)
    return ratios


def build_observations(
    report: Mapping[str, object],
    *,
    launched_at: int,
) -> tuple[TrackedObservation, ...]:
    """Cut one full report down to its post-launch rows.

    上线日那一行**属于两段**：它是研究段的终点，也是观察段的起点。留着它曲线
    才接得上，衰减比也才有统一的零点。
    """

    rows = [row for row in _equity_rows(report) if row[0] >= launched_at]
    if not rows:
        return ()
    benchmark = _benchmark_ratio(report, launched_at)
    base_equity = rows[0][1]
    observations: list[TrackedObservation] = []
    peak = rows[0][1]
    for trade_date, total, cash, count in rows:
        peak = max(peak, total)
        ratio = benchmark.get(trade_date)
        benchmark_value = None if ratio is None else base_equity * ratio
        drawdown = (peak - total) / peak if peak > 0 else 0.0
        observations.append(
            TrackedObservation(
                trade_date=trade_date,
                equity=total,
                cash=cash,
                positions_count=count,
                benchmark_value=benchmark_value,
                drawdown=drawdown,
            )
        )
    return tuple(observations)


def build_segments(
    report: Mapping[str, object],
    *,
    launched_at: int,
) -> tuple[TrackedSegment | None, TrackedSegment | None]:
    """Split one report into the pre-launch and post-launch windows."""

    before = [row for row in _equity_rows(report) if row[0] <= launched_at]
    after = [row for row in _equity_rows(report) if row[0] >= launched_at]
    research = _segment(before, report, since=min(before[0][0], launched_at)) if before else None
    observation = _segment(after, report, since=launched_at) if after else None
    return research, observation


def _segment(
    rows: list[tuple[int, float, float, int]],
    report: Mapping[str, object],
    *,
    since: int,
) -> TrackedSegment | None:
    if len(rows) < 2:
        return None
    trades = _trades_frame(report, since=since)
    return TrackedSegment(
        start_date=_date_text(rows[0][0]),
        end_date=_date_text(rows[-1][0]),
        bars=len(rows),
        performance=_performance([row[1] for row in rows], trades),
    )


def _report_payload(result: PortfolioBacktestReport | Mapping[str, object]) -> Mapping[str, object]:
    if isinstance(result, PortfolioBacktestReport):
        return result.to_dict()
    return dict(result)


class StrategyTrackingService:
    """Put strategies online and replay them against newer market data.

    两个入口对应同一个重跑：``launch`` 用策略固化的 end_date 跑（得到研究段），
    ``refresh`` 用库里最新交易日跑（同一份配置、同一段历史，只是多跑了几天）。
    因为都是全量跑，观察段和研究段永远出自**同一次模拟**，不会出现"两端用不同
    口径拼起来"的情况。
    """

    def __init__(
        self,
        *,
        store: DuckDbMarketDataStore,
        runner: TrackingRunner,
        available_until: Callable[[], str | None] | None = None,
    ) -> None:
        self._store = store
        self._runner = runner
        # 默认问行情库，但允许注入：调度器要能先确认"今天的数据到了没"，
        # 测试也不该被迫造一个有几千万行日线的库。
        self._available_until = available_until or (lambda: self._store.status().data_end)

    def launch(self, strategy_id: str) -> TrackingSnapshot:
        """Go online: replay the frozen window once and pin it as the yardstick."""

        record = self._read(strategy_id)
        launched_at = _date_key(self._request(record)["end_date"])
        report = self._run(self._request(record), _date_text(launched_at))
        research, _ = build_segments(report, launched_at=launched_at)
        observations = build_observations(report, launched_at=launched_at)
        # 研究段的绩效就此钉死。后面任何一次重算都不会动它——动了就不是样本外了。
        summary = self._research_summary(record, research)
        started = self._store.start_strategy_tracking(
            strategy_id,
            launched_at=launched_at,
            seed_points=[row.to_dict() for row in observations[:1]],  # 上线日：接力点，两段共用
        )
        if started is not None and summary is not None:
            self._store.update_strategy_summary(strategy_id, summary)
        return self.snapshot(strategy_id)

    def stop(self, strategy_id: str) -> TrackingSnapshot:
        """Go offline. Observations already made are kept."""

        self._store.stop_strategy_tracking(strategy_id)
        return self.snapshot(strategy_id)

    def refresh(self, strategy_id: str) -> TrackingSnapshot:
        """Replay up to the newest market day and append whatever is missing.

        幂等：同一天跑几次结果都一样。可补跑：机器关机漏了几天，开机后跑一次
        就把整段补齐——因为每次都是从头跑到最新。
        """

        record = self._read(strategy_id)
        launched_at = _read_launched_at(record)
        if launched_at is None:
            raise ValueError("策略尚未上线")
        latest_raw = self._available_until()
        if not latest_raw:
            return self._failed(strategy_id, "行情库里没有可用于追踪的行情")
        latest = _date_key(latest_raw)
        if launched_at >= latest:
            # 上线日之后还没有新的交易日。这不是失败，也不需要重跑。
            return self.snapshot(strategy_id)
        try:
            report = self._run(self._request(record), _date_text(latest))
        except Exception as exc:  # noqa: BLE001 - 追踪失败不能把整个后台带走
            logger.exception("策略追踪重算失败: %s", strategy_id)
            self._store.finish_strategy_tracking(strategy_id, error=str(exc)[:500])
            return self.snapshot(strategy_id)

        observations = build_observations(report, launched_at=launched_at)
        research, observation = build_segments(report, launched_at=launched_at)
        if not observations:
            return self._failed(strategy_id, "重算结果里没有上线日之后的交易日")
        try:
            self._store.save_tracking_points(strategy_id, [row.to_dict() for row in observations])
            summary = self._research_summary(record, research)
            if summary is not None and research is not None:
                self._store.update_strategy_summary(strategy_id, summary)
        except Exception as exc:  # noqa: BLE001 - 写失败必须在库里留痕再上抛
            # 只上抛的话库里 `last_error` 仍是空的、`last_tracked_date` 停在旧值，
            # 界面上是「一切正常、只是没更新」——日志里的原因没人会去看。
            logger.exception("策略追踪写入失败: %s", strategy_id)
            self._store.finish_strategy_tracking(strategy_id, error=str(exc)[:500])
            raise
        self._store.finish_strategy_tracking(
            strategy_id, last_tracked_date=observations[-1].trade_date
        )
        return self.snapshot(strategy_id)

    def snapshot(self, strategy_id: str) -> TrackingSnapshot:
        """Read the last computed state without spending any CPU."""

        record = self._read(strategy_id)
        launched_at = _read_launched_at(record)
        rows = self._store.list_tracking_points(strategy_id) if launched_at else []
        observations = tuple(
            TrackedObservation(
                trade_date=int(_number(row["trade_date"])),
                equity=_number(row["equity"]),
                cash=_number(row["cash"]),
                positions_count=int(_number(row["positions_count"])),
                benchmark_value=(
                    None if row["benchmark_value"] is None else _number(row["benchmark_value"])
                ),
                drawdown=None if row["drawdown"] is None else _number(row["drawdown"]),
            )
            for row in rows
        )
        research = _research_from_summary(record.get("summary"))
        observation = self._observation_segment(observations)
        benchmark = self._benchmark_performance(observations)
        return TrackingSnapshot(
            strategy_id=str(strategy_id),
            tracking_status=str(record.get("tracking_status") or "off"),
            launched_at=launched_at,
            last_tracked_date=_optional_int(record.get("last_tracked_date")),
            last_error=str(record.get("last_error") or ""),
            observations=observations,
            research=research,
            observation=observation,
            decay=_decay(research, observation),
            benchmark_performance=benchmark,
            excess_annual_return=_excess(observation, benchmark),
            # 上线日那一行是接力点，不是观察日，所以阈值要 +1。
            observation_is_short=len(observations) < MIN_OBSERVATION_BARS + 1,
        )

    # ------------------------------------------------------------------

    def _read(self, strategy_id: str) -> Mapping[str, object]:
        record = self._store.read_strategy(strategy_id)
        if record is None:
            raise ValueError("策略不存在")
        request = record.get("request")
        if not isinstance(request, Mapping) or not request.get("end_date"):
            raise ValueError("策略缺少可追踪的回测配置")
        return record

    @staticmethod
    def _request(record: Mapping[str, object]) -> Mapping[str, object]:
        request = record.get("request")
        return request if isinstance(request, Mapping) else {}

    def _run(self, request: Mapping[str, object], end_date: str) -> Mapping[str, object]:
        payload = dict(request)
        payload["date_mode"] = "custom"
        payload["end_date"] = end_date
        config = PortfolioBacktestRequest.model_validate(payload).to_config()
        return _report_payload(self._runner.run(config))

    def _research_summary(
        self,
        record: Mapping[str, object],
        research: TrackedSegment | None,
    ) -> Mapping[str, object] | None:
        """Research performance is stored once and never recomputed again."""

        if research is None:
            return None
        existing = record.get("summary")
        summary = dict(existing) if isinstance(existing, Mapping) else {}
        summary["tracking_research"] = research.to_dict()
        return summary

    def _failed(self, strategy_id: str, message: str) -> TrackingSnapshot:
        self._store.finish_strategy_tracking(strategy_id, error=message)
        return self.snapshot(strategy_id)

    @staticmethod
    def _observation_segment(
        observations: Sequence[TrackedObservation],
    ) -> TrackedSegment | None:
        if len(observations) < 2:
            return None
        return TrackedSegment(
            start_date=_date_text(observations[0].trade_date),
            end_date=_date_text(observations[-1].trade_date),
            bars=len(observations),
            performance=_performance([row.equity for row in observations], _EMPTY_TRADES),
        )

    @staticmethod
    def _benchmark_performance(
        observations: Sequence[TrackedObservation],
    ) -> Mapping[str, float | None] | None:
        """同期基准的绩效：用来回答"这几个月它跑赢躺平了吗"。"""

        values = [row.benchmark_value for row in observations if row.benchmark_value is not None]
        if len(values) < 2:
            return None
        return _performance(values, _EMPTY_TRADES)


def _read_launched_at(record: Mapping[str, object]) -> int | None:
    raw = record.get("launched_at")
    if not raw:
        return None
    try:
        return _date_key(raw)
    except (TypeError, ValueError):
        return None


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _research_from_summary(summary: object) -> TrackedSegment | None:
    """研究段从上线的摘要里读回来——它冻结过，不需要重算。"""

    if not isinstance(summary, Mapping):
        return None
    payload = summary.get("tracking_research")
    if not isinstance(payload, Mapping):
        return None
    performance = payload.get("performance")
    if not isinstance(performance, Mapping):
        return None
    return TrackedSegment(
        start_date=str(payload.get("start_date") or ""),
        end_date=str(payload.get("end_date") or ""),
        bars=int(_optional_int(payload.get("bars")) or 0),
        performance={
            key: _finite_or_none(value)
            for key, value in performance.items()
            if isinstance(key, str)
        },
    )


def _excess(
    observation: TrackedSegment | None,
    benchmark: Mapping[str, float | None] | None,
) -> float | None:
    if observation is None or benchmark is None:
        return None
    strategy = observation.performance.get("annual_return")
    baseline = benchmark.get("annual_return")
    if strategy is None or baseline is None:
        return None
    return float(strategy) - float(baseline)


__all__ = [
    "MIN_OBSERVATION_BARS",
    "StrategyTrackingService",
    "TrackingRunner",
    "TrackedObservation",
    "TrackedSegment",
    "TrackingSnapshot",
    "build_observations",
    "build_segments",
]
