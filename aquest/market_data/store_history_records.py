"""Composite mixin for the history records persisted by DuckDB."""

from __future__ import annotations

from .store_history_backtests import BacktestHistoryMixin
from .store_history_discovery import StrategyDiscoveryHistoryMixin
from .store_history_screen import ScreenHistoryMixin


class HistoryRecordsMixin(BacktestHistoryMixin, StrategyDiscoveryHistoryMixin, ScreenHistoryMixin):
    pass
