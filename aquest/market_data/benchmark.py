"""模拟盘对比基准：510300（沪深300ETF）前复权日线。

基准不进个股行情表：ETF 价格 3 位小数，和按 0.01 系数解析的个股日线不是
同一套口径，混进去会污染选股范围与指标。这里只存收盘价，全量覆盖写入；
前复权按最新除权基准重算，整表替换才不会留下两个基准拼接的历史。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date
from typing import Protocol

from aquest.market_data.store import DuckDbMarketDataStore

#: 前复权含分红再投资，口径接近沪深300全收益，仍扣 ETF 管理费与跟踪误差。
BENCHMARK_SYMBOL = "510300"
BENCHMARK_LABEL = "沪深300ETF 510300 · 前复权"
BENCHMARK_START_DATE = date(2015, 1, 1)
BENCHMARK_FETCH_TIMEOUT_SECONDS = 30.0


class BenchmarkFetcher(Protocol):
    def __call__(
        self,
        start_date: date,
        end_date: date,
        *,
        timeout: float = BENCHMARK_FETCH_TIMEOUT_SECONDS,
    ) -> Sequence[tuple[date, float]]: ...


class BenchmarkSyncError(RuntimeError):
    """基准取不到或落库失败；调用方决定是报错还是只记日志。"""


def refresh_benchmark_bars(
    store: DuckDbMarketDataStore,
    fetcher: BenchmarkFetcher,
    *,
    today: date,
    timeout: float = BENCHMARK_FETCH_TIMEOUT_SECONDS,
) -> int:
    """拉取 2015 至今的前复权日线并整表替换；返回写入根数。"""

    try:
        fetched = fetcher(BENCHMARK_START_DATE, today, timeout=timeout)
    except Exception as exc:
        raise BenchmarkSyncError(f"基准行情下载失败：{exc}") from exc
    closes: dict[date, float] = {}
    for bar_date, close in fetched:
        if not isinstance(bar_date, date) or not BENCHMARK_START_DATE <= bar_date <= today:
            continue
        value = float(close)
        if not math.isfinite(value) or value <= 0:
            continue
        closes[bar_date] = value
    if not closes:
        raise BenchmarkSyncError("基准行情没有返回有效日线")
    rows = sorted(closes.items())
    try:
        return store.replace_benchmark_bars(BENCHMARK_SYMBOL, rows)
    except Exception as exc:
        raise BenchmarkSyncError(f"基准行情写入失败：{exc}") from exc
