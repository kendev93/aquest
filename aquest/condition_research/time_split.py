"""Chronological search, validation, and holdout windows."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class DateWindow:
    start_date: date
    end_date: date


@dataclass(frozen=True)
class ResearchSplits:
    train: DateWindow
    validation: DateWindow
    holdout: DateWindow


def split_calendar_range(
    start_date: date,
    end_date: date,
    *,
    train_ratio: float = 0.6,
    validation_ratio: float = 0.2,
    trading_dates: Sequence[date] | None = None,
) -> ResearchSplits:
    """Split a range by trading sessions when provided, otherwise calendar days."""

    if start_date > end_date:
        raise ValueError("研究开始日期不能晚于结束日期")
    if not 0.0 < train_ratio < 1.0 or not 0.0 < validation_ratio < 1.0:
        raise ValueError("训练和验证比例必须在 0 和 1 之间")
    if train_ratio + validation_ratio >= 1.0:
        raise ValueError("训练和验证比例之和必须小于 1")

    if trading_dates is not None:
        sessions = sorted({day for day in trading_dates if start_date <= day <= end_date})
        if len(sessions) < 3:
            raise ValueError("研究日期范围至少需要 3 个实际交易日才能进行时间切分")
        train_count = int(len(sessions) * train_ratio)
        validation_count = int(len(sessions) * validation_ratio)
        holdout_count = len(sessions) - train_count - validation_count
        if min(train_count, validation_count, holdout_count) < 1:
            raise ValueError("实际交易日不足，无法按指定比例保留训练、验证和留出样本")
        train_end = sessions[train_count - 1]
        validation_start = sessions[train_count]
        validation_end = sessions[train_count + validation_count - 1]
        holdout_start = sessions[train_count + validation_count]
        return ResearchSplits(
            train=DateWindow(sessions[0], train_end),
            validation=DateWindow(validation_start, validation_end),
            holdout=DateWindow(holdout_start, sessions[-1]),
        )

    total_days = (end_date - start_date).days + 1
    train_days = int(total_days * train_ratio)
    validation_days = int(total_days * validation_ratio)
    holdout_days = total_days - train_days - validation_days
    if min(train_days, validation_days, holdout_days) < 1:
        raise ValueError("研究日期范围至少需要 3 天才能进行时间切分")

    train_end = start_date + timedelta(days=train_days - 1)
    validation_start = train_end + timedelta(days=1)
    validation_end = validation_start + timedelta(days=validation_days - 1)
    holdout_start = validation_end + timedelta(days=1)
    return ResearchSplits(
        train=DateWindow(start_date, train_end),
        validation=DateWindow(validation_start, validation_end),
        holdout=DateWindow(holdout_start, end_date),
    )


#: 一个滚动复验窗口至少占这么多自然日。再短就不是"另一个时间段"了，
#: 而是同一段行情切碎后的噪声。
MIN_WINDOW_DAYS = 30


def resolve_window_count(
    start_date: date,
    end_date: date,
    requested: int,
    *,
    min_days: int = MIN_WINDOW_DAYS,
) -> int:
    """How many windows actually fit. 放不下就降级，不报错。

    宁可少几个窗口也不让每个窗口短到没有意义——窗口太短会被标成"样本不足"
    然后从一致性统计里剔掉，等于白跑。
    """

    if requested <= 1:
        return 1
    total_days = (end_date - start_date).days + 1
    if total_days < min_days:
        return 1
    return max(1, min(requested, total_days // min_days))


def split_windows(
    start_date: date,
    end_date: date,
    count: int,
    *,
    min_days: int = MIN_WINDOW_DAYS,
) -> tuple[DateWindow, ...]:
    """把一个日期区间切成 ``count`` 个首尾相接、不重叠不漏的连续窗口。

    count 大于能放下的数量时自动降级（见 ``resolve_window_count``）。
    余数分摊给前面的窗口，保证每窗长度最多差一天。
    """

    if start_date > end_date:
        raise ValueError("窗口开始日期不能晚于结束日期")
    if count < 1:
        raise ValueError("窗口数至少为 1")
    used = resolve_window_count(start_date, end_date, count, min_days=min_days)
    total_days = (end_date - start_date).days + 1
    base, remainder = divmod(total_days, used)
    windows: list[DateWindow] = []
    cursor = start_date
    for index in range(used):
        length = base + (1 if index < remainder else 0)
        window_end = cursor + timedelta(days=length - 1)
        windows.append(DateWindow(cursor, window_end))
        cursor = window_end + timedelta(days=1)
    return tuple(windows)
