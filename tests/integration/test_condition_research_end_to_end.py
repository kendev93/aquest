from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from selector_app.backtest.service import BacktestService
from selector_app.condition_research.models import ConditionResearchConfig
from selector_app.condition_research.service import ConditionResearchService
from selector_app.market_data.models import StockRef
from selector_app.screening.rules import RuleGroup

_DEFAULT_PATTERN = np.array([0, 1, 2, 1, 0, -1, 0, 2, 3, 2, 0, -1], dtype=float)
# 训练段需要同时出现盈利与亏损交易，挖掘阶段才可能产出候选。
_MIXED_OUTCOME_PATTERN = np.array([0, 1, 2, 3, 4, 3, 2, 1, 0, -1, -2, -1], dtype=float)


class FakeUniverseAdapter:
    def __init__(self, pattern: np.ndarray | None = None, tiles: int = 4) -> None:
        self.refs = [
            StockRef("SH", "600000", Path("/tmp/vipdoc"), name="测试甲", instrument_type="stock"),
            StockRef("SZ", "000001", Path("/tmp/vipdoc"), name="测试乙", instrument_type="stock"),
        ]
        self.pattern = _DEFAULT_PATTERN if pattern is None else pattern
        self.tiles = tiles
        self.frames = {ref.code: self._frame(offset) for offset, ref in enumerate(self.refs)}

    def _frame(self, offset: int) -> pd.DataFrame:
        close = 10 + self.pattern + offset
        close = np.tile(close, self.tiles)
        return pd.DataFrame(
            {
                "date": pd.date_range("2020-01-01", periods=len(close), freq="D"),
                "open": close,
                "high": close + 0.4,
                "low": close - 0.4,
                "close": close,
                "volume": np.full(len(close), 100_000.0),
                "amount": close * 100_000.0,
                "bar_status": "completed",
            }
        )

    def list_stock_refs(
        self,
        vipdoc_path,
        universe,
        universe_file=None,
        instrument_types=None,
        boards=None,
    ):
        del vipdoc_path, universe_file, boards
        refs = self.refs
        if universe == "sh":
            refs = [ref for ref in refs if ref.market == "SH"]
        elif universe == "sz":
            refs = [ref for ref in refs if ref.market == "SZ"]
        if instrument_types:
            refs = [ref for ref in refs if ref.instrument_type in instrument_types]
        return refs

    def stock_ref(self, vipdoc_path, market, code):
        del vipdoc_path
        return next(ref for ref in self.refs if ref.market == market and ref.code == code)

    def read_stock(self, ref):
        return self.frames[ref.code].copy(deep=True)


def test_condition_research_runs_independent_stock_baselines_and_time_splits() -> None:
    adapter = FakeUniverseAdapter()
    service = ConditionResearchService(
        adapter=adapter,
        backtest_service=BacktestService(adapter=adapter),
    )
    config = ConditionResearchConfig(
        vipdoc_path="/tmp/vipdoc",
        universe="all",
        buy_rule=RuleGroup(signals=("custom.buy",)),
        sell_rule=RuleGroup(signals=("custom.sell",)),
        formula_text=("BUY:CROSS(C,REF(C,1)); SELL:CROSS(REF(C,1),C); SCORE:C/MA(C,3);"),
        start_date=date(2020, 1, 1),
        end_date=date(2020, 2, 18),
        min_trades=1,
        min_instruments=1,
        candidate_limit=3,
        holdout_candidates=1,
    )

    result = service.run(config)

    assert result["total_candidates"] == 2
    assert result["processed"] == 2
    assert "validation" in result["baseline"]
    assert "full_period" in result["baseline"]
    assert "holdout" in result["baseline"]
    assert result["splits"]["train"]["end_date"] < result["splits"]["validation"]["start_date"]
    assert result["splits"]["validation"]["end_date"] < result["splits"]["holdout"]["start_date"]


def test_condition_research_mines_candidates_from_real_engine_trades() -> None:
    """真实引擎的成交记录必须能转成挖掘样本，否则研究恒返回空候选。

    回归背景：成交记录投影曾丢掉 ``signal_date``，采样器因此拿到 ``None``，
    样本集恒为空，研究只会返回“基础交易样本不足”而没有任何候选。
    """

    adapter = FakeUniverseAdapter(pattern=_MIXED_OUTCOME_PATTERN, tiles=20)
    service = ConditionResearchService(
        adapter=adapter,
        backtest_service=BacktestService(adapter=adapter),
    )
    config = ConditionResearchConfig(
        vipdoc_path="/tmp/vipdoc",
        universe="all",
        buy_rule=RuleGroup(signals=("custom.buy",)),
        sell_rule=RuleGroup(signals=("custom.sell",)),
        formula_text=("BUY:CROSS(C,REF(C,1)); SELL:CROSS(REF(C,1),C); SCORE:C/MA(C,3);"),
        start_date=date(2020, 1, 1),
        end_date=date(2020, 8, 27),
        min_trades=1,
        min_instruments=1,
        candidate_limit=3,
        holdout_candidates=1,
    )

    result = service.run(config)

    pooled = result["baseline"]["train"]["pooled_trades"]
    assert pooled["closed_trades"] > 0
    assert 0.0 < pooled["win_rate"] < 1.0
    assert result["candidates"], str(result.get("diagnostic") or "未挖出候选条件")
