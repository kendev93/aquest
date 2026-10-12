"""噪声门槛口径回归：逐单元格置换 vs 按交易日块置换，真实 A 股上差多少。

条件研究的噪声门槛（``mining._permutation_max_lifts``）逐单元格打乱标签，而
波段研究和因子检验按交易日块搬动。两者不一致，这个脚本用真实行情量化代价，
回答「要不要统一」。

合成数据上得到的结论是反的（见 mining.py 里的说明），所以必须用真实数据。

用法（需要一个行情库副本，**不要直接连生产库**）::

    docker cp aquest-backend-1:/data/market/market.duckdb /tmp/snap.duckdb
    .venv/bin/python scripts/perm_caliber_regression.py --database /tmp/snap.duckdb

全程只读，不写任何东西。
"""

from __future__ import annotations

import argparse

import duckdb
import numpy as np
import pandas as pd

START = "2021-01-01"
END = "2025-12-31"
STOCK_COUNT = 200
HOLD_DAYS = 5
PERMUTATIONS = 200
BLOCK_SIZE = 20
SEED = 20240918
QUANTILE_COUNT = 19


def load_panel(
    database: str, count: int
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """主板抽样股票的收盘/最高/最低/成交量面板，列为股票、行为交易日。"""

    con = duckdb.connect(database, read_only=True)
    try:
        symbols = [
            row[0]
            for row in con.execute(
                """
                select market || '-' || code
                from daily_bars
                where board = 'main'
                group by 1
                having count(*) > 900
                order by 1
                """
            ).fetchall()
        ]
        rng = np.random.default_rng(SEED)
        picked = sorted(
            str(item) for item in rng.choice(symbols, size=min(count, len(symbols)), replace=False)
        )
        literal = ", ".join(f"'{item}'" for item in picked)
        frame = con.execute(
            f"""
            select trade_date, market || '-' || code as symbol, high, low, close, volume
            from daily_bars
            where market || '-' || code in ({literal})
              and trade_date between date '{START}' and date '{END}'
            order by trade_date, symbol
            """
        ).df()
    finally:
        con.close()
    parts = {name: group.set_index("trade_date") for name, group in frame.groupby("symbol")}
    close = pd.DataFrame({name: part["close"] for name, part in parts.items()}).sort_index()
    columns = close.columns
    index = close.index
    reindexed = {
        field: pd.DataFrame({name: part[field] for name, part in parts.items()}).reindex(
            index=index, columns=columns
        )
        for field in ("high", "low", "volume")
    }
    return close, reindexed["high"], reindexed["low"], reindexed["volume"]


def build_indicators(
    close: pd.DataFrame, high: pd.DataFrame, low: pd.DataFrame, volume: pd.DataFrame
) -> dict[str, np.ndarray]:
    """七个真实指标：动量、反转、趋势、量能、超买超卖、位置、成交额。"""

    delta = close.diff()
    avg_gain = delta.clip(lower=0.0).ewm(alpha=1 / 14, adjust=False).mean()
    avg_loss = (-delta).clip(lower=0.0).ewm(alpha=1 / 14, adjust=False).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        rsi = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    low20 = low.rolling(20).min()
    high20 = high.rolling(20).max()
    spread = (high20 - low20).replace(0.0, np.nan)
    return {
        "20日动量": (close / close.shift(20) - 1.0).to_numpy(dtype=float),
        "5日反转": (close / close.shift(5) - 1.0).to_numpy(dtype=float),
        "均线比": (close.rolling(5).mean() / close.rolling(20).mean() - 1.0).to_numpy(dtype=float),
        "量能比": (volume.rolling(5).mean() / volume.rolling(20).mean()).to_numpy(dtype=float),
        "RSI14": rsi.to_numpy(dtype=float),
        "20日位置": ((close - low20) / spread).to_numpy(dtype=float),
        "成交额对数": np.log(volume.replace(0.0, np.nan)).to_numpy(dtype=float),
    }


def daily_rate(outcomes: np.ndarray, mask: np.ndarray) -> float:
    """按交易日等权的胜率：先算每天的胜率再平均（对应 _grouped_outcome_rate）。"""

    valid = mask & ~np.isnan(outcomes)
    sums = np.where(valid, outcomes, 0.0).sum(axis=1)
    counts = valid.sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        per_day = np.where(counts > 0, sums / np.where(counts > 0, counts, 1), np.nan)
    return float(np.nanmean(per_day)) if np.isfinite(per_day).any() else float("nan")


def block_order(day_count: int, rng: np.random.Generator) -> np.ndarray:
    """把日历切成连续块再整体重排（对齐 swing 的 block_permutations）。"""

    size = max(1, BLOCK_SIZE)
    if day_count // size < 2:
        size = max(1, day_count // 2)
    blocks = [np.arange(a, min(a + size, day_count)) for a in range(0, day_count, size)]
    return np.concatenate([blocks[index] for index in rng.permutation(len(blocks))])


def same_day_correlation(outcomes: np.ndarray, rng: np.random.Generator) -> float:
    """同一天入场的两只股票，盈亏到底有多相关。"""

    stocks = outcomes.shape[1]
    values: list[float] = []
    for _ in range(400):
        left, right = int(rng.integers(0, stocks)), int(rng.integers(0, stocks))
        if left == right:
            continue
        both = ~np.isnan(outcomes[:, left]) & ~np.isnan(outcomes[:, right])
        if int(both.sum()) < 100:
            continue
        first, second = outcomes[both, left], outcomes[both, right]
        if first.std() > 0 and second.std() > 0:
            values.append(float(np.corrcoef(first, second)[0, 1]))
    return float(np.mean(values))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, help="行情库副本路径（只读打开）")
    parser.add_argument("--stocks", type=int, default=STOCK_COUNT)
    parser.add_argument("--permutations", type=int, default=PERMUTATIONS)
    args = parser.parse_args()

    close, high, low, volume = load_panel(args.database, args.stocks)
    indicators = build_indicators(close, high, low, volume)

    forward = close.shift(-HOLD_DAYS) / close - 1.0
    outcomes = np.where(forward.isna(), np.nan, (forward > 0).astype(float))
    days, stocks = outcomes.shape

    rng = np.random.default_rng(SEED)
    pair_corr = same_day_correlation(outcomes, rng)
    per_day = np.nanmean(outcomes, axis=1)
    series = per_day[np.isfinite(per_day)]
    autocorr = (
        float(np.corrcoef(series[:-1], series[1:])[0, 1]) if series.size > 3 else float("nan")
    )

    usable = ~np.isnan(outcomes)
    masks: list[np.ndarray] = []
    labels: list[str] = []
    for name, values in indicators.items():
        for quantile in np.linspace(0.05, 0.95, QUANTILE_COUNT):
            cut = float(np.nanquantile(values, quantile))
            for operator, mask in ((">", values > cut), ("<", values < cut)):
                keep = mask & usable
                if int(keep.sum()) < 200:
                    continue
                masks.append(keep)
                labels.append(f"{name} {operator} {cut:.4g}")
    base_rate = daily_rate(outcomes, usable)
    real_lifts = np.array([daily_rate(outcomes, mask) / base_rate for mask in masks])

    perm_rng = np.random.default_rng(SEED + 1)
    flat = outcomes.ravel()
    live = ~np.isnan(flat)
    cell_max: list[float] = []
    block_max: list[float] = []
    for _ in range(args.permutations):
        for mode, sink in (("cell", cell_max), ("block", block_max)):
            if mode == "cell":
                moved = flat.copy()
                moved[live] = perm_rng.permutation(flat[live])
                moved = moved.reshape(outcomes.shape)
            else:
                moved = outcomes[block_order(days, perm_rng)]
            moved_base = daily_rate(moved, usable)
            if not moved_base > 1e-12:
                continue
            best = 0.0
            for mask in masks:
                rate = daily_rate(moved, mask)
                if np.isfinite(rate):
                    best = max(best, rate / moved_base)
            sink.append(best)

    cell_q = float(np.quantile(cell_max, 0.95))
    block_q = float(np.quantile(block_max, 0.95))
    print(f"面板: {days} 个交易日 × {stocks} 只主板股票，持有 {HOLD_DAYS} 日")
    print(f"整体胜率: {base_rate:.3f}，条件数: {len(masks)}，置换次数: {args.permutations}")
    print()
    print("【真实结构】")
    print(f"  同一天入场的两只股票，盈亏相关: {pair_corr:.3f}")
    print(f"  日度胜率序列的相邻日自相关:    {autocorr:.3f}")
    print(f"  ⇒ 名义样本 {int(usable.sum())} 个，独立单位实际只有 {days} 天")
    print()
    print("【两种置换的 95% 噪声门槛】")
    print(f"  逐单元格（当前实现）: {cell_q:.3f}")
    print(f"  按日期块:            {block_q:.3f}")
    print(f"  差异: {(block_q / cell_q - 1) * 100:+.1f}%")
    print()
    print("【判定结果】")
    print(f"  真实 lift 最高的条件: {labels[int(np.argmax(real_lifts))]} = {real_lifts.max():.3f}")
    print(f"  逐单元格判为显著: {int((real_lifts >= cell_q).sum())} / {len(masks)}")
    print(f"  按日期块判为显著: {int((real_lifts >= block_q).sum())} / {len(masks)}")


if __name__ == "__main__":
    main()
