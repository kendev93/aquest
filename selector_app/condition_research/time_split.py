"""Chronological search, validation, and holdout windows."""

from __future__ import annotations

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
) -> ResearchSplits:
    """Split an inclusive calendar range into ordered, non-overlapping windows."""

    if start_date > end_date:
        raise ValueError("研究开始日期不能晚于结束日期")
    if not 0.0 < train_ratio < 1.0 or not 0.0 < validation_ratio < 1.0:
        raise ValueError("训练和验证比例必须在 0 和 1 之间")
    if train_ratio + validation_ratio >= 1.0:
        raise ValueError("训练和验证比例之和必须小于 1")

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
