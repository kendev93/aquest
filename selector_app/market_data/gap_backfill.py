"""从行情自身的除权跳空反推缺失的分红送转事件。

分红送配接口会漏记录：它覆盖的是"权益分派"，而重组、股改等导致的股本扩张
不在其中。漏掉的那一次会在 raw 价格上留下一个真实的跳空，被指标当成暴跌。

这些事件能补出来，因为"这次到底有没有除权"**不是猜测，而是市场自身的硬约束
推出来的**：主板跌停就是 -10.00%，所以跌幅超过 -10.5% 的 bar 不可能是市场
波动，只能发生过股本变化。真正需要估计的只有"除权幅度"，而除权日的**开盘价**
是它的最佳单一估计——除权参考价本就是按开盘集合竞价的基准算出来的。

在 5285 个已有真实事件上实测（这些事件的因子是已知的，可以当标准答案）：

===============  ==============  ===============  ==============
估计口径           中位误差          90 分位          误差 >5% 占比
===============  ==============  ===============  ==============
**开盘价**         **0.720%**     **2.869%**      **3.4%**
收盘价             2.162%          7.784%         20.3%
OHLC 均值          1.332%          4.488%          7.5%
VWAP（额/量）      1.751%          5.663%         44.5%
===============  ==============  ===============  ==============

所以只有开盘价口径够用，收盘价差三倍。

**跨停牌的跳空一律不推断。** 复牌后的累计跌幅不受涨跌幅约束，把 600733
"停牌 238 天后 -82%" 当成除权会写出荒谬的因子。这类跳空既不推断、也不改数据，
只在报告里列出——它们本来就该保留复牌跌幅，复权不该动它们。
"""

from __future__ import annotations

import bisect
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol, cast

import pandas as pd

from .adjustment import CorporateAction

#: 推断因子的上限。10 转 190 相当于股本扩大 20 倍，A 股历史上没有这种操作；
#: 越过它说明这次跳空的成因不是送转（更可能是数据错误或我方识别错了），
#: 宁可留一个未复权的跳空，也不能写一条错因子去静默污染全部历史指标。
MAX_INFERRED_FACTOR = 20.0

#: 推断事件的类型与来源标记。刻意与 eastmoney 拉取的事件区分开：这些因子是
#: 估算的，审计时要能一眼看出哪些不是原始数据。
INFERRED_ACTION_TYPE = "inferred"
INFERRED_SOURCE = "inferred_from_open"
INFERRED_PROVIDER = "derived_from_open"


class _ActionStore(Protocol):
    def read_price_gap_candidates(self) -> pd.DataFrame: ...

    def read_authoritative_trading_dates(
        self,
        start_date: date,
        end_date: date,
    ) -> list[date]: ...

    def upsert_corporate_actions(
        self,
        actions: Sequence[CorporateAction],
        *,
        source: str = "online",
        provider: str = "eastmoney",
    ) -> int: ...


@dataclass(frozen=True)
class SkippedGap:
    """检出但不能安全补成事件的跳空，附不能补的原因。

    这些跳空会被原样留在行情里。对绝大多数情况这是正确的——复牌跌幅不是除权，
    复权不该去"修正"它。留一份清单是为了让"某个除权日仍有跳空"这种问题能
    被看见，而不是靠指标异常事后去猜。

    ``reason`` 是**固定分类**（聚合用的键），具体数值放 ``detail``。早期版本把
    "跨越 128 个交易日"直接当 reason，结果每个天数都成了独立键，聚合出来的
    分布毫无意义。
    """

    #: 前后两根 bar 之间夹着交易日 —— 停牌复牌的跌幅是跨期累计的，不适用跌停幅度。
    REASON_HALTED = "halted"
    #: 开盘价不低于前收盘，算不出大于 1 的因子 —— 多半是盘中跌停而非除权。
    REASON_IMPLAUSIBLE = "implausible_factor"

    market: str
    code: str
    trade_date: date
    close: float
    drop_pct: float
    reason: str
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {
            "market": self.market,
            "code": self.code,
            "trade_date": self.trade_date.isoformat(),
            "close": self.close,
            "drop_pct": self.drop_pct,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class InferredGap:
    """一条被判定为除权、并已用开盘价反推定因子的事件。

    刻意留清单：因子是估算的，审计时必须能一眼看出"哪些事件不是原始数据"，
    以及当时用了哪个价格估的。
    """

    market: str
    code: str
    ex_date: date
    prev_close: float
    open_price: float
    factor_ratio: float

    def to_dict(self) -> dict[str, object]:
        return {
            "market": self.market,
            "code": self.code,
            "ex_date": self.ex_date.isoformat(),
            "prev_close": self.prev_close,
            "open": self.open_price,
            "factor_ratio": self.factor_ratio,
        }


@dataclass(frozen=True)
class BackfillReport:
    """一次补全的结果。"""

    inferred: int
    inferred_symbols: int
    written: int
    inferred_detail: tuple[InferredGap, ...]
    skipped: tuple[SkippedGap, ...]

    def to_dict(self) -> dict[str, object]:
        skipped_by_reason: dict[str, int] = {}
        for item in self.skipped:
            skipped_by_reason[item.reason] = skipped_by_reason.get(item.reason, 0) + 1
        return {
            "inferred": self.inferred,
            "inferred_symbols": self.inferred_symbols,
            "written": self.written,
            "inferred_detail": [item.to_dict() for item in self.inferred_detail],
            "skipped": len(self.skipped),
            "skipped_by_reason": skipped_by_reason,
            "skipped_detail": [item.to_dict() for item in self.skipped],
        }


def _as_date(value: object) -> date:
    """收敛成 ``datetime.date``。

    store 层把 ``trade_date`` 规范成 ``date`` 列并转成 datetime64，落到这里就是
    ``pandas.Timestamp``。它虽然是 ``date`` 的子类，却不能和纯 ``date`` 混在同一个
    有序容器里比较（``bisect`` 会抛 ``TypeError``），而交易日历那边是纯 ``date``，
    所以这里必须显式收敛，不靠子类蒙混过关。
    """

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _trading_days_between(
    earlier: date,
    later: date,
    calendar: Sequence[date],
) -> int:
    """两个日期之间夹着多少个交易日（不含两端）。

    用来判断"前后两根 bar 是否紧邻交易日"。停牌跨越的 bar 之间的返回值大于 0，
    那时跌幅是跨期累计的，不能拿跌停幅度去套。
    """

    if not calendar:
        return 0
    left = bisect.bisect_right(calendar, earlier)
    right = bisect.bisect_left(calendar, later)
    return max(right - left, 0)


def _require_calendar_coverage(
    candidates: pd.DataFrame,
    calendar: Sequence[date],
) -> None:
    """要求交易日历完整覆盖候选日期范围，否则直接报错。

    这个检查挡的是一个很隐蔽的失效：:func:`_trading_days_between` 在范围之外
    只会返回 0，于是**所有**跨停牌的跳空都会被判成"紧邻交易日"，全部按除权
    写入事件表 —— 恰好是本模块存在的意义所反对的那件事。缺日历时的正确反应
    是拒绝处理，不是猜。
    """

    if not calendar:
        raise ValueError("交易日历为空，无法判定停牌跨越")
    first = _as_date(candidates["date"].min())
    last = _as_date(candidates["date"].max())
    if calendar[0] > first or calendar[-1] < last:
        raise ValueError(
            "交易日历未覆盖候选日期范围："
            f"候选 {first} ~ {last}，日历 {calendar[0]} ~ {calendar[-1]}"
        )


def infer_corporate_actions(
    candidates: pd.DataFrame,
    calendar: Sequence[date],
) -> tuple[list[CorporateAction], list[SkippedGap]]:
    """把跳空候选分成"可推断"与"不可推断"两堆。

    可推断的那堆用 ``factor = 前收盘价 / 当日开盘价`` 定因子，并折算成
    ``share_ratio = factor - 1``、``cash_per_share = 0``：这样
    :func:`~selector_app.market_data.adjustment.compute_factor_ratio` 能从
    存进去的字段原样重算出同一个因子，事件表的自洽校验不会失败。
    """

    if candidates.empty:
        return [], []
    _require_calendar_coverage(candidates, calendar)
    actions: list[CorporateAction] = []
    skipped: list[SkippedGap] = []
    for row in candidates.itertuples(index=False):
        trade_date = _as_date(row.date)
        prev_date = _as_date(row.prev_date)
        close = float(cast(float, row.close))
        open_price = float(cast(float, row.open))
        prev_close = float(cast(float, row.prev_close))
        drop_pct = (close / prev_close - 1) * 100 if prev_close else 0.0

        missed = _trading_days_between(prev_date, trade_date, calendar)
        if missed:
            skipped.append(
                SkippedGap(
                    market=str(row.market),
                    code=str(row.code),
                    trade_date=trade_date,
                    close=close,
                    drop_pct=drop_pct,
                    reason=SkippedGap.REASON_HALTED,
                    detail=f"跨越 {missed} 个交易日",
                )
            )
            continue

        factor_ratio = prev_close / open_price
        if not (1.0 < factor_ratio <= MAX_INFERRED_FACTOR):
            skipped.append(
                SkippedGap(
                    market=str(row.market),
                    code=str(row.code),
                    trade_date=trade_date,
                    close=close,
                    drop_pct=drop_pct,
                    reason=SkippedGap.REASON_IMPLAUSIBLE,
                    detail=f"开盘价隐含因子 {factor_ratio:.4f} 不可用",
                )
            )
            continue

        actions.append(
            CorporateAction(
                market=str(row.market),
                code=str(row.code),
                ex_date=trade_date,
                share_ratio=factor_ratio - 1.0,
                cash_per_share=0.0,
                factor_ratio=factor_ratio,
                action_type=INFERRED_ACTION_TYPE,
            )
        )
    return actions, skipped


def backfill_inferred_actions(
    store: _ActionStore,
    *,
    write: bool = True,
) -> BackfillReport:
    """扫描全市场并补齐缺失的除权事件。

    ``write=False`` 只做体检、不落库，用来先看看能补出多少、哪些补不了。
    """

    candidates = store.read_price_gap_candidates()
    if candidates.empty:
        return BackfillReport(0, 0, 0, (), ())
    calendar_reader = getattr(store, "read_authoritative_trading_dates", None)
    if not callable(calendar_reader):
        raise ValueError(
            "市场数据存储没有可验证的权威交易日历覆盖 API；"
            "需要持久化 provider 请求的完整覆盖区间，并按来源读取覆盖期内交易日"
        )
    calendar_start = min(_as_date(item) for item in candidates["prev_date"])
    calendar_end = max(_as_date(item) for item in candidates["date"])
    calendar = calendar_reader(calendar_start, calendar_end)
    actions, skipped = infer_corporate_actions(candidates, calendar)
    detail: list[InferredGap] = []
    for item in candidates.itertuples(index=False):
        trade_date = _as_date(item.date)
        prev_close = float(cast(float, item.prev_close))
        open_price = float(cast(float, item.open))
        if any(
            action.ex_date == trade_date
            and action.market == str(item.market)
            and action.code == str(item.code)
            for action in actions
        ):
            detail.append(
                InferredGap(
                    market=str(item.market),
                    code=str(item.code),
                    ex_date=trade_date,
                    prev_close=prev_close,
                    open_price=open_price,
                    factor_ratio=prev_close / open_price,
                )
            )
    symbols = {(item.market, item.code) for item in actions}
    written = (
        store.upsert_corporate_actions(
            actions,
            source=INFERRED_SOURCE,
            provider=INFERRED_PROVIDER,
        )
        if write and actions
        else 0
    )
    return BackfillReport(
        inferred=len(actions),
        inferred_symbols=len(symbols),
        written=written,
        inferred_detail=tuple(detail),
        skipped=tuple(skipped),
    )


__all__ = [
    "BackfillReport",
    "INFERRED_ACTION_TYPE",
    "INFERRED_PROVIDER",
    "INFERRED_SOURCE",
    "MAX_INFERRED_FACTOR",
    "InferredGap",
    "SkippedGap",
    "backfill_inferred_actions",
    "infer_corporate_actions",
]
