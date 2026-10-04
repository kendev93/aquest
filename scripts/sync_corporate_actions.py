"""批量拉取全市场分红送转事件，写入事件表。

复权因子的唯一来源就是这张表 —— 没有它，指标就仍然算在含除权跳空的 raw 序列上。
一次拉全市场约 5000 次 HTTP 请求，按项目现有的限流节奏（默认 0.15 秒一次）大约
半小时，所以支持：

* ``--limit``  先跑几十只验证脚本与接口，稳定后再放开全量
* ``--resume`` 跳过事件表里已经有的标的，中断后可以直接续跑
* 分批读行情：一次 ``read_many_bars`` 取 200 只，避免逐标的查询和全量读进内存

    # 试跑
    docker compose run --rm --no-deps -T -e PYTHONPATH=/app \\
        -v "$(pwd)/scripts:/app/scripts:ro" backend \\
        python scripts/sync_corporate_actions.py --limit 20 --write

    # 全量（后台跑，输出到日志）
    docker compose run -d --rm --no-deps -T -e PYTHONPATH=/app \\
        -v "$(pwd)/scripts:/app/scripts:ro" backend \\
        python scripts/sync_corporate_actions.py --write --resume
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Iterator, Sequence
from datetime import date

import pandas as pd

from selector_app.adapters.online_market_sync import _default_module
from selector_app.market_data.adjustment import CorporateAction
from selector_app.market_data.store import DuckDbMarketDataStore
from selector_app.market_data.store_base import default_database_path
from selector_app.market_data.store_bars import corporate_actions_from_frame

DEFAULT_BATCH_SIZE = 200


def _chunks(items: Sequence[object], size: int) -> Iterator[Sequence[object]]:
    for offset in range(0, len(items), size):
        yield items[offset : offset + size]


def _close_lookup(frame: pd.DataFrame) -> object:
    """按日期索引的收盘价取值器：返回除权日**之前**最后一根 bar 的收盘价。"""

    if frame.empty:
        return lambda _ex_date: None
    series = frame.assign(ts=frame["date"]).set_index("ts")["close"].sort_index()

    def prev_close_for(ex_date: date) -> float | None:
        prior = series[series.index < pd.Timestamp(ex_date)]
        return float(prior.iloc[-1]) if len(prior) else None

    return prev_close_for


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=None)
    parser.add_argument("--write", action="store_true", help="写入事件表（默认只统计不落库）")
    parser.add_argument("--resume", action="store_true", help="跳过事件表里已有的标的")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 只（0 = 全部）")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--interval", type=float, default=0.15, help="请求间隔秒数")
    parser.add_argument("--timeout", type=float, default=25.0)
    args = parser.parse_args(argv)

    store = DuckDbMarketDataStore(args.database or default_database_path())
    instruments = [item for item in store.list_instruments() if (item.bar_count or 0) > 0]
    total = len(instruments)
    if args.limit:
        instruments = instruments[: args.limit]
    print(f"本地有行情的标的 {total} 只，本次处理 {len(instruments)} 只", flush=True)

    done: set[tuple[str, str]] = set()
    if args.resume:
        stored = store.read_corporate_actions_batch(
            [(item.market, item.code) for item in instruments]
        )
        done = {
            (str(row.market).upper(), str(row.code).strip())
            for row in stored[["market", "code"]].drop_duplicates().itertuples(index=False)
        }
        print(f"事件表里已有 {len(done)} 只，跳过", flush=True)

    module = _default_module()
    pending = [item for item in instruments if (item.market, item.code) not in done]
    started = time.monotonic()
    processed = 0
    fetched_actions = 0
    skipped_dates = 0
    failures: list[tuple[str, str, str]] = []
    next_request = started

    for batch in _chunks(pending, args.batch_size):
        try:
            frames = store.read_many_bars(list(batch))
        except Exception as exc:  # noqa: BLE001 - 单批读失败不应终止整轮
            print(f"读取行情批次失败：{exc}", file=sys.stderr, flush=True)
            continue
        grouped = {
            (str(key[0]).upper(), str(key[1]).strip()): group.reset_index(drop=True)
            for key, group in frames.groupby(["market", "code"], sort=False)
        }
        for ref in batch:
            key = (str(ref.market).upper(), str(ref.code).strip())
            frame = grouped.get(key)
            if frame is None or frame.empty:
                continue
            wait = next_request - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            next_request = time.monotonic() + args.interval
            try:
                actions, skipped = module.fetch_corporate_actions(
                    ref.market,
                    ref.code,
                    prev_close_for=_close_lookup(frame),
                    timeout=args.timeout,
                )
            except Exception as exc:  # noqa: BLE001 - 逐只失败只记账不中断
                failures.append((key[0], key[1], str(exc)[:120]))
                processed += 1
                continue
            fetched_actions += len(actions)
            skipped_dates += len(skipped)
            if actions and args.write:
                store.upsert_corporate_actions(actions, provider="eastmoney")
            processed += 1
            if processed % 50 == 0:
                rate = processed / max(time.monotonic() - started, 1e-6)
                remaining = (len(pending) - processed) / max(rate, 1e-6)
                print(
                    f"[{processed}/{len(pending)}] 事件 {fetched_actions} 条"
                    f" 无因子 {skipped_dates} 失败 {len(failures)}"
                    f" {rate:.1f} 只/秒 预计剩余 {remaining / 60:.1f} 分钟",
                    flush=True,
                )

    elapsed = time.monotonic() - started
    print(
        f"\n完成：处理 {processed} 只，事件 {fetched_actions} 条，"
        f"算不出因子 {skipped_dates} 条，失败 {len(failures)} 只，用时 {elapsed / 60:.1f} 分钟"
    )
    if failures:
        print("失败清单（前 30 只）：", file=sys.stderr)
        for market, code, reason in failures[:30]:
            print(f"  {market}{code}  {reason}", file=sys.stderr)
    if args.write:
        print("已写入事件表。指标缓存会在下一次回测时按新的事件版本自动重算。")
    else:
        print("（只统计未落库，加 --write 才写入事件表）")
    return 1 if failures and fetched_actions == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
