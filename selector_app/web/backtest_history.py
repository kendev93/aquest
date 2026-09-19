"""Helpers for recording completed backtest jobs without changing their result."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Literal

from selector_app.market_data.store import DuckDbMarketDataStore

BacktestHistoryType = Literal[
    "single_stock",
    "portfolio",
    "strategy_fitness",
    "condition_research",
    "swing_research",
]

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    """Return an aware UTC timestamp for a task boundary."""

    return datetime.now(timezone.utc)


def save_completed_backtest_history(
    store: DuckDbMarketDataStore,
    *,
    history_id: str,
    backtest_type: BacktestHistoryType,
    request: Mapping[str, object],
    result: Mapping[str, object],
    started_at: datetime,
    finished_at: datetime,
) -> None:
    """Best-effort persistence for a successful backtest result.

    A history write must not turn an already completed backtest into a failed
    job. The exception is logged with the task id so the local operator can
    diagnose a locked or damaged DuckDB independently.
    """

    try:
        store.save_backtest_history(
            history_id,
            backtest_type=backtest_type,
            request=request,
            result=result,
            started_at=started_at,
            finished_at=finished_at,
        )
    except Exception:  # noqa: BLE001 - result delivery must survive history I/O failures
        logger.exception("自动保存%s回测历史失败: %s", backtest_type, history_id)
