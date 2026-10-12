"""由分红送转事件在本地计算复权序列。

为什么不直接存后复权价
----------------------
后复权因子从上市首日累乘，**每发生一次分红除权，全部历史 bar 的价格都会
整体改变**。库里存的 1990 年那批价格与明年重新下载的同一区间并不是同一组
数字，回测结果无法长期对齐。前复权只是漂移方向相反，同样不可复现。

价格一旦被写成"结果"而不是"事实"，就再也回不到当时的样子。raw 是已经发生过
的事实，永不变化，因此能和本项目的增量同步（只回补 ``_INCREMENTAL_OVERLAP_DAYS``
天）根本兼容 —— 复权只在这里、在计算期完成，不落盘。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, cast

import numpy as np

__all__ = [
    "ADJUSTED_PRICE_COLUMNS",
    "ADJUST_VERSION_COLUMN",
    "AdjustmentMismatch",
    "CorporateAction",
    "apply_adjustment",
    "build_factor_series",
    "compute_factor_ratio",
    "events_version",
    "frame_adjust_version",
    "mark_adjusted",
    "verify_against_provider",
]


#: 复权事件版本标记列。它和 ``price_basis`` 是两回事：``price_basis`` 说"这批价格
#: 是什么口径"，``adjust_version`` 说"这批价格按哪一版事件做过除权校正"。
ADJUST_VERSION_COLUMN = "adjust_version"


#: 复权只作用于价格。``volume`` / ``amount`` 是**事实量** —— 分红送转不改变
#: 当日实际成交了多少股、多少元，按复权价缩放它们会凭空造出成交量。
ADJUSTED_PRICE_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close")

#: ``date.toordinal()``（公元 1 年起）与 ``datetime64[D].astype(int64)``
#: （1970-01-01 起）差一个固定偏移。统一折算到 1970 基准，pandas / numpy 输入
#: 就走完全量化的向量化路径，只有 date 对象才需要逐个转换。
_ORDINAL_EPOCH_OFFSET = date(1970, 1, 1).toordinal()

_NEUTRAL_FACTOR = 1.0


def _is_finite(value: object) -> bool:
    """有限数判定，对非数字输入返回 False 而不是抛异常。"""

    try:
        return bool(np.isfinite(float(cast(float, value))))
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class CorporateAction:
    """一只股票在某个除权除息日的一次股本/权益变动。

    ``share_ratio`` 是**每股**股本扩张倍数：10 送 3 记 ``0.3``，10 转 5 记 ``0.5``，
    送转同时发生时记两者之和。``cash_per_share`` 是**每股**税前派息（元）：
    10 派 4.2 元记 ``0.42``。

    ``factor_ratio`` 是这一次事件带来的价格因子增量，预先算好存下来，而不是等到
    计算因子那天再去查前收盘价。两个好处：一是因子计算不依赖日线是否含有前一根
    bar（停牌、上市首日都会缺），二是写入时就完成了一次校验 —— 算不出因子的事件
    干脆不写，宁可缺一条（校验工具会报出来，表现为某个除权日仍有跳空），也不能写
    一条错因子去静默污染全部历史指标。
    """

    market: str
    code: str
    ex_date: date
    share_ratio: float
    cash_per_share: float
    factor_ratio: float
    action_type: str

    def __post_init__(self) -> None:
        if not _is_finite(self.share_ratio) or self.share_ratio < 0:
            raise ValueError("股本扩张倍数必须是大于等于 0 的有限数字")
        if not _is_finite(self.cash_per_share) or self.cash_per_share < 0:
            raise ValueError("每股派息必须是大于等于 0 的有限数字")
        if not _is_finite(self.factor_ratio) or self.factor_ratio <= 0:
            raise ValueError("复权因子增量必须是大于 0 的有限数字")

    @property
    def is_neutral(self) -> bool:
        """没有送转也没有派息的事件对价格没有任何影响。"""

        return self.share_ratio == 0 and self.cash_per_share == 0


@dataclass(frozen=True)
class AdjustmentMismatch:
    """自算因子与 provider 反推因子不一致的一个事件。

    差异点直接指向缺失的事件类别：集中在配股 / 回购注销这类非现金事件上。
    ``observed is None`` 表示 provider 那边算不出因子，本身也是需要看见的信号。
    """

    ex_date: date
    expected: float
    observed: float | None
    drift: float | None


def compute_factor_ratio(
    *,
    prev_close: float,
    share_ratio: float,
    cash_per_share: float,
) -> float:
    """按除权除息公式算单次因子增量。

        除权参考价 = (前收盘价 - 每股派息) / (1 + 每股股本扩张倍数)
        factor_ratio = 前收盘价 / 除权参考价

    这是**整个复权体系里唯一需要精确的地方**，其余全是乘法累乘。写错这里后面
    所有指标都会错，所以单独抽成纯函数，便于逐项对照交易所规则做测试。
    """

    base = float(prev_close) - float(cash_per_share)
    if base <= 0:
        raise ValueError("除权前收盘价减去每股派息后必须为正")
    reference = base / (1.0 + float(share_ratio))
    if reference <= 0:
        raise ValueError("除权参考价必须为正")
    return float(prev_close) / reference


def _to_ordinal(value: date) -> int:
    """单个日期折算成 1970 基准的天数（与 :func:`_to_ordinals` 同一基准）。"""

    return value.toordinal() - _ORDINAL_EPOCH_OFFSET


def _to_ordinals(dates: object) -> np.ndarray:
    """把日期序列折算成 1970 基准的 int64 天数。

    全市场 5000+ 只标的、每只 1000+ 根 bar 的规模下，能不能走完全向量化的路径
    决定了因子计算是秒级还是分钟级，所以 numpy / pandas 输入一律走
    ``datetime64[D]``，只有 ``date`` 对象才逐个转换。
    """

    if isinstance(dates, np.ndarray):
        if np.issubdtype(dates.dtype, np.datetime64):
            return dates.astype("datetime64[D]").astype(np.int64)
        if np.issubdtype(dates.dtype, np.integer):
            return dates.astype(np.int64)
    to_numpy = getattr(dates, "to_numpy", None)
    if callable(to_numpy):
        try:
            return _to_ordinals(to_numpy())
        except (TypeError, ValueError):
            pass
    return np.array(
        [item.toordinal() - _ORDINAL_EPOCH_OFFSET for item in cast(Sequence[date], dates)],
        dtype=np.int64,
    )


def build_factor_series(dates: object, actions: Sequence[CorporateAction]) -> np.ndarray:
    """返回与 ``dates`` 等长的分段常数复权因子数组。

    因子从**除权当日**起生效（``side="left"``），而不是次日。这不是细节：除权日的
    raw 价格已经是除权**之后**的价格，只有当天就乘上因子才能把跳空抹平。若从次一
    bar 起算，除权日那个断崖会原封不动留在复权序列里，复权就白做了。

    事件日不在交易日列表里时（停牌），``searchsorted`` 自然落到下一个交易日，
    等价于"下一个交易日起生效"。

    复杂度 O(n + m)（n=bar 数，m=事件数），不做 Python 逐 bar 循环。
    """

    day_values = _to_ordinals(dates)
    count = int(day_values.shape[0])
    if count == 0:
        return np.empty(0, dtype=float)
    if not actions:
        return np.full(count, _NEUTRAL_FACTOR, dtype=float)

    steps: list[tuple[int, float]] = []
    cumulative = _NEUTRAL_FACTOR
    for action in sorted(actions, key=lambda item: item.ex_date):
        if action.factor_ratio == _NEUTRAL_FACTOR:
            continue
        start = int(np.searchsorted(day_values, _to_ordinal(action.ex_date), side="left"))
        if start >= count:
            # 事件落在样本区间之外：只影响更晚的 bar，对本区间无作用。
            continue
        cumulative *= action.factor_ratio
        steps.append((start, cumulative))
    if not steps:
        return np.full(count, _NEUTRAL_FACTOR, dtype=float)

    factors = np.empty(count, dtype=float)
    filled = 0
    current = _NEUTRAL_FACTOR
    for start, value in steps:
        if start > filled:
            factors[filled:start] = current
            filled = start
        current = value
    factors[filled:] = current
    return factors


def apply_adjustment(frame: Any, factors: np.ndarray, *, version: str = "") -> Any:
    """返回复权后的 frame：只缩放 OHLC，``volume`` / ``amount`` 原样保留。

    ``version`` 是事件表的内容哈希，会作为 ``adjust_version`` 列一起写进 frame。
    这一列不是装饰：指标缓存的修订号优先取行情的标的级版本号，而事件补录或修正
    **一行行情都不会变**，行情版本号对此完全无感。少了这一列，复权口径和 raw 口径
    的指标会共用同一个缓存 key 互相覆盖 —— 而且因为两边算出来的都是"合法"的
    指标值，错缓存不会以任何形式暴露出来。

    刻意不落盘、也不缓存复权价格 —— 缓存一份复权价就等于把"结果"存了下来，
    事件表一更新那份缓存就成了脏数据，而且没有任何东西能察觉。
    """

    if len(frame) != len(factors):
        raise ValueError("复权因子长度必须与行情行数一致")
    columns = [name for name in ADJUSTED_PRICE_COLUMNS if name in frame.columns]
    if not columns or len(frame) == 0:
        return frame.copy(deep=False)
    scaled = {name: frame[name].to_numpy(dtype=float) * factors for name in columns}
    if version:
        scaled["adjust_version"] = version
    return frame.assign(**scaled)


def mark_adjusted(
    frame: Any, actions: Sequence[CorporateAction], *, date_column: str = "date"
) -> Any:
    """复权并打上事件版本标记；没有事件时原样返回。

    指标计算入口统一走这里，"复权了没有"和"复权依据是什么"变成 frame 自带的
    信息，缓存层不需要知道复权这回事。
    """

    if not actions or frame.empty:
        return frame
    return apply_adjustment(
        frame,
        build_factor_series(frame[date_column], actions),
        version=events_version(actions),
    )


def frame_adjust_version(frame: Any) -> str:
    """读出 frame 上的事件版本标记；没有标记返回空串。

    同一序列混用多个版本说明调用方把两套口径混进了同一个缓存条目 —— 那必然是错的，
    直接拒绝而不是挑一个。
    """

    if "adjust_version" not in frame.columns:
        return ""
    values = tuple(frame["adjust_version"].dropna().astype(str).unique())
    if len(values) > 1:
        raise ValueError("同一行情序列不能混用多个复权版本")
    return values[0] if values else ""


def verify_against_provider(
    raw: Any,
    provider_adjusted: Any,
    actions: Sequence[CorporateAction],
    *,
    tolerance: float = 1e-4,
) -> list[AdjustmentMismatch]:
    """用 provider 后复权反推隐含因子，与自算因子逐事件比对。

    做法：按 ``trade_date`` 对齐两边，在每个事件日之后的第一根 bar 上取隐含因子，
    与前一根 bar 的隐含因子相除，得到 provider 眼中的"单次因子"，再与
    ``action.factor_ratio`` 比对。不一致的事件原样返回。

    provider 的复权价**只当校验材料**，绝不作为数据源：它不可复现，但能独立算出
    一个因子序列，正好用来验证自算口径有没有漏掉配股、回购注销这类非现金事件。
    """

    if tolerance <= 0:
        raise ValueError("容差必须大于 0")
    if not actions:
        return []
    required = {"trade_date", "close"}
    for label, frame in (("本地行情", raw), ("provider 复权行情", provider_adjusted)):
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(f"{label}缺少列: {sorted(missing)}")

    merged = raw[["trade_date", "close"]].merge(
        provider_adjusted[["trade_date", "close"]],
        on="trade_date",
        how="inner",
        suffixes=("_raw", "_provider"),
    )
    if merged.empty:
        raise ValueError("本地行情与 provider 行情没有可对齐的交易日")
    merged = merged.sort_values("trade_date")
    raw_close = merged["close_raw"].to_numpy(dtype=float)
    provider_close = merged["close_provider"].to_numpy(dtype=float)
    if np.any(raw_close == 0):
        raise ValueError("行情里存在 0 收盘价，无法反推因子")

    day_values = _to_ordinals(merged["trade_date"].to_numpy())
    implicit = provider_close / raw_close
    mismatches: list[AdjustmentMismatch] = []
    for action in sorted(actions, key=lambda item: item.ex_date):
        index = int(np.searchsorted(day_values, _to_ordinal(action.ex_date), side="left"))
        if index >= len(day_values) or index == 0:
            # 事件落在样本外，或除权日就是样本首日（没有"前一根"可比）。
            continue
        observed = float(implicit[index] / implicit[index - 1])
        if not _is_finite(observed) or observed <= 0:
            mismatches.append(
                AdjustmentMismatch(
                    ex_date=action.ex_date,
                    expected=action.factor_ratio,
                    observed=None,
                    drift=None,
                )
            )
            continue
        drift = observed / action.factor_ratio - 1.0
        if abs(drift) > tolerance:
            mismatches.append(
                AdjustmentMismatch(
                    ex_date=action.ex_date,
                    expected=action.factor_ratio,
                    observed=observed,
                    drift=drift,
                )
            )
    return mismatches


def events_version(actions: Sequence[CorporateAction]) -> str:
    """事件表的内容哈希，用作指标缓存的失效依据。

    事件是**标的级**数据，不该混进逐行帧哈希里，所以单独算一个版本号：事件补录
    或修正后版本变化，指标缓存自动失效重算；没变化则完全复用。
    """

    digest = hashlib.sha256()
    for action in sorted(actions, key=lambda item: (item.market, item.code, item.ex_date)):
        digest.update(
            f"{action.market}|{action.code}|{action.ex_date.isoformat()}|"
            f"{action.share_ratio!r}|{action.cash_per_share!r}|{action.factor_ratio!r}\n".encode()
        )
    return digest.hexdigest()
