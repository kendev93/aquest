"""Orchestration for named baskets of instruments.

一条原则贯穿全部：**组不交易。** 它不产生信号、不模拟调仓、不假设你在什么时候
买了多少。它只回答一件事——这批票从入组那天收盘算起，拿到现在是什么样。想看带
调仓和仓位管理的结果，那是组合回测的职责。

也因此，这里的曲线口径必须和组合回测的基准**能直接对上**：两边都是等权买入持有、
都是起点有价才计入、都是缺失日沿用前收盘价。否则同一批票在两页上会给出不同的
最大回撤，而没人会去查这种差异。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any, cast

import pandas as pd

from selector_app.market_data.adjustment import CorporateAction
from selector_app.market_data.models import InstrumentRef, MarketCode
from selector_app.market_data.store_bars import corporate_actions_from_frame

from .models import (
    GROUP_CURVE_LABEL,
    MARKET_CURVE_LABEL,
    MAX_ITEMS,
    MAX_LOOKBACK_DAYS,
    MIN_TRADING_DAYS,
    NAME_MAX_LENGTH,
    NOTE_MAX_LENGTH,
    SOURCE_LABELS,
    SOURCE_MANUAL,
    GroupMember,
    GroupPerformance,
    normalize_members,
)
from .performance import (
    adjust_closes_for_dividends,
    equal_weight_curve,
    excess_return,
    format_day,
    member_spread,
    pivot_closes,
    stalled_members,
)
from .summary import (
    WINDOW_LABELS,
    GroupSummary,
    normalize_windows,
    summarize_groups,
)

#: 批量查事件时一次带多少只标的。全市场约 5000 只，一次性拼成 OR 条件会让 SQL
#: 又长又慢，拆开走反而更快，单批失败也不会拖垮整批。
_ACTION_BATCH_SIZE = 500


def _actions_by_key(
    store: Any,
    frame: pd.DataFrame,
) -> dict[str, tuple[CorporateAction, ...]]:
    """按 ``市场:代码`` 取出这批标的的除权事件，供 ``adjust_closes_for_dividends`` 用。

    取不到就返回空字典，调用方退化成不复权——少一个校正不该让整个观察页打不开。
    """

    if frame.empty or "market" not in frame.columns or "code" not in frame.columns:
        return {}
    unique = frame[["market", "code"]].drop_duplicates()
    refs = [
        InstrumentRef(
            market=cast(MarketCode, str(row.market)),
            code=str(row.code),
            instrument_type="stock",
        )
        for row in unique.itertuples(index=False)
    ]
    grouped: dict[str, list[CorporateAction]] = {}
    try:
        for offset in range(0, len(refs), _ACTION_BATCH_SIZE):
            batch = store.read_corporate_actions_batch(refs[offset : offset + _ACTION_BATCH_SIZE])
            for action in corporate_actions_from_frame(batch):
                grouped.setdefault(f"{action.market}:{action.code}", []).append(action)
    except Exception:  # noqa: BLE001 - 事件取不到要退化而不是中断整个观察
        return {}
    return {key: tuple(value) for key, value in grouped.items()}


def _adjusted_close_matrix(store: Any, frame: pd.DataFrame) -> pd.DataFrame:
    """读取的长表 → 复权 → 透视成日期 x 标的宽表。

    股票组所有曲线都从这里进口径，保证「股票组概览」「组内表现」与组合回测的
    基准三者数字一致。
    """

    return pivot_closes(adjust_closes_for_dividends(frame, _actions_by_key(store, frame)))


class StockGroupService:
    """Save baskets of instruments and report what they did afterwards."""

    def __init__(self, store: Any) -> None:
        self._store = store

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def list_groups(self) -> list[dict[str, object]]:
        records = self._store.list_stock_groups()
        return _attach_criteria(self._store, [_decorate(record) for record in records])

    def read_group(self, group_id: str) -> dict[str, object] | None:
        record = self._store.read_stock_group(group_id)
        if record is None:
            return None
        return _attach_criteria(self._store, [_decorate(record)])[0]

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def create_group(
        self,
        *,
        name: str,
        items: Sequence[Mapping[str, object]],
        note: str = "",
        source: str = SOURCE_MANUAL,
        source_history_id: str | None = None,
        criteria: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Save a new basket. Raises ``ValueError`` for anything the user can fix."""

        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("分组名称不能为空")
        if len(normalized_name) > NAME_MAX_LENGTH:
            raise ValueError(f"分组名称不能超过 {NAME_MAX_LENGTH} 个字符")
        normalized_note = note.strip()
        if len(normalized_note) > NOTE_MAX_LENGTH:
            raise ValueError(f"分组备注不能超过 {NOTE_MAX_LENGTH} 个字符")
        members = normalize_members(items)
        if not members:
            raise ValueError("分组至少要包含一只有效标的")
        if len(members) > MAX_ITEMS:
            raise ValueError(f"一组最多只能放 {MAX_ITEMS} 只标的")
        checked_criteria = _checked_criteria(criteria)
        record = self._store.save_stock_group(
            uuid.uuid4().hex,
            name=normalized_name,
            items=[member.to_dict() for member in members],
            note=normalized_note,
            source=source,
            source_history_id=source_history_id,
        )
        if checked_criteria is not None:
            self._store.save_stock_group_criteria(
                str(record["group_id"]), criteria=checked_criteria
            )
        return self.read_group(str(record["group_id"])) or _decorate(record)

    def update_group(
        self,
        group_id: str,
        *,
        name: str | None = None,
        note: str | None = None,
        items: Sequence[Mapping[str, object]] | None = None,
        criteria: Mapping[str, object] | None = None,
    ) -> dict[str, object] | None:
        normalized_name = name.strip() if isinstance(name, str) else None
        if normalized_name is not None:
            if not normalized_name:
                raise ValueError("分组名称不能为空")
            if len(normalized_name) > NAME_MAX_LENGTH:
                raise ValueError(f"分组名称不能超过 {NAME_MAX_LENGTH} 个字符")
        normalized_note = note.strip() if isinstance(note, str) else None
        if normalized_note is not None and len(normalized_note) > NOTE_MAX_LENGTH:
            raise ValueError(f"分组备注不能超过 {NOTE_MAX_LENGTH} 个字符")
        normalized_items = None
        if items is not None:
            members = normalize_members(items)
            if not members:
                raise ValueError("分组至少要包含一只有效标的")
            if len(members) > MAX_ITEMS:
                raise ValueError(f"一组最多只能放 {MAX_ITEMS} 只标的")
            normalized_items = [member.to_dict() for member in members]
        checked_criteria = _checked_criteria(criteria)
        record = self._store.update_stock_group(
            group_id,
            name=normalized_name,
            note=normalized_note,
            items=normalized_items,
        )
        if record is None:
            return None
        if checked_criteria is not None:
            self._store.save_stock_group_criteria(group_id, criteria=checked_criteria)
        return self.read_group(str(record["group_id"])) or _decorate(record)

    def remove_members(
        self,
        group_id: str,
        keys: Sequence[tuple[str, str]],
    ) -> dict[str, object] | None:
        """Drop individual members, keeping the rest and their order."""

        record = self._store.read_stock_group(group_id)
        if record is None:
            return None
        members = normalize_members(_items_of(record))
        dropping = {(market.strip().upper(), code.strip()) for market, code in keys}
        kept = [member for member in members if member.key not in dropping]
        if len(kept) == len(members):
            return _decorate(record)
        if not kept:
            raise ValueError("分组至少要保留一只标的")
        updated = self._store.update_stock_group(
            group_id, items=[member.to_dict() for member in kept]
        )
        return None if updated is None else _decorate(updated)

    def delete_group(self, group_id: str) -> bool:
        return bool(self._store.delete_stock_group(group_id))

    # ------------------------------------------------------------------
    # 观察
    # ------------------------------------------------------------------

    def summarize(self, windows: Sequence[int] | None = None) -> dict[str, object]:
        """Folded-row numbers for every group, in one pass over one price matrix.

        这里刻意不复用 ``performance``：那边每组起点不同，得各组各扫一遍全市场；
        折叠态要的是"最近这段时间谁涨谁跌"，同一个窗口下所有组共享一份行情，一
        次读、N 次切片就够了。代价是这两组数字**不同源**，UI 必须标清楚。
        """

        chosen = normalize_windows(windows)
        records = self._store.list_stock_groups_with_items()
        groups: dict[str, tuple[GroupMember, ...]] = {
            str(record.get("group_id")): normalize_members(_items_of(record)) for record in records
        }
        if not groups:
            return _summary_payload(chosen, (), None)
        span = max(chosen)
        # 窗口终点必须落在库里真实有的那一天，和 performance 同一个判据。行情是手动
        # 同步的，最新交易日几乎从不等于今天；用 today 收尾时"近 20 个交易日"会变成
        # 一段行情还没同步到的区间，窗口先是悄悄不满、再旧一点就整块消失——而
        # complete=False 只在"读到了但不够长"时才给，读不到的时候什么都没有。
        end = self._store.latest_trade_date() or date.today()
        # 交易日 → 自然日的换算宁可多读几天：少读了窗口就不满，多读只是多切几行。
        start = end - timedelta(days=span * 7 // 5 + 30)
        market_prices = _adjusted_close_matrix(
            self._store, self._store.read_close_matrix(start_date=start, end_date=end)
        )
        if market_prices.empty:
            return _summary_payload(chosen, (), None)
        summaries = summarize_groups(market_prices, groups, chosen)
        return _summary_payload(chosen, summaries, format_day(market_prices.index[-1]))

    def performance(self, group_id: str) -> GroupPerformance | None:
        """What this group did from the day it was saved until the latest bar."""

        record = self._store.read_stock_group(group_id)
        if record is None:
            return None
        members = normalize_members(_items_of(record))
        notes: list[str] = []
        if not members:
            return GroupPerformance(
                group_id=str(record["group_id"]),
                start_date=None,
                end_date=None,
                trading_days=0,
                group_curve=None,
                market_curve=None,
                excess_return=None,
                spread=None,
                excluded=(),
                stalled=(),
                truncated=False,
                notes=("这一组里没有有效标的",),
            )
        created = _as_date(record.get("created_at"))
        start, truncated = _window_start(created)
        if truncated:
            notes.append(
                f"入组时间早于 {MAX_LOOKBACK_DAYS // 365} 年前，观察窗口已截断到最近 "
                f"{MAX_LOOKBACK_DAYS // 365} 年"
            )
        # 行情是手动同步的，最新交易日几乎从不等于今天。窗口终点必须落在库里真实
        # 有的那一天：用 today 收尾会让"入组之后"这段时间里根本不存在的日期也进
        # 区间，而入组晚于最新行情时整个区间为空——那不是"库里没有行情"，更不是
        # "这些标的在起点没有收盘价"，只是入组之后还没同步过。
        latest = self._store.latest_trade_date()
        end = latest or date.today()
        if created is not None and latest is not None and created > latest:
            return GroupPerformance(
                group_id=str(record["group_id"]),
                start_date=format_day(start),
                end_date=format_day(latest),
                trading_days=0,
                group_curve=None,
                market_curve=None,
                excess_return=None,
                spread=None,
                excluded=(),
                stalled=(),
                truncated=truncated,
                notes=(
                    *notes,
                    f"入组时间是 {format_day(created)}，库里最新行情只到 "
                    f"{format_day(latest)}——入组之后还没有新的交易日可观察，先同步行情再看。",
                ),
            )
        refs = [
            InstrumentRef(
                market=cast(MarketCode, member.market),
                code=member.code,
                instrument_type="stock",
            )
            for member in members
        ]
        market_prices = _adjusted_close_matrix(
            self._store, self._store.read_close_matrix(start_date=start, end_date=end)
        )
        if market_prices.empty:
            return GroupPerformance(
                group_id=str(record["group_id"]),
                start_date=format_day(start),
                end_date=None,
                trading_days=0,
                group_curve=None,
                market_curve=None,
                excess_return=None,
                spread=None,
                excluded=tuple(member.to_dict() for member in members),
                stalled=(),
                truncated=truncated,
                notes=(*notes, "这段时间里库里没有行情，无法计算表现"),
            )
        group_prices = _adjusted_close_matrix(
            self._store, self._store.read_close_matrix(refs, start_date=start, end_date=end)
        ).reindex(market_prices.index)
        group_curve = equal_weight_curve(group_prices, GROUP_CURVE_LABEL)
        market_curve = equal_weight_curve(market_prices, MARKET_CURVE_LABEL)
        excluded = _excluded(group_prices, members)
        if excluded:
            notes.append(f"{len(excluded)} 只标的在起点当天没有收盘价（新股或停牌），未计入曲线")
        if group_curve is None and not excluded:
            notes.append("组内标的在起点都没有收盘价，无法计算表现")
        stalled = stalled_members(group_prices, members)
        if stalled:
            # 不替用户把这笔卖掉：摘牌的票按末次收盘价挂着，是「钱还在里面、变现
            # 不了」的写法。这里只说清楚有几只、从哪天起没了价。
            earliest = min(str(item["last_date"]) for item in stalled)
            notes.append(
                f"{len(stalled)} 只标的自 {earliest} 起不再有价（摘牌或长期停牌）："
                "曲线中这部分沿用末次收盘价，没有按那个价卖出"
            )
        if len(market_prices.index) < MIN_TRADING_DAYS:
            notes.append("入组至今不足两个交易日，暂时看不出涨跌")
        return GroupPerformance(
            group_id=str(record["group_id"]),
            start_date=format_day(market_prices.index[0]),
            end_date=format_day(market_prices.index[-1]),
            trading_days=int(len(market_prices.index)),
            group_curve=group_curve,
            market_curve=market_curve,
            excess_return=excess_return(group_curve, market_curve),
            spread=member_spread(group_prices, members),
            excluded=excluded,
            stalled=stalled,
            truncated=truncated,
            notes=tuple(notes),
        )


def _summary_payload(
    windows: Sequence[int],
    summaries: Sequence[GroupSummary],
    as_of: str | None,
) -> dict[str, object]:
    return {
        "as_of": as_of,
        "windows": list(windows),
        "labels": {
            str(window): WINDOW_LABELS.get(window, f"近 {window} 个交易日") for window in windows
        },
        "groups": [item.to_dict() for item in summaries],
    }


def _checked_criteria(criteria: Mapping[str, object] | None) -> Mapping[str, object] | None:
    """Validate the criteria snapshot, or return ``None`` when there is none.

    没有快照是合法的（手建的组就没有），但**给了却缺摘要**不是——那会让折叠态
    空着一栏，看上去像「忘了存」而不是「这组本来就没有筛选条件」。
    """

    if criteria is None:
        return None
    if not isinstance(criteria, Mapping):
        raise ValueError("筛选条件快照必须是对象")
    if not criteria:
        return None
    summary = criteria.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("筛选条件摘要不能为空")
    return criteria


def _attach_criteria(store: Any, records: list[dict[str, object]]) -> list[dict[str, object]]:
    """Hang each group's criteria snapshot on the record, in one query.

    折叠态要显示「这组是怎么选出来的」，所以列表也得带。但一次查全部而不是每组
    一次查询——组的数量不限，N+1 迟早会在几十个组的时候咬人。
    """

    if not records:
        return records
    if not hasattr(store, "read_stock_group_criteria"):
        # 存储层还没这张表（老库刚升级、或测试里的替身），没有快照就当没有筛选条件。
        for record in records:
            record["criteria"] = None
        return records
    found = store.read_stock_group_criteria([str(record.get("group_id")) for record in records])
    for record in records:
        record["criteria"] = found.get(str(record.get("group_id")))
    return records


def _items_of(record: Mapping[str, object]) -> list[Mapping[str, object]]:
    raw = record.get("items")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def _decorate(record: Mapping[str, object]) -> dict[str, object]:
    """Add display-only fields (来源中文名、成员数) on top of the stored record."""

    payload = dict(record)
    # 列表接口刻意不带成员（一次把几十组的成员全查出来没必要），只带数量；此时
    # 数量是存储层算好的，不能拿空的成员列表再算一遍把它冲成 0。
    if "items" in payload:
        items = _items_of(payload)
        payload["items"] = items
        payload["item_count"] = len(normalize_members(items))
    source = str(payload.get("source") or SOURCE_MANUAL)
    payload["source_label"] = SOURCE_LABELS.get(source, source)
    return payload


def _excluded(
    prices: pd.DataFrame,
    members: Sequence[GroupMember],
) -> tuple[dict[str, object], ...]:
    """入组首日没有收盘价、因此没进曲线的标的。

    这些不能静默丢掉：「这一组到底算了几只」是判断结论可信度的一部分，少了两只
    会让剩下的几只显得更有代表性。
    """

    if prices.empty:
        return tuple(member.to_dict() for member in members)
    first = prices.iloc[0]
    usable = {str(key) for key in prices.columns[first.notna() & (first > 0)]}
    return tuple(
        member.to_dict() for member in members if f"{member.market}:{member.code}" not in usable
    )


def _window_start(created: date | None) -> tuple[date, bool]:
    """观察窗口的起点，以及是否因为太久之前而被截断。"""

    today = date.today()
    earliest = today - timedelta(days=MAX_LOOKBACK_DAYS)
    if created is None:
        return earliest, False
    return (created, False) if created >= earliest else (earliest, True)


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).date()
        except ValueError:
            return None
    return None


__all__ = ["StockGroupService"]
