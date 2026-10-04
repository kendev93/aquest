"""用真实行情验证本地复权：除权日的跳空是否真的被抹平。

拉取 eastmoney 的分红送转明细，按本地 raw 行情的前收盘价算出每条事件的因子，
然后对比 raw 与复权后的**日收益率**：除权日 raw 会显示一个断崖式的跳空（浦发银行
2002-08-22 一天 -33%），复权之后这个跳空应该基本消失，剩下的就是当天的真实涨跌。

    docker compose run --rm --no-deps -T backend python scripts/verify_adjustment.py
    docker compose run --rm --no-deps -T backend python scripts/verify_adjustment.py --code 600000
"""

from __future__ import annotations

import argparse
import sys

import pandas as pd

from selector_app.adapters.online_market_sync import _default_module
from selector_app.market_data.adjustment import CorporateAction, build_factor_series, mark_adjusted
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.market_data.store_base import default_database_path
from selector_app.market_data.store_bars import corporate_actions_from_frame

#: 浦发银行 600000 在库里 raw 序列上单日跌幅超过 15% 的日子，全部是送转/派息造成的
#: 除权跳空，不是真实暴跌。用它们当回归样本最直接。
PUFA_EX_DATES = (
    "2002-08-22",
    "2008-04-24",
    "2009-06-09",
    "2010-06-10",
    "2011-06-03",
    "2017-05-25",
)


def _prev_close_lookup(closes: pd.Series) -> object:
    def prev_close_for(ex_date: object) -> float | None:
        prior = closes[closes.index < pd.Timestamp(ex_date)]
        return float(prior.iloc[-1]) if len(prior) else None

    return prev_close_for


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code", default="600000", help="股票代码（默认浦发银行）")
    parser.add_argument(
        "--database",
        default=None,
        help="行情库路径；默认用项目配置的数据库",
    )
    parser.add_argument("--write", action="store_true", help="把事件写入事件表（默认只验证不落库）")
    parser.add_argument("--timeout", type=float, default=25.0)
    args = parser.parse_args(argv)

    database = args.database
    store = DuckDbMarketDataStore(database or default_database_path())
    market = "SH" if args.code.startswith(("6", "9")) else "SZ"
    frame = store.read_bars(market, args.code)
    if frame.empty:
        print(f"{args.code} 没有本地行情，无法验证", file=sys.stderr)
        return 1
    print(f"{market}{args.code} 本地 raw 行情 {len(frame)} 根：{frame['date'].min().date()} → {frame['date'].max().date()}")

    closes = frame.assign(ts=frame["date"]).set_index("ts")["close"].sort_index()
    module = _default_module()
    try:
        actions, skipped = module.fetch_corporate_actions(
            market,
            args.code,
            prev_close_for=_prev_close_lookup(closes),
            timeout=args.timeout,
        )
    except Exception as exc:  # noqa: BLE001 - 校验脚本要把网络失败说清楚
        print(f"拉取分红送配失败：{exc}", file=sys.stderr)
        return 1

    print(f"拉到已实施事件 {len(actions)} 条，算不出因子被跳过 {len(skipped)} 条：{skipped}")
    for action in actions[:5]:
        print(
            f"  {action.ex_date}  {action.action_type:<8}"
            f"  股本扩张 {action.share_ratio:<6} 每股派息 {action.cash_per_share:<6}"
            f" 因子 {action.factor_ratio:.6f}"
        )
    if not actions:
        print("没有已实施事件，复权无从谈起", file=sys.stderr)
        return 1

    effective = actions if args.write else _merge_with_stored(store, market, args.code, actions)
    adjusted = mark_adjusted(frame, effective)

    print("\n除权日当天的日收益率（raw 是断崖，复权后应接近真实涨跌）：")
    dates = frame["date"]
    raw_return = frame["close"].pct_change()
    adjusted_return = adjusted["close"].pct_change()
    worst = 0.0
    for text in PUFA_EX_DATES:
        position = pd.Timestamp(text)
        matches = dates == position
        if not matches.any():
            continue
        index = int(matches[matches].index[0])
        before = float(raw_return.iloc[index]) * 100
        after = float(adjusted_return.iloc[index]) * 100
        worst = max(worst, abs(after))
        print(f"  {text}  raw {before:7.2f}%   →   复权 {after:6.2f}%")
    print(f"\n复权后残留的最大跳空幅度：{worst:.2f}%")

    factors = build_factor_series(dates, effective)
    print(f"因子取值：{sorted(set(round(float(value), 6) for value in factors))[:6]}")

    if args.write:
        written = store.upsert_corporate_actions(actions, provider="eastmoney")
        print(f"\n已写入事件表 {written} 条")
    else:
        print("\n（只验证未落库，加 --write 才写入事件表）")
    return 0


def _merge_with_stored(
    store: DuckDbMarketDataStore,
    market: str,
    code: str,
    fetched: list[CorporateAction],
) -> list[CorporateAction]:
    """把库里已有事件与刚拉到的合并，让验证覆盖完整因子序列。"""

    stored = corporate_actions_from_frame(store.read_corporate_actions(market, code))
    merged = {(action.ex_date, action.action_type): action for action in stored}
    merged.update({(action.ex_date, action.action_type): action for action in fetched})
    return list(merged.values())


if __name__ == "__main__":
    raise SystemExit(main())
