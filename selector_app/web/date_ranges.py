"""Shared date-range validation and preset resolution for backtest APIs."""

from __future__ import annotations

from datetime import date
from typing import Literal

from selector_app.market_data.store_base import HISTORY_START

DateRangeMode = Literal["recent5", "full", "custom"]
DEFAULT_LOOKBACK_YEARS = 5


def coerce_date(value: date | str | None) -> date | None:
    """Convert a persisted store date into a calendar date."""

    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"回测日期格式错误: {value}") from exc


def validate_date_range_request(
    mode: DateRangeMode,
    requested_start: date | None,
    requested_end: date | None,
) -> None:
    """Validate the mode/date combination before accessing the data store."""

    if mode == "custom" and (requested_start is None or requested_end is None):
        raise ValueError("自定义日期范围必须同时设置开始日期和结束日期")
    if (requested_start is None) != (requested_end is None):
        raise ValueError("开始日期和结束日期必须同时设置")


def resolve_date_range(
    *,
    mode: DateRangeMode,
    requested_start: date | None,
    requested_end: date | None,
    available_start: date | str | None,
    available_end: date | str | None,
) -> tuple[date | None, date | None]:
    """Resolve an explicit range or a preset against completed local data.

    Explicit dates take precedence so older API clients remain compatible even
    though the default mode is now ``recent5``.  ``full`` returns the actual
    available store bounds, which keeps the persisted request self-describing.
    """

    if mode not in {"recent5", "full", "custom"}:
        raise ValueError("回测日期模式无效")
    validate_date_range_request(mode, requested_start, requested_end)
    if requested_start is not None and requested_end is not None:
        return requested_start, requested_end

    resolved_start = coerce_date(available_start)
    resolved_end = coerce_date(available_end)
    if resolved_start is None or resolved_end is None:
        raise ValueError("行情数据库没有已完成日线，请先同步行情")
    if resolved_start > resolved_end:
        raise ValueError("本地行情日期范围无效")
    if mode == "full":
        # "全历史"是"库里全部"，不是"从 1990 年起"。1990-2014 那段候选池小到不能
        # 代表市场（1990 年全市场只有 4 只股票），摆出来只会让人误选；真删干净了
        # 这个 max 就是空操作，万一将来又有路径把旧数据灌回来（通达信本地导入
        # 干过一次），它至少保证选不到那段样本。
        start = max(resolved_start, HISTORY_START)
        if start > resolved_end:
            raise ValueError("本地行情全部早于保留起点，没有可用样本")
        return start, resolved_end

    target_start = _subtract_calendar_years(resolved_end, DEFAULT_LOOKBACK_YEARS)
    return max(target_start, resolved_start), resolved_end


def _subtract_calendar_years(value: date, years: int) -> date:
    target_year = value.year - years
    try:
        return value.replace(year=target_year)
    except ValueError:
        return value.replace(year=target_year, day=28)


__all__ = [
    "DEFAULT_LOOKBACK_YEARS",
    "DateRangeMode",
    "coerce_date",
    "resolve_date_range",
    "validate_date_range_request",
]
