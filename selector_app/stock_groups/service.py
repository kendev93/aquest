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

from selector_app.market_data.models import InstrumentRef, MarketCode

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
    equal_weight_curve,
    excess_return,
    member_spread,
    pivot_closes,
)


class StockGroupService:
    """Save baskets of instruments and report what they did afterwards."""

    def __init__(self, store: Any) -> None:
        self._store = store

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def list_groups(self) -> list[dict[str, object]]:
        records = self._store.list_stock_groups()
        return [_decorate(record) for record in records]

    def read_group(self, group_id: str) -> dict[str, object] | None:
        record = self._store.read_stock_group(group_id)
        return None if record is None else _decorate(record)

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
        record = self._store.save_stock_group(
            uuid.uuid4().hex,
            name=normalized_name,
            items=[member.to_dict() for member in members],
            note=normalized_note,
            source=source,
            source_history_id=source_history_id,
        )
        return _decorate(record)

    def update_group(
        self,
        group_id: str,
        *,
        name: str | None = None,
        note: str | None = None,
        items: Sequence[Mapping[str, object]] | None = None,
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
        record = self._store.update_stock_group(
            group_id,
            name=normalized_name,
            note=normalized_note,
            items=normalized_items,
        )
        return None if record is None else _decorate(record)

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
        refs = [
            InstrumentRef(
                market=cast(MarketCode, member.market),
                code=member.code,
                instrument_type="stock",
            )
            for member in members
        ]
        market_prices = pivot_closes(
            self._store.read_close_matrix(start_date=start, end_date=date.today())
        )
        if market_prices.empty:
            return GroupPerformance(
                group_id=str(record["group_id"]),
                start_date=_format_day(start),
                end_date=None,
                trading_days=0,
                group_curve=None,
                market_curve=None,
                excess_return=None,
                spread=None,
                excluded=tuple(member.to_dict() for member in members),
                truncated=truncated,
                notes=(*notes, "这段时间里库里没有行情，无法计算表现"),
            )
        group_prices = pivot_closes(
            self._store.read_close_matrix(refs, start_date=start, end_date=date.today())
        ).reindex(market_prices.index)
        group_curve = equal_weight_curve(group_prices, GROUP_CURVE_LABEL)
        market_curve = equal_weight_curve(market_prices, MARKET_CURVE_LABEL)
        excluded = _excluded(group_prices, members)
        if excluded:
            notes.append(f"{len(excluded)} 只标的在起点当天没有收盘价（新股或停牌），未计入曲线")
        if group_curve is None and not excluded:
            notes.append("组内标的在起点都没有收盘价，无法计算表现")
        if len(market_prices.index) < MIN_TRADING_DAYS:
            notes.append("入组至今不足两个交易日，暂时看不出涨跌")
        return GroupPerformance(
            group_id=str(record["group_id"]),
            start_date=_format_day(market_prices.index[0]),
            end_date=_format_day(market_prices.index[-1]),
            trading_days=int(len(market_prices.index)),
            group_curve=group_curve,
            market_curve=market_curve,
            excess_return=excess_return(group_curve, market_curve),
            spread=member_spread(group_prices, members),
            excluded=excluded,
            truncated=truncated,
            notes=tuple(notes),
        )


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


def _format_day(day: object) -> str | None:
    """YYYYMMDD 整数 → YYYY-MM-DD，前端两种格式都用得到，统一给带横杠的。"""

    text = str(day).strip()
    if not text.isdigit():
        return None
    padded = text.zfill(8)
    return f"{padded[:4]}-{padded[4:6]}-{padded[6:8]}"


__all__ = ["StockGroupService"]
