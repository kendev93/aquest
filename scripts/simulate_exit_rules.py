#!/usr/bin/env python3
"""退出规则的离线反事实模拟：给"最多持仓多久""移动止损"这类改动先算一笔账。

它回答的问题很窄：**如果这次回测的每一段持仓额外多一条退出规则，盈亏会怎么变。**

做法是把成交明细按标的 FIFO 配成持仓段（口径同 scripts/analyze_portfolio_losses.py），
再拿真实日线重放这一段：规则在哪天触发、触发后按次日开盘价（与回测 execution=next_open
一致）卖出、扣掉印花税和佣金，得到这一段的新盈亏，和实际盈亏相减。

**它不是完整重跑，读数字时必须记住这三条：**
1. 只替换"这一段"的退出点，不重放槽位补位——提前卖出会空出槽位并买入别的标的，
   那部分收益这里完全没有，所以结果是"单段视角"的，不是策略的最终绩效。
2. 触发判定用当日收盘 / 最高价，成交用次日开盘，不模拟涨跌停无法成交和停牌。
3. 移动止损按"收盘跌破最高价的 X%"判定，真实实现可能用盘中低点或别的基准，
   触发点会有差异；等引擎里的移动止损做完，要用它真正重跑一次才算数。

用法（宿主机，日线已导出成 CSV）：
    python scripts/simulate_exit_rules.py --result-json /tmp/pb_result.json --bars /tmp/bars.csv

用法（容器内直接读库，默认取最新一次组合回测）：
    scripts/container_python.sh scripts/simulate_exit_rules.py
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SELL_FEE_RATE = 0.0011  # 印花税 0.001 + 佣金 0.0001，与组合回测默认一致
DEFAULT_BUY_FEE_RATE = 0.0001
DATETIME_LEN = 10


@dataclass(frozen=True)
class Trip:
    """一段持仓。exit_date 为空表示回测结束时还没平仓。"""

    market: str
    code: str
    name: str
    entry_date: str
    exit_date: str
    size: float
    cost_basis: float
    pnl: float
    reason: str

    @property
    def label(self) -> str:
        return f"{self.market}{self.code} {self.name}".strip()


@dataclass(frozen=True)
class Rule:
    """一条候选退出规则。三个都为 None 表示"不改"，即实际结果。"""

    label: str
    hold_days: int | None = None
    trailing_pct: float | None = None
    stop_loss_pct: float | None = None
    # 建仓后 weak_days 个交易日仍弱于 weak_pct，就当买错了先出来。
    weak_days: int | None = None
    weak_pct: float | None = None
    # 移动止损的激活线：先涨到这个幅度才开始跟，避免和止盈抢着卖。
    activate_pct: float | None = None


@dataclass(frozen=True)
class Simulated:
    trip: Trip
    pnl: float
    exit_date: str
    triggered: bool


# 行情序列：日期 -> (开盘, 最高, 最低, 收盘)，按日期升序。
Bars = dict[tuple[str, str], list[tuple[str, float, float, float, float]]]


def _num(value: object) -> float:
    """CSV 里全是字符串，所以这里要把数字文本也认下来。"""

    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return 0.0
    else:
        return 0.0
    return number if number == number and number not in (float("inf"), float("-inf")) else 0.0


def _key(market: object, code: object) -> tuple[str, str]:
    return (str(market), str(code))


def pair_trips(trades: Sequence[Mapping[str, object]]) -> tuple[list[Trip], list[Trip]]:
    """配对出已平仓段，剩下的买入批次作为期末持仓段返回。"""

    ordered = sorted(trades, key=lambda row: str(row.get("date") or ""))
    lots: dict[tuple[str, str], list[dict[str, object]]] = {}
    closed: list[Trip] = []

    for trade in ordered:
        key = _key(trade.get("market"), trade.get("code"))
        queue = lots.setdefault(key, [])
        date = str(trade.get("date") or "")
        size = _num(trade.get("size"))
        if str(trade.get("direction")) == "BUY":
            if size > 0:
                queue.append(
                    {
                        "date": date,
                        "size": size,
                        "name": str(trade.get("name") or ""),
                        "price": _num(trade.get("price")),
                    }
                )
            continue
        if str(trade.get("direction")) != "SELL" or size <= 0:
            continue
        remaining = size
        pnl_total = _num(trade.get("pnl"))
        basis_total = _num(trade.get("cost_basis"))
        while remaining > 1e-9 and queue:
            lot = queue[0]
            lot_size = _num(lot["size"])
            used = min(lot_size, remaining)
            share = used / size
            closed.append(
                Trip(
                    market=key[0],
                    code=key[1],
                    name=str(trade.get("name") or ""),
                    entry_date=str(lot["date"]),
                    exit_date=date,
                    size=used,
                    cost_basis=basis_total * share,
                    pnl=pnl_total * share,
                    reason=str(trade.get("reason") or "未标注"),
                )
            )
            remaining -= used
            lot["size"] = lot_size - used
            if _num(lot["size"]) <= 1e-9:
                queue.pop(0)
        if remaining > 1e-9:
            share = remaining / size
            closed.append(
                Trip(
                    market=key[0],
                    code=key[1],
                    name=str(trade.get("name") or ""),
                    entry_date="",
                    exit_date=date,
                    size=remaining,
                    cost_basis=basis_total * share,
                    pnl=pnl_total * share,
                    reason=str(trade.get("reason") or "未标注"),
                )
            )

    open_lots = [
        Trip(
            market=key[0],
            code=key[1],
            name=str(lot.get("name") or ""),
            entry_date=str(lot.get("date") or ""),
            exit_date="",
            size=_num(lot.get("size")),
            cost_basis=_num(lot.get("size")) * _num(lot.get("price")) * (1 + DEFAULT_BUY_FEE_RATE),
            pnl=0.0,
            reason="期末未平仓",
        )
        for key, queue in lots.items()
        for lot in queue
        if _num(lot.get("size")) > 1e-9
    ]
    return closed, open_lots


def attach_unrealized(open_lots: Sequence[Trip], result: Mapping[str, object]) -> list[Trip]:
    """给期末未平仓段补上浮亏：不补的话基准里就少了期末这一块。"""

    states = result.get("states")
    if not isinstance(states, list) or not states:
        return list(open_lots)
    last = states[-1]
    if not isinstance(last, Mapping):
        return list(open_lots)
    holdings = last.get("holdings")
    if not isinstance(holdings, list):
        return list(open_lots)

    totals: dict[tuple[str, str], float] = {}
    unrealized: dict[tuple[str, str], float] = {}
    for row in holdings:
        if not isinstance(row, Mapping):
            continue
        key = _key(row.get("market"), row.get("code"))
        totals[key] = totals.get(key, 0.0) + _num(row.get("size"))
        unrealized[key] = unrealized.get(key, 0.0) + _num(row.get("unrealized_pnl"))

    rows: list[Trip] = []
    for lot in open_lots:
        key = (lot.market, lot.code)
        total = totals.get(key, 0.0)
        share = lot.size / total if total > 0 else 0.0
        rows.append(
            Trip(
                market=lot.market,
                code=lot.code,
                name=lot.name,
                entry_date=lot.entry_date,
                exit_date="",
                size=lot.size,
                cost_basis=lot.cost_basis,
                pnl=unrealized.get(key, 0.0) * share,
                reason="期末未平仓",
            )
        )
    return rows


def trips_from_holdings(result: Mapping[str, object]) -> list[Trip]:
    """老结果里 states 缺字段时，用期末持仓兜底成"未平仓段"。"""

    states = result.get("states")
    if not isinstance(states, list) or not states:
        return []
    last = states[-1]
    if not isinstance(last, Mapping):
        return []
    holdings = last.get("holdings")
    if not isinstance(holdings, list):
        return []
    rows: list[Trip] = []
    for row in holdings:
        if not isinstance(row, Mapping):
            continue
        rows.append(
            Trip(
                market=str(row.get("market") or ""),
                code=str(row.get("code") or ""),
                name=str(row.get("name") or ""),
                entry_date="",
                exit_date="",
                size=_num(row.get("size")),
                cost_basis=_num(row.get("size"))
                * _num(row.get("entry_price"))
                * (1 + DEFAULT_BUY_FEE_RATE),
                pnl=_num(row.get("unrealized_pnl")),
                reason="期末未平仓",
            )
        )
    return rows


def load_bars_csv(path: Path, keys: Iterable[tuple[str, str]]) -> Bars:
    wanted = set(keys)
    bars: Bars = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            key = _key(row.get("market"), row.get("code"))
            if key not in wanted:
                continue
            bars.setdefault(key, []).append(
                (
                    str(row.get("trade_date") or "")[:DATETIME_LEN],
                    _num(row.get("open")),
                    _num(row.get("high")),
                    _num(row.get("low")),
                    _num(row.get("close")),
                )
            )
    for series in bars.values():
        series.sort()
    return bars


def load_bars_duckdb(
    database: Path,
    keys: Sequence[tuple[str, str]],
    start: str,
    end: str,
) -> Bars:
    import duckdb

    connection = duckdb.connect(str(database), read_only=True)
    try:
        placeholders = ", ".join("(?, ?)" for _ in keys)
        parameters: list[object] = []
        for market, code in keys:
            parameters.extend([market, code])
        rows = connection.execute(
            f"""
            SELECT market, code, trade_date, open, high, low, close
            FROM daily_bars
            WHERE (market, code) IN ({placeholders})
              AND trade_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
            ORDER BY market, code, trade_date
            """,
            [*parameters, start, end],
        ).fetchall()
    finally:
        connection.close()
    bars: Bars = {}
    for market, code, trade_date, open_, high, low, close in rows:
        bars.setdefault(_key(market, code), []).append(
            (str(trade_date)[:DATETIME_LEN], _num(open_), _num(high), _num(low), _num(close))
        )
    for series in bars.values():
        series.sort()
    return bars


def _index_on_or_after(series: Sequence[tuple[str, float, float, float, float]], date: str) -> int:
    for index, row in enumerate(series):
        if row[0] >= date:
            return index
    return -1


def _index_on_or_before(series: Sequence[tuple[str, float, float, float, float]], date: str) -> int:
    found = -1
    for index, row in enumerate(series):
        if row[0] <= date:
            found = index
        else:
            break
    return found


def simulate_trip(
    trip: Trip,
    series: Sequence[tuple[str, float, float, float, float]],
    rule: Rule,
    sell_fee_rate: float,
) -> Simulated:
    """回放一段持仓，返回规则下的新盈亏。触发就用次日开盘价成交。"""

    if not series:
        return Simulated(trip=trip, pnl=trip.pnl, exit_date=trip.exit_date, triggered=False)
    entry_index = _index_on_or_after(series, trip.entry_date)
    if entry_index < 0:
        return Simulated(trip=trip, pnl=trip.pnl, exit_date=trip.exit_date, triggered=False)
    last_index = len(series) - 1
    exit_index = _index_on_or_before(series, trip.exit_date) if trip.exit_date else last_index
    if exit_index < entry_index:
        exit_index = last_index

    trigger: int | None = None
    if rule.hold_days is not None:
        candidate = entry_index + rule.hold_days
        if candidate <= exit_index:
            trigger = candidate
    cost_price = trip.cost_basis / trip.size if trip.size > 0 else 0.0
    if rule.trailing_pct is not None:
        peak = 0.0
        for index in range(entry_index, exit_index + 1):
            peak = max(peak, series[index][2])
            if rule.activate_pct is not None and peak < cost_price * (1 + rule.activate_pct):
                continue
            if peak > 0 and series[index][4] <= peak * (1 - rule.trailing_pct):
                if trigger is None or index < trigger:
                    trigger = index
                break
    if rule.weak_days is not None and rule.weak_pct is not None and cost_price > 0:
        candidate = entry_index + rule.weak_days
        if candidate <= exit_index and series[candidate][4] <= cost_price * (1 - rule.weak_pct):
            if trigger is None or candidate < trigger:
                trigger = candidate
    if rule.stop_loss_pct is not None and trip.size > 0:
        floor = (trip.cost_basis / trip.size) * (1 - rule.stop_loss_pct)
        for index in range(entry_index, exit_index + 1):
            if series[index][4] <= floor:
                if trigger is None or index < trigger:
                    trigger = index
                break
    if trigger is None:
        return Simulated(
            trip=trip,
            pnl=trip.pnl,
            exit_date=trip.exit_date or series[exit_index][0],
            triggered=False,
        )

    sell_index = min(trigger + 1, last_index)
    price = series[sell_index][1]
    if price <= 0:
        price = series[trigger][4]
    pnl = trip.size * price * (1 - sell_fee_rate) - trip.cost_basis
    return Simulated(trip=trip, pnl=pnl, exit_date=series[sell_index][0], triggered=True)


@dataclass(frozen=True)
class Summary:
    label: str
    net: float
    gross_loss: float
    worst: float
    win_rate: float
    triggered: int
    changed: int
    saved: float
    given_up: float


def summarize(label: str, baseline: Sequence[Simulated], simulated: Sequence[Simulated]) -> Summary:
    sim_net = sum(item.pnl for item in simulated)
    triggered = [item for item in simulated if item.triggered]
    saved = 0.0
    given_up = 0.0
    for before, after in zip(baseline, simulated):
        if not after.triggered:
            continue
        delta = after.pnl - before.pnl
        if delta > 0:
            saved += delta
        else:
            given_up += delta
    winners = sum(1 for item in simulated if item.pnl > 0)
    return Summary(
        label=label,
        net=sim_net,
        gross_loss=-sum(item.pnl for item in simulated if item.pnl < 0),
        worst=min((item.pnl for item in simulated), default=0.0),
        win_rate=winners / len(simulated) if simulated else 0.0,
        triggered=len(triggered),
        changed=len(triggered),
        saved=saved,
        given_up=given_up,
    )


def _money(value: float) -> str:
    return f"{value:,.0f}"


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> list[str]:
    widths = [
        max(len(str(header)), *(len(str(row[index])) for row in rows)) if rows else len(str(header))
        for index, header in enumerate(headers)
    ]
    lines = ["  ".join(str(header).ljust(widths[index]) for index, header in enumerate(headers))]
    lines.append("  ".join("-" * width for width in widths))
    for row in rows:
        lines.append("  ".join(str(cell).ljust(widths[index]) for index, cell in enumerate(row)))
    return lines


def default_rules() -> list[Rule]:
    return [
        Rule("最多持有 60 日", hold_days=60),
        Rule("最多持有 90 日", hold_days=90),
        Rule("最多持有 120 日", hold_days=120),
        Rule("硬止损 15%", stop_loss_pct=0.15),
        Rule("硬止损 20%", stop_loss_pct=0.20),
        Rule("移动止损 15%", trailing_pct=0.15),
        Rule("移动止损 25%", trailing_pct=0.25),
        Rule("先涨 15% 再跟 移动止损 10%", trailing_pct=0.10, activate_pct=0.15),
        Rule("先涨 20% 再跟 移动止损 10%", trailing_pct=0.10, activate_pct=0.20),
        Rule("先涨 15% 再跟 移动止损 15%", trailing_pct=0.15, activate_pct=0.15),
        Rule("弱势早退：20 日仍跌 5%", weak_days=20, weak_pct=0.05),
        Rule("弱势早退：20 日仍跌 8%", weak_days=20, weak_pct=0.08),
        Rule("弱势早退：40 日仍跌 5%", weak_days=40, weak_pct=0.05),
        Rule("弱势早退 20 日 5% + 最多 120 日", weak_days=20, weak_pct=0.05, hold_days=120),
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="模拟最长持仓天数与移动止损对盈亏的影响")
    parser.add_argument("--result-json", help="组合回测结果 JSON（不给则从库里取最新一条）")
    parser.add_argument("--database", help="行情库路径，容器内默认 /data/market/market.duckdb")
    parser.add_argument("--bars", help="日线 CSV（market,code,trade_date,open,high,low,close）")
    parser.add_argument("--request-json", help="回测请求 JSON，用来取真实费率")
    parser.add_argument("--hold-days", help="自定义最长持仓交易日数，逗号分隔")
    parser.add_argument("--trailing", help="自定义移动止损比例（如 0.15），逗号分隔")
    parser.add_argument("--stop-loss", help="自定义硬止损比例（按成本价算），逗号分隔")
    parser.add_argument("--weak", help="弱势早退：天数:幅度，逗号分隔，如 20:0.05")
    parser.add_argument("--activate", help="移动止损的激活线：先涨到这个幅度才开始跟")
    parser.add_argument("--top", type=int, default=8, help="展示受影响最大的前 N 段")
    args = parser.parse_args(argv)

    result_path = args.result_json
    if not result_path:
        import duckdb

        database = args.database or "/data/market/market.duckdb"
        connection = duckdb.connect(database, read_only=True)
        row = connection.execute(
            "SELECT result_json FROM backtest_history WHERE backtest_type = 'portfolio' "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        connection.close()
        if row is None:
            print("库里没有组合回测历史。")
            return 1
        payload = json.loads(row[0])
        Path("/tmp").mkdir(exist_ok=True)
        result_path = "/tmp/simulate_exit_rules_result.json"
        Path(result_path).write_text(row[0], encoding="utf-8")
    else:
        payload = json.loads(Path(result_path).read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        print("回测结果格式不对。")
        return 1

    sell_fee_rate = DEFAULT_SELL_FEE_RATE
    if args.request_json:
        request = json.loads(Path(args.request_json).read_text(encoding="utf-8"))
        if isinstance(request, Mapping):
            sell_fee_rate = _num(request.get("stamp_tax")) + _num(request.get("commission"))

    trades_raw = payload.get("trades")
    trades = (
        [row for row in trades_raw if isinstance(row, Mapping)]
        if isinstance(trades_raw, list)
        else []
    )
    closed, open_lots = pair_trips(trades)
    open_lots = attach_unrealized(open_lots, payload)
    if not open_lots:
        open_lots = trips_from_holdings(payload)
    all_trips = closed + open_lots
    if not all_trips:
        print("这次回测没有可回放的持仓段。")
        return 1

    keys = sorted({(trip.market, trip.code) for trip in all_trips})
    if args.bars:
        bars = load_bars_csv(Path(args.bars), keys)
    else:
        database = args.database or "/data/market/market.duckdb"
        bars = load_bars_duckdb(Path(database), keys, "2000-01-01", "2100-01-01")
    missing = [key for key in keys if key not in bars]
    if missing:
        print(f"行情库里没有这些标的的日线，模拟会跳过：{missing[:10]}")
    sim_trips = [trip for trip in all_trips if (trip.market, trip.code) in bars]

    rules: list[Rule] = []
    activate = float(args.activate) if args.activate else None
    if args.hold_days or args.trailing or args.stop_loss or args.weak:
        for text in (args.hold_days or "").split(","):
            if text.strip():
                rules.append(Rule(f"最多持有 {text.strip()} 日", hold_days=int(text)))
        for text in (args.trailing or "").split(","):
            if text.strip():
                value = float(text)
                suffix = f"（先涨 {_pct(activate)} 激活）" if activate is not None else ""
                rules.append(
                    Rule(
                        f"移动止损 {_pct(value)}{suffix}", trailing_pct=value, activate_pct=activate
                    )
                )
        for text in (args.stop_loss or "").split(","):
            if text.strip():
                value = float(text)
                rules.append(Rule(f"硬止损 {_pct(value)}", stop_loss_pct=value))
        for text in (args.weak or "").split(","):
            if not text.strip():
                continue
            days, pct = text.split(":")
            rules.append(
                Rule(
                    f"弱势早退：{days.strip()} 日仍跌 {_pct(float(pct))}",
                    weak_days=int(days),
                    weak_pct=float(pct),
                )
            )
    if not rules:
        rules = default_rules()

    baseline = [
        Simulated(trip=trip, pnl=trip.pnl, exit_date=trip.exit_date or "", triggered=False)
        for trip in sim_trips
    ]
    base = summarize("实际（未加规则）", baseline, baseline)

    lines: list[str] = []
    lines.append("# 退出规则模拟")
    lines.append("")
    lines.append(
        f"- 回放 {len(sim_trips)} 段持仓（已平仓 {len(closed)} 段，期末未平仓 {len(open_lots)} 段）"
    )
    lines.append(f"- 卖出费率：{sell_fee_rate:.4%}（印花税 + 佣金），成交价取触发日次日开盘")
    lines.append("")
    rows: list[Sequence[object]] = []
    for rule in rules:
        simulated = [
            simulate_trip(trip, bars[(trip.market, trip.code)], rule, sell_fee_rate)
            for trip in sim_trips
        ]
        summary = summarize(rule.label, baseline, simulated)
        rows.append(
            (
                rule.label,
                summary.changed,
                _money(summary.net),
                _money(summary.net - base.net),
                _money(summary.gross_loss),
                _money(summary.gross_loss - base.gross_loss),
                _money(summary.worst),
                _pct(summary.win_rate),
                _money(summary.saved),
                _money(summary.given_up),
            )
        )
    lines.extend(
        _table(
            (
                "规则",
                "触发段",
                "净盈亏",
                "净盈亏变化",
                "亏损额",
                "亏损变化",
                "最深单笔",
                "胜率",
                "省下",
                "让出",
            ),
            [
                (
                    "实际（未加规则）",
                    "—",
                    _money(base.net),
                    "—",
                    _money(base.gross_loss),
                    "—",
                    _money(base.worst),
                    _pct(base.win_rate),
                    "—",
                    "—",
                ),
                *rows,
            ],
        )
    )
    lines.append("")
    lines.append(
        "「省下」= 被提前卖掉的段里，相比实际多赚/少亏的部分；「让出」= 提前卖掉反而少赚的部分。"
    )
    lines.append("")

    # 挑一套规则展开细节，看它到底动了哪些段。
    detail_rule = rules[0]
    simulated = [
        simulate_trip(trip, bars[(trip.market, trip.code)], detail_rule, sell_fee_rate)
        for trip in sim_trips
    ]
    deltas = [
        (after.pnl - before.pnl, before, after)
        for before, after in zip(baseline, simulated)
        if after.triggered
    ]
    deltas.sort(key=lambda item: item[0])
    lines.append(f"## 「{detail_rule.label}」触发的 {len(deltas)} 段里，变化最大的几段")
    lines.append("")
    lines.extend(
        _table(
            ("标的", "原平仓", "新卖出", "原盈亏", "新盈亏", "变化"),
            [
                (
                    after.trip.label,
                    before.trip.exit_date or "未平仓",
                    after.exit_date,
                    _money(before.pnl),
                    _money(after.pnl),
                    _money(delta),
                )
                for delta, before, after in deltas[: args.top]
            ]
            + [
                (
                    after.trip.label,
                    before.trip.exit_date or "未平仓",
                    after.exit_date,
                    _money(before.pnl),
                    _money(after.pnl),
                    _money(delta),
                )
                for delta, before, after in deltas[-args.top :]
            ],
        )
    )
    lines.append("")
    lines.append("> 这是单段回放：提前卖出空出的槽位会买什么，这里没有模拟，所以别把它当最终绩效。")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
