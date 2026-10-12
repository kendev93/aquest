"""策略池的「个股买入持有」基准：必须把分红算进去。

这个数字是策略「跑赢基准多少」的分母。早期版本用不复权收盘价算，把 11 年里的
每一次现金分红都吞掉了——浦发银行名义 -41%、按持有者现金流算实际 +31%——于是
「跑赢基准」变成一句空话：策略只要不像指数那么亏就算赢。
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import numpy as np
import pandas as pd
import pytest

from aquest.adapters.local_day_adapter import StockRef
from aquest.individual_research.models import IndividualResearchConfig
from aquest.market_data.adjustment import CorporateAction, compute_factor_ratio
from aquest.strategy_pool.service import (
    BENCHMARK_CALIBER_VERSION,
    _symbol_benchmark,
)

BEFORE = 20.0
CASH_PER_SHARE = 0.5
AFTER = BEFORE - CASH_PER_SHARE
_DATES = pd.date_range("2026-01-05", periods=12, freq="B")
_REF = StockRef(
    market="SH",
    code="600000",
    name="测试600000",
    path=Path("/tmp/vipdoc/sh600000.day"),
)


def _frame() -> pd.DataFrame:
    """第 5 个交易日除权派息 0.5 元，之后价格稳定在 19.5。"""

    closes = np.where(np.arange(len(_DATES)) < 5, BEFORE, AFTER)
    return pd.DataFrame(
        {
            "date": _DATES,
            "open": closes,
            "high": closes + 0.02,
            "low": closes - 0.02,
            "close": closes,
            "volume": np.full(len(_DATES), 100_000.0),
            "amount": closes * 100_000.0,
        }
    )


def _config() -> IndividualResearchConfig:
    return IndividualResearchConfig(
        market="SH",
        code="600000",
        name="测试600000",
        start_date=_DATES[0].date(),
        end_date=_DATES[-1].date(),
        value_ids=(),
        signal_ids=(),
    )


def _actions() -> tuple[CorporateAction, ...]:
    return (
        CorporateAction(
            market="SH",
            code="600000",
            ex_date=_DATES[5].date(),
            share_ratio=0.0,
            cash_per_share=CASH_PER_SHARE,
            factor_ratio=compute_factor_ratio(
                prev_close=BEFORE,
                share_ratio=0.0,
                cash_per_share=CASH_PER_SHARE,
            ),
            action_type="dividend",
        ),
    )


class _Adapter:
    """带分红事件读取的最小行情适配器。"""

    def __init__(self, actions: tuple[CorporateAction, ...] = ()) -> None:
        self._actions = actions

    def read_stock(self, ref: StockRef) -> pd.DataFrame:
        return _frame()

    def read_corporate_actions(self, ref: StockRef) -> tuple[CorporateAction, ...]:
        return self._actions


class _AdapterWithoutActions:
    """不支持事件读取的适配器：基准应当退化成不复权，而不是崩掉。"""

    def read_stock(self, ref: StockRef) -> pd.DataFrame:
        return _frame()


def test_symbol_benchmark_credits_cash_dividends() -> None:
    """基准要把派息算进去：20 → 19.5 之外，持有者手上还多拿了 0.5 元现金。"""

    result = _symbol_benchmark(_Adapter(_actions()), _REF, _config(), {})

    assert result is not None
    full = cast("dict[str, object]", result["full_period"])
    # 不复权口径是 (19.5 / 20) - 1 = -2.5%，漏掉的那 0.5 元正是派息。
    assert float(cast("float", full["total_return"])) == pytest.approx(0.0, abs=1e-9)


def test_symbol_benchmark_falls_back_without_events() -> None:
    """拿不到事件就老实按不复权算：少一个校正不该让整个策略池挂掉。"""

    result = _symbol_benchmark(_AdapterWithoutActions(), _REF, _config(), {})

    assert result is not None
    full = cast("dict[str, object]", result["full_period"])
    assert float(cast("float", full["total_return"])) == pytest.approx(-0.025)


def test_symbol_benchmark_stamps_its_caliber() -> None:
    """口径版本写进结果里，存量行才能被认出来、在下次重选时重算。"""

    result = _symbol_benchmark(_Adapter(_actions()), _REF, _config(), {})

    assert result is not None
    assert result["caliber_version"] == BENCHMARK_CALIBER_VERSION
    assert "分红再投资" in str(result["method"])
    # 版本要落得下来：它是 JSON 的一个键，必须能被 dump 成标量字符串。
    assert isinstance(result["caliber_version"], str)
