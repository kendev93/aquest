#!/usr/bin/env python3
"""把一次组合回测的亏损拆开看：亏在哪一年、亏在多长的持仓、亏在多深的幅度。

它回答的是"亏损的结构"，不是"某只股票该不该买"——所以**不做 ST/退市过滤**，
也不对标的做任何好坏判断：标的名称只是标签，用来定位，不参与统计口径。

口径说明（读报告前必看）：
  * 盈亏以**平仓配对（round trip）**为单位，而不是以成交行为单位：一次买入
    可能分两次卖出，按 FIFO 拆到每一段上，持有天数才是真实的。
  * ``pnl`` 已扣买入佣金、卖出佣金和印花税；收益率 = ``pnl / cost_basis``，
    分母是含买入成本的本金，所以可以直接和"跌了百分之几"对上。
  * 期末仍持有的仓位没有平仓，单独作为"未实现浮亏"一节列出，不混进已实现。
  * 名称是当前库里的快照，不是买入当天的名称，别用它做历史归因。

用法：
    python scripts/analyze_portfolio_losses.py --result-json /tmp/pb_result.json
    python scripts/analyze_portfolio_losses.py --database /data/market/market.duckdb
    python scripts/analyze_portfolio_losses.py --database ... --history-id <id>

数据库只以只读方式打开（DuckDB 单写者）。在容器里跑：
    scripts/container_python.sh scripts/analyze_portfolio_losses.py --database <库路径>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

LATEST = "（最新组合回测）"

#: 持有天数分桶。右端点含在内，最后一段是开区间。
HOLDING_BUCKETS: tuple[tuple[int, int, str], ...] = (
    (0, 10, "≤10 日"),
    (10, 30, "11–30 日"),
    (30, 60, "31–60 日"),
    (60, 120, "61–120 日"),
    (120, 10**9, ">120 日"),
)

#: 收益率分桶（含下界，不含上界，最后一段含上界）。
RETURN_BUCKETS: tuple[tuple[float, float, str], ...] = (
    (-1.0, -0.30, "≤ -30%"),
    (-0.30, -0.20, "-30% ~ -20%"),
    (-0.20, -0.10, "-20% ~ -10%"),
    (-0.10, 0.0, "-10% ~ 0%"),
    (0.0, 0.10, "0% ~ 10%"),
    (0.10, 0.20, "10% ~ 20%"),
    (0.20, 0.50, "20% ~ 50%"),
    (0.50, 10.0, "> 50%"),
)


@dataclass(frozen=True)
class RoundTrip:
    """一段完整的持仓：一批买入份额被卖出时结算一次。"""

    market: str
    code: str
    name: str
    entry_date: str
    exit_date: str
    holding_days: int
    size: float
    pnl: float
    cost_basis: float
    reason: str

    @property
    def return_pct(self) -> float:
        if self.cost_basis <= 0:
            return 0.0
        return self.pnl / self.cost_basis

    @property
    def label(self) -> str:
        return f"{self.market}{self.code} {self.name}"


def resolve_database_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    configured = os.getenv("AQUEST_DATA_DIR")
    if configured:
        return Path(configured).expanduser() / "market.duckdb"
    try:
        from aquest.market_data.store_base import default_database_path
    except ImportError:
        return Path.home() / ".aquest" / "market.duckdb"
    return default_database_path()


def load_result(
    database: str | None,
    history_id: str | None,
    result_json: str | None,
) -> dict[str, object]:
    """取一份组合回测报告：可以直接给 JSON 文件，也可以从库里读最新一条。"""

    if result_json:
        payload = json.loads(Path(result_json).expanduser().read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise SystemExit("结果 JSON 顶层必须是对象")
        return payload

    import duckdb

    path = resolve_database_path(database)
    if not path.exists():
        raise SystemExit(f"行情库不存在: {path}")
    connection = duckdb.connect(str(path), read_only=True)
    try:
        if history_id:
            rows = connection.execute(
                """
                SELECT result_json FROM backtest_history
                WHERE history_id = ? AND backtest_type = ?
                """,
                [history_id, "portfolio"],
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT result_json FROM backtest_history
                WHERE backtest_type = 'portfolio'
                ORDER BY finished_at DESC, history_id DESC
                LIMIT 1
                """
            ).fetchall()
    finally:
        connection.close()
    if not rows:
        raise SystemExit("库里没有可用的组合回测历史")
    payload = json.loads(str(rows[0][0]))
    if not isinstance(payload, dict):
        raise SystemExit("回测历史里的结果是坏数据")
    return payload


def _date_key(value: object) -> int:
    text = str(value).strip()
    return int(text.replace("-", ""))


def _days_between(start: str, end: str) -> int:
    """按自然日近似持有天数：交易日历要靠库，这里只要量级。"""

    from datetime import date

    def parse(text: str) -> date:
        digits = _date_key(text)
        return date(digits // 10000, digits // 100 % 100, digits % 100)

    return (parse(end) - parse(start)).days


def pair_round_trips(trades: Iterable[Mapping[str, object]]) -> list[RoundTrip]:
    """按标的 FIFO 把买入和卖出配成完整的持仓段。"""

    from collections import defaultdict, deque

    open_lots: dict[str, deque[tuple[str, float]]] = defaultdict(deque)
    trips: list[RoundTrip] = []
    for trade in sorted(trades, key=lambda row: _date_key(row.get("date"))):
        direction = str(trade.get("direction") or "")
        market = str(trade.get("market") or "")
        code = str(trade.get("code") or "")
        key = f"{market}.{code}"
        size = _num(trade.get("size"))
        if direction == "BUY":
            if size > 0:
                open_lots[key].append((str(trade.get("date") or ""), size))
            continue
        if direction != "SELL" or size <= 0:
            continue
        remaining = size
        pnl_total = _num(trade.get("pnl"))
        basis_total = _num(trade.get("cost_basis"))
        exit_date = str(trade.get("date") or "")
        while remaining > 1e-9 and open_lots[key]:
            entry_date, lot_size = open_lots[key][0]
            used = min(lot_size, remaining)
            share = used / size if size else 0.0
            trips.append(
                RoundTrip(
                    market=market,
                    code=code,
                    name=str(trade.get("name") or ""),
                    entry_date=entry_date,
                    exit_date=exit_date,
                    holding_days=max(0, _days_between(entry_date, exit_date)),
                    size=used,
                    pnl=pnl_total * share,
                    cost_basis=basis_total * share if basis_total else 0.0,
                    reason=str(trade.get("reason") or ""),
                )
            )
            remaining -= used
            left = lot_size - used
            if left <= 1e-9:
                open_lots[key].popleft()
            else:
                open_lots[key][0] = (entry_date, left)
        if remaining > 1e-9:
            # 卖出量超过记录的买入量（回测被截断或数据缺行）：按整笔记一次，
            # 持有天数未知，用 0 标记而不是编一个数。
            trips.append(
                RoundTrip(
                    market=market,
                    code=code,
                    name=str(trade.get("name") or ""),
                    entry_date="",
                    exit_date=exit_date,
                    holding_days=0,
                    size=remaining,
                    pnl=pnl_total * (remaining / size if size else 0.0),
                    cost_basis=basis_total * (remaining / size if size else 0.0),
                    reason=str(trade.get("reason") or ""),
                )
            )
    return sorted(trips, key=lambda trip: _date_key(trip.exit_date))


def _num(value: object) -> float:
    """报告里的数字可能是 int/float/None/字符串，统一成有限浮点数。"""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    number = float(value)
    return number if number == number and number not in (float("inf"), float("-inf")) else 0.0


def _money(value: float) -> str:
    return f"{value:,.0f}"


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _bucket_stats(
    trips: Sequence[RoundTrip],
    buckets: Sequence[tuple[float, float, str]],
    *,
    value_of: Callable[[RoundTrip], float],
) -> list[tuple[str, int, int, float, float]]:
    rows: list[tuple[str, int, int, float, float]] = []
    for low, high, label in buckets:
        picked = [trip for trip in trips if low <= value_of(trip) < high]
        if not picked:
            rows.append((label, 0, 0, 0.0, 0.0))
            continue
        losses = [trip.pnl for trip in picked if trip.pnl < 0]
        rows.append((label, len(picked), len(losses), sum(t.pnl for t in picked), sum(losses)))
    return rows


def _holding_value(trip: RoundTrip) -> float:
    return float(trip.holding_days)


def _return_value(trip: RoundTrip) -> float:
    return trip.return_pct


def _table(header: tuple[str, ...], rows: Sequence[tuple[str, ...]]) -> list[str]:
    widths = [len(name) for name in header]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(str(cell)))
    lines = ["  ".join(name.ljust(widths[index]) for index, name in enumerate(header))]
    lines.append("  ".join("-" * width for width in widths))
    for row in rows:
        lines.append("  ".join(str(cell).ljust(widths[index]) for index, cell in enumerate(row)))
    return lines


def _drawdown_episodes(result: Mapping[str, object]) -> list[tuple[str, str, float, int]]:
    """净值曲线上最深的三段回撤：(起点, 终点, 深度, 持续交易日)。"""

    curve = result.get("equity_curve")
    if not isinstance(curve, list):
        return []
    episodes: list[tuple[str, str, float, int]] = []
    peak_date = ""
    peak_value = 0.0
    trough_date = ""
    trough_value = 0.0
    bars = 0
    for row in curve:
        if not isinstance(row, Mapping):
            continue
        date = str(row.get("date") or "")
        total = float(row.get("total") or 0.0)
        if total >= peak_value:
            if trough_value and trough_value < peak_value:
                episodes.append((peak_date, trough_date, trough_value / peak_value - 1.0, bars))
            peak_value, peak_date = total, date
            trough_value, trough_date = 0.0, ""
            bars = 0
            continue
        bars += 1
        if not trough_value or total < trough_value:
            trough_value, trough_date = total, date
    if trough_value and trough_value < peak_value:
        episodes.append((peak_date, trough_date, trough_value / peak_value - 1.0, bars))
    return sorted(episodes, key=lambda item: item[2])[:3]


def _benchmark_change(result: Mapping[str, object], start: str, end: str) -> float | None:
    """同期基准（等权买入持有）的涨跌幅。"""

    benchmark = result.get("benchmark")
    if not isinstance(benchmark, Mapping):
        return None
    dates = benchmark.get("dates")
    totals = benchmark.get("totals")
    if not isinstance(dates, list) or not isinstance(totals, list):
        return None
    start_key, end_key = _date_key(start), _date_key(end)
    begin: float | None = None
    finish: float | None = None
    for index, date in enumerate(dates):
        key = int(date)
        if begin is None and key >= start_key:
            begin = _num(totals[index])
        if key <= end_key:
            finish = _num(totals[index])
    if begin is None or finish is None or begin <= 0:
        return None
    return finish / begin - 1.0


def build_report(result: Mapping[str, object], *, source: str = LATEST) -> str:
    trades_raw = result.get("trades")
    trades = (
        [row for row in trades_raw if isinstance(row, Mapping)]
        if isinstance(trades_raw, list)
        else []
    )
    trips = pair_round_trips(trades)
    winners = [trip for trip in trips if trip.pnl > 0]
    losers = [trip for trip in trips if trip.pnl < 0]
    gross_win = sum(trip.pnl for trip in winners)
    gross_loss = -sum(trip.pnl for trip in losers)
    performance = result.get("performance")
    performance = performance if isinstance(performance, Mapping) else {}

    costs = sum(
        _num(row.get("commission")) + _num(row.get("stamp_tax")) + _num(row.get("slippage"))
        for row in trades
    )

    lines: list[str] = []
    lines.append("# 组合回测亏损分析")
    lines.append("")
    lines.append(f"- 回测：{source}")
    lines.append(
        f"- 区间：{result.get('start_date')} → {result.get('end_date')}"
        f"（{result.get('bars')} 个交易日）"
    )
    if performance.get("total_return") is not None:
        lines.append(
            f"- 整体：总收益 {_pct(float(performance.get('total_return') or 0.0))}"
            f"，年化 {_pct(float(performance.get('annual_return') or 0.0))}"
            f"，最大回撤 {_pct(float(performance.get('max_drawdown') or 0.0))}"
        )
    lines.append("")

    lines.append("## 1. 总览")
    lines.append("")
    lines.extend(
        _table(
            ("指标", "数值"),
            [
                ("平仓段数", str(len(trips))),
                ("盈利段", f"{len(winners)}（{len(winners) / len(trips):.1%}）" if trips else "0"),
                ("亏损段", f"{len(losers)}（{len(losers) / len(trips):.1%}）" if trips else "0"),
                ("已实现净盈亏", _money(sum(trip.pnl for trip in trips))),
                ("盈利总额", _money(gross_win)),
                ("亏损总额", _money(-gross_loss)),
                (
                    "盈亏比",
                    f"{gross_win / gross_loss:.2f}" if gross_loss else "—",
                ),
                (
                    "平均盈利 / 平均亏损",
                    (
                        f"{_money(gross_win / len(winners))} / {_money(gross_loss / len(losers))}"
                        if winners and losers
                        else "—"
                    ),
                ),
                (
                    "最深单笔",
                    min((trip.pnl for trip in trips), default=0.0).__format__(",.0f"),
                ),
                ("交易成本（费税滑点）", _money(costs)),
                (
                    "成本 / 亏损总额",
                    f"{costs / gross_loss:.1%}" if gross_loss else "—",
                ),
            ],
        )
    )
    lines.append("")

    lines.append("## 2. 亏损发生在哪一年")
    lines.append("")
    by_year: dict[str, list[RoundTrip]] = {}
    for trip in trips:
        by_year.setdefault(trip.exit_date[:4], []).append(trip)
    rows: list[tuple[str, ...]] = []
    for year in sorted(by_year):
        picked = by_year[year]
        loss = -sum(trip.pnl for trip in picked if trip.pnl < 0)
        rows.append(
            (
                year,
                str(len(picked)),
                str(sum(1 for trip in picked if trip.pnl < 0)),
                _money(sum(trip.pnl for trip in picked)),
                _money(-loss),
            )
        )
    lines.extend(_table(("年份", "平仓", "亏损", "净盈亏", "亏损额"), rows))
    lines.append("")

    lines.append("## 3. 亏损与持有天数")
    lines.append("")
    rows = [
        (label, str(count), str(loss_count), _money(total), _money(loss))
        for label, count, loss_count, total, loss in _bucket_stats(
            trips, HOLDING_BUCKETS, value_of=_holding_value
        )
    ]
    lines.extend(_table(("持有天数", "平仓", "亏损", "净盈亏", "亏损额"), rows))
    lines.append("")

    lines.append("## 4. 亏损幅度分布")
    lines.append("")
    rows = [
        (label, str(count), str(loss_count), _money(total), _money(loss))
        for label, count, loss_count, total, loss in _bucket_stats(
            trips, RETURN_BUCKETS, value_of=_return_value
        )
    ]
    lines.extend(_table(("收益率区间", "平仓", "亏损", "净盈亏", "亏损额"), rows))
    lines.append("")

    lines.append("## 5. 卖出原因")
    lines.append("")
    by_reason: dict[str, list[RoundTrip]] = {}
    for trip in trips:
        by_reason.setdefault(trip.reason or "未标注", []).append(trip)
    rows = []
    for reason in sorted(by_reason, key=lambda key: sum(t.pnl for t in by_reason[key])):
        picked = by_reason[reason]
        rows.append(
            (
                reason,
                str(len(picked)),
                str(sum(1 for trip in picked if trip.pnl < 0)),
                _money(sum(trip.pnl for trip in picked)),
                _money(sum(trip.pnl for trip in picked if trip.pnl < 0)),
            )
        )
    lines.extend(_table(("原因", "平仓", "亏损", "净盈亏", "亏损额"), rows))
    lines.append("")

    lines.append("## 6. 亏损最重的 15 段平仓")
    lines.append("")
    worst = sorted(trips, key=lambda trip: trip.pnl)[:15]
    rows = [
        (
            trip.label,
            f"{trip.entry_date}→{trip.exit_date}",
            f"{trip.holding_days}d",
            _pct(trip.return_pct),
            _money(trip.pnl),
            trip.reason,
        )
        for trip in worst
    ]
    lines.extend(_table(("标的", "持有区间", "天数", "收益率", "盈亏", "原因"), rows))
    lines.append("")

    lines.append("## 7. 连续亏损")
    lines.append("")
    streak = max_streak = 0
    streak_loss = worst_streak_loss = 0.0
    for trip in trips:
        if trip.pnl < 0:
            streak += 1
            streak_loss += trip.pnl
            max_streak = max(max_streak, streak)
            worst_streak_loss = min(worst_streak_loss, streak_loss)
        else:
            streak, streak_loss = 0, 0.0
    last_streak = 0
    for trip in reversed(trips):
        if trip.pnl < 0:
            last_streak += 1
        else:
            break
    lines.extend(
        _table(
            ("指标", "数值"),
            [
                ("最长连亏段数", str(max_streak)),
                ("连亏期间最大累计亏损", _money(worst_streak_loss)),
                ("期末是否处于连亏中", f"是（{last_streak} 段）" if last_streak else "否"),
            ],
        )
    )
    lines.append("")

    lines.append("## 8. 净值最深的三段回撤")
    lines.append("")
    episodes = _drawdown_episodes(result)
    if episodes:
        rows = []
        for start, end, depth, bars in episodes:
            change = _benchmark_change(result, start, end)
            rows.append(
                (
                    f"{start} → {end}",
                    f"{bars} 日",
                    _pct(depth),
                    "—" if change is None else _pct(change),
                )
            )
        lines.extend(_table(("区间", "持续", "回撤深度", "同期基准"), rows))
    else:
        lines.append("（净值曲线为空，无法计算）")
    lines.append("")

    lines.append("## 9. 期末未实现浮亏")
    lines.append("")
    states = result.get("states")
    holdings: list[Mapping[str, object]] = []
    if isinstance(states, list) and states:
        last = states[-1]
        if isinstance(last, Mapping) and isinstance(last.get("holdings"), list):
            holdings = [row for row in last["holdings"] if isinstance(row, Mapping)]
    if holdings:
        rows = []
        for row in sorted(holdings, key=lambda item: _num(item.get("unrealized_pnl"))):
            entry = _num(row.get("entry_price"))
            close = _num(row.get("close"))
            rows.append(
                (
                    f"{row.get('market')}{row.get('code')} {row.get('name')}",
                    f"{entry:.2f} → {close:.2f}",
                    _pct(close / entry - 1.0) if entry else "—",
                    _money(_num(row.get("unrealized_pnl"))),
                )
            )
        lines.extend(_table(("标的", "成本→现价", "浮动", "浮盈亏"), rows))
        unrealized = sum(_num(row.get("unrealized_pnl")) for row in holdings)
        lines.append("")
        lines.append(f"合计浮盈亏：{_money(unrealized)}（亏损额 {_money(min(unrealized, 0.0))}）")
    else:
        lines.append("（无期末持仓）")
    lines.append("")

    lines.append("## 10. 风控设置")
    lines.append("")
    risk = result.get("risk_controls")
    risk = risk if isinstance(risk, Mapping) else {}

    def describe(prefix: str) -> str:
        pct = _num(risk.get(f"{prefix}_pct"))
        atr = _num(risk.get(f"{prefix}_atr_multiplier"))
        if pct > 0:
            return f"{_pct(pct)}（固定比例）"
        if atr > 0:
            return f"{atr:g}×ATR"
        return "未启用"

    lines.extend(
        _table(
            ("项目", "设置"),
            [
                ("止盈", describe("take_profit")),
                ("止损", describe("stop_loss")),
                ("移动止损", describe("trailing_stop")),
            ],
        )
    )
    if _num(risk.get("stop_loss_pct")) <= 0 and _num(risk.get("trailing_stop_pct")) <= 0:
        lines.append("")
        lines.append("没有止损/移动止损：亏损段只能等卖出条件触发，深度由行情决定。")
    lines.append("")
    lines.append("> 标的名称取自行情库当前快照，不代表买入当天的状态，请勿据此做历史归因。")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="分析一次组合回测的亏损结构")
    parser.add_argument("--database", help="行情库路径（只读打开），默认取本机/容器默认库")
    parser.add_argument("--history-id", help="指定组合回测历史 ID，默认取最新一条")
    parser.add_argument("--result-json", help="直接用已导出的回测结果 JSON")
    args = parser.parse_args(argv)

    if not args.database and not args.result_json:
        parser.error("需要 --result-json 或 --database")
    result = load_result(args.database, args.history_id, args.result_json)
    source = args.history_id or ("（结果 JSON）" if args.result_json else LATEST)
    print(build_report(result, source=source))
    return 0


if __name__ == "__main__":
    sys.exit(main())
